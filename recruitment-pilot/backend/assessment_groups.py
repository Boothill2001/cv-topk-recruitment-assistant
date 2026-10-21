"""Durable, sequential full-source orchestration; TopK only limits reads."""
import json, uuid
from . import db, engine, content_fetch, public_assessment, assessment_scoring, ai_assessment, jev
from .errors import IntegrationError
from .ai_runtime import redact_error
from .prompts import PROMPTS
from .source_identity import distinct_sources

VERSION = 'all-public-sources-3'
AI_BATCH_SIZE = 5
SQL = '''CREATE TABLE IF NOT EXISTS assessment_groups (
 id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, search_id TEXT NOT NULL,
 job_id TEXT NOT NULL, config_version INTEGER NOT NULL, snapshot TEXT NOT NULL,
 status TEXT NOT NULL, data TEXT NOT NULL, error TEXT, created TEXT NOT NULL, updated TEXT NOT NULL);
 CREATE INDEX IF NOT EXISTS assessment_groups_search ON assessment_groups(search_id);
'''

def row(gid):
    value = db.one('SELECT * FROM assessment_groups WHERE id=?', (gid,))
    if not value: raise KeyError(gid)
    return {**value, 'snapshot': json.loads(value['snapshot']), 'data': json.loads(value['data'])}

def compatible(value):
    snap = value['snapshot']
    c = db.one('SELECT * FROM configs WHERE job_id=?', (value['job_id'],))
    p = db.one("SELECT revision,status FROM profiles WHERE id=? AND kind='job'", (value['job_id'],))
    jev_mode=snap.get('assessment_engine')=='JEV'
    model_ok=(snap['model']==jev.MODEL and snap.get('adapter_version')==jev.VERSION and snap['prompt_hash']==engine.digest(jev.POLICY)) if jev_mode else (
        db.setting('provider','deepseek')==snap['provider'] and db.setting('model','deepseek-flash')==snap['model']
        and engine.digest(PROMPTS['public_evidence_assessment'])==snap['prompt_hash'] and ai_assessment.model_budget()==snap['model_budget'])
    return bool(snap.get('version')==VERSION and c and p and c['version']==value['config_version'] and c['criteria_approved'] and c['approved']
        and p['revision']==snap['job_revision'] and p['status']=='READY' and model_ok
        and content_fetch.VERSION==snap['content_adapter'] and assessment_scoring.VERSION==snap['scorer_version'])

def save(gid, data):
    db.execute('UPDATE assessment_groups SET data=?,updated=? WHERE id=?', (db.dumps(data), db.now(), gid))

@engine.serialized
def create(sid, version, assessment_engine="CURRENT"):
    if assessment_engine not in ("CURRENT","JEV"):raise IntegrationError("Engine không hợp lệ.")
    if assessment_engine=='JEV' and jev.credential_problem():raise IntegrationError(jev.credential_problem())
    search = content_fetch.search_detail(sid)
    job_id = search.get('job_id')
    if not job_id: raise IntegrationError('Chọn nhóm People theo JD để xử lý tất cả nguồn.')
    c, jp = engine.checked_config(job_id, version)
    if assessment_engine=='CURRENT' and not ai_assessment.model_budget().get('verified'):
        raise IntegrationError('Chưa xác nhận giới hạn context/output của model đã chọn. Cập nhật budget trước khi lấy nội dung và chấm; không tự đổi model.')
    if not c['criteria_approved'] or not c['approved'] or not search.get('is_current'):
        raise IntegrationError('Xác nhận JD và hướng tìm hiện hành trước khi xử lý tất cả.')
    if search['status'] not in ('COMPLETED', 'PARTIAL'): raise IntegrationError('Chờ tìm nguồn hoàn tất.')
    sources = distinct_sources(search.get('results', []))
    if not sources: raise IntegrationError('Chưa có nguồn để xử lý.')
    if not all(content_fetch.exa_search.safe_url(s['url']) for s in sources): raise IntegrationError('URL nguồn không hợp lệ.')
    config = json.loads(c['data'])
    snap = {'version': VERSION, 'job_revision': jp['revision'], 'config': config,
            'public_jd': [s['text'] for s in engine.sources(job_id, 'job') if s['group'] == 'job_public'],
            'sources': sources, 'search_snapshot': search['snapshot'],
            'provider': db.setting('provider', 'deepseek'), 'model': db.setting('model', 'deepseek-flash'),
            'prompt_hash': engine.digest(PROMPTS['public_evidence_assessment']),
            'scorer_version': assessment_scoring.VERSION, 'content_adapter': content_fetch.VERSION,
            'model_budget': ai_assessment.model_budget()}
    if assessment_engine=='JEV':snap.update(**jev.settings())
    fp = engine.digest(db.dumps(snap))
    with db.LOCK, db.conn() as connection:
        old = connection.execute('SELECT id FROM assessment_groups WHERE fingerprint=?', (fp,)).fetchone()
        if old: return detail(old['id'])
        gid = str(uuid.uuid4()); now = db.now()
        urls = [s['url'] for s in sources]
        data = {'chunks': [{'urls': urls[i:i+20], 'fetch_id': None} for i in range(0, len(urls), 20)], 'batches': [], 'excluded': {}}
        connection.execute('INSERT INTO assessment_groups VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                           (gid, fp, sid, job_id, version, db.dumps(snap), 'PENDING', db.dumps(data), None, now, now))
        connection.execute('INSERT INTO tasks(kind,payload,status,created,updated) VALUES(?,?,?,?,?)',
                           ('assessment_group', db.dumps({'group_id': gid}), 'PENDING', now, now))
    return detail(gid)

def preview(sid, version, assessment_engine="CURRENT"):
    """Local-only cache inspection; no tasks, provider requests or money estimates."""
    from datetime import datetime, timedelta, timezone
    search = content_fetch.search_detail(sid)
    c, jp = engine.checked_config(search['job_id'], version)
    if not c['approved'] or not c['criteria_approved'] or not search.get('is_current'):
        raise IntegrationError('JD và hướng tìm cần được xác nhận theo yêu cầu hiện tại.')
    sources = distinct_sources(search.get('results', []))
    cutoff = (datetime.now(timezone.utc)-timedelta(hours=24)).isoformat(); cached = {}
    for entry in db.rows('SELECT data FROM content_fetches WHERE updated>=? ORDER BY updated DESC', (cutoff,)):
        for item in json.loads(entry['data']).get('results', []):
            if item.get('status')=='AVAILABLE' and item.get('fetched_at','')>=cutoff and item.get('adapter_version')==content_fetch.VERSION and item.get('provider_source')!='user-provided-pdf':
                cached.setdefault(item['url'], item)
    config = json.loads(c['data'])
    snap = {'criteria':[c for c in config['criteria'] if c['enabled']],
            'public_jd':[s['text'] for s in engine.sources(search['job_id'],'job') if s['group']=='job_public'],
            'content_fetch_id':'cached-preview','assessment_engine':assessment_engine}
    all_records = []; available = 0
    for offset in range(0,len(sources),20):
        records = []
        for index, source in enumerate(sources[offset:offset+20],1):
            item = cached.get(source['url'])
            if not item: continue
            available += 1
            parts = content_fetch.blocks(item['text'])
            records.append({'id':f'PUB{index:03d}','title':source.get('title',''),'url':source['url'],
                **(public_assessment.paragraph_catalog if assessment_engine=='JEV' else public_assessment.selection_catalog)(parts,f'PUB{index:03d}'), 'revision':item['revision'], 'raw_text':item['text'] if assessment_engine=='JEV' else None,
                'warnings':item.get('warnings',[]),'provider_source':item.get('provider_source'),'fetched_at':item.get('fetched_at')})
        all_records.extend(records)
    batches, excluded = plan_batches({**snap,'sources':all_records})
    return {'total_sources':len(sources),'cached_available':available,'needs_content':len(sources)-available,
            'cached_batches':batches,'cached_excluded':excluded,'assessment_engine':assessment_engine,
            'model_budget':jev.settings()['model_budget'] if assessment_engine=='JEV' else ai_assessment.model_budget(),
            'jev_preview':jev.preview({**snap,'sources':all_records}) if assessment_engine=='JEV' else None,
            'new_provider_calls':None,'cost_dollars':None,'provider_called':False,
            'note':'Chỉ kiểm tra cache hiện có; số batch cuối được tính sau khi lấy nội dung. Không có tác vụ có phí.'}

def detail(gid, limit=None):
    value = row(gid); snap = value['snapshot']; data = value['data']
    sources = {s['url']: s for s in snap['sources']}; order = {u: i for i, u in enumerate(sources)}
    contents = {}; batches = []; results = []; costs = []; usages = []; reasons = {}
    reasons.update(data['excluded'])
    for chunk in data['chunks']:
        if chunk['fetch_id']:
            fetch = content_fetch.detail(chunk['fetch_id'])
            contents.update({s['url']: s for s in fetch['results']})
            costs.extend(fetch.get('usage', []))
    for batch in data['batches']:
        a = public_assessment.detail(batch['assessment_id']) if batch.get('assessment_id') else None
        batches.append({'urls': batch['urls'], 'assessment_id': batch.get('assessment_id'),
                        'status': a['status'] if a else 'PENDING', 'attempts': a['attempts'] if a else 0,
                        'error': a.get('error') if a else None,'steps':a.get('steps',[]) if a else []})
        if a:
            usages.append({'assessment_id': a['id'], **a.get('usage', {})})
            reasons.update(a.get('source_errors',{}))
            if a['status'] == 'COMPLETED' or snap.get('assessment_engine')=='JEV':
                for result in a.get('results', []):
                    results.append({**result, 'assessment_id': a['id'], 'child_source_id': result['candidate_id'],
                                    'candidate_id': 'SRC'+engine.digest(result['url'])[:24], 'be_rank': order[result['url']]})
            else:
                for u in batch['urls']: reasons[u] = a.get('error') or 'Batch chưa chấm xong: '+a['status']
    # URL, not name or local PUB IDs, is the identity across independently validated batches.
    if data.get('inherited_results'):
        results = data['inherited_results'] + results
    results = assessment_scoring.ordered(list({r['url']: r for r in results}.values()))
    done = {r['url'] for r in results}
    unassessed = []
    for u, s in sources.items():
        if u in done: continue
        item = contents.get(u, {})
        unassessed.append({'url': u, 'title': s.get('title'), 'status': item.get('status', 'PENDING'),
                           'reason': reasons.get(u) or item.get('error') or ('Chưa đánh giá AI' if item.get('status') == 'AVAILABLE' else 'Chưa có text khả dụng; không tự dùng đoạn trích'),
                           'fetch_id': next((c['fetch_id'] for c in data['chunks'] if u in c['urls']), None)})
    fetches = [{'id': c['fetch_id'], 'source_count': len(c['urls']), **({k: content_fetch.detail(c['fetch_id']).get(k) for k in ('status', 'attempts', 'error')} if c['fetch_id'] else {'status': 'PENDING'})} for c in data['chunks']]
    from datetime import datetime
    from .candidate_ranking import ranked_candidates, VERSION as ranking_version
    candidates = ranked_candidates(results, snap['config']['criteria'])
    elapsed = (datetime.fromisoformat(db.now() if value['status'] in ('PENDING','RUNNING') else value['updated'])-datetime.fromisoformat(value['created'])).total_seconds()
    return {k: value[k] for k in ('id','search_id','job_id','config_version','status','error','created','updated')} | {
        'elapsed_seconds': round(elapsed, 1),
        'parent_group_id':snap.get('parent_group_id'),
        'mixed_adapter_versions':bool(data.get('inherited_results')),
        'retained_sources':len(data.get('inherited_results',[])),
        'new_assessed_sources':len(results)-len(data.get('inherited_results',[])),
        'assessment_engine':snap.get('assessment_engine','CURRENT'),'adapter_version':snap.get('adapter_version'),
        'ai_estimated_cost_dollars':sum(u.get('estimated_cost_dollars',0) for u in usages) if snap.get('assessment_engine')=='JEV' else None,
        'is_current': compatible(value), 'provider': snap['provider'], 'model': snap['model'],
        'criteria': [c for c in snap['config']['criteria'] if c['enabled']], 'scorer_version': snap['scorer_version'],
        'total_sources': len(sources), 'content_available': sum(s.get('status') == 'AVAILABLE' for s in contents.values()),
        'assessed_sources': len(results), 'unassessed_sources': unassessed,
        'total_candidates': len(candidates), 'ranking_policy_version': ranking_version,
        'source_results': results,
        'results': candidates if limit is None else candidates[:limit], 'batches': batches, 'content_fetches': fetches,
        'usage': usages, 'total_tokens': sum(u.get('total_tokens',0) or 0 for u in usages),
        'content_cost_dollars': sum(c.get('cost_dollars') or 0 for c in costs),
        'content_cost_unreported': any(c.get('cost_dollars') is None for c in costs),
        'ai_cost_dollars': None, 'provisional': value['status'] != 'COMPLETED', 'policy_benchmarked': False}

def plan_batches(snap):
    """Stable contiguous packing; never trim a source or hide singleton failures."""
    groups = []; pending = []; excluded = {}
    for source in snap['sources']:
        check=public_assessment.budget_preview({**snap, 'sources': pending+[source]})
        if len(pending) < (1 if snap.get('assessment_engine')=='JEV' else AI_BATCH_SIZE) and check.get('technical_fits',check['fits']):
            pending.append(source); continue
        if pending: groups.append([s['url'] for s in pending]); pending = []
        preview = public_assessment.budget_preview({**snap, 'sources': [source]})
        if preview.get('technical_fits',preview['fits']): pending = [source]
        else: excluded[source['url']] = 'Nguồn đơn lẻ vượt giới hạn: '+db.dumps(preview['violations'])
    if pending: groups.append([s['url'] for s in pending])
    return groups, excluded

def content_manifest(value, available):
    """Immutable local manifest across fetch chunks. This never calls Exa."""
    snapshot = {'search_id':value['search_id'], 'parent_group_id':value['id'], 'kind':'content-manifest',
                'adapter_version':content_fetch.VERSION,
                'revisions':{s['url']:s.get('revision') for s in available}}
    fingerprint = engine.digest(db.dumps(snapshot))
    old = db.one('SELECT id FROM content_fetches WHERE fingerprint=?',(fingerprint,))
    if old: return old['id']
    fid = str(uuid.uuid4()); now = db.now()
    db.execute('INSERT INTO content_fetches VALUES(?,?,?,?,?,?,?,?,?,?)',
               (fid,fingerprint,value['search_id'],db.dumps(snapshot),'COMPLETED',
                db.dumps({'results':available,'usage':[],'known_cost_dollars':0,'has_unreported_cost':False}),0,None,now,now))
    return fid

async def run(gid):
    with db.LOCK, db.conn() as connection:
        claim = connection.execute("UPDATE assessment_groups SET status='RUNNING',updated=? WHERE id=? AND status IN ('PENDING','INTERRUPTED') RETURNING id", (db.now(), gid)).fetchone()
    if not claim: return
    value = row(gid); data = value['data']; retrying = data.pop('retry_requested', False)
    try:
        if value['snapshot'].get('assessment_engine')=='JEV' and jev.credential_problem():raise IntegrationError(jev.credential_problem())
        for chunk in data['chunks']:
            if not compatible(value): raise IntegrationError('JD, config, provider/model hoặc prompt đã đổi. Giữ kết quả cũ; tạo lượt theo yêu cầu hiện tại.')
            if not chunk['fetch_id']:
                chunk['fetch_id'] = content_fetch.create(value['search_id'], chunk['urls'], enqueue=False)['id']
                save(gid, data)
            fetch = content_fetch.detail(chunk['fetch_id'])
            if not value['snapshot'].get('parent_group_id') and (fetch['status'] in ('PENDING','INTERRUPTED') or (retrying and fetch['status'] in ('FAILED','PARTIAL') and fetch['attempts'] < 3)):
                await content_fetch.run(fetch['id'])
            save(gid, data)
        if not compatible(value): raise IntegrationError('Input đã đổi; giữ nội dung và tạo lượt cập nhật.')
        contents = {s['url']:s for chunk in data['chunks'] for s in content_fetch.detail(chunk['fetch_id'])['results'] if s['status']=='AVAILABLE'}
        assigned = {u for batch in data['batches'] for u in batch['urls']} | {r['url'] for r in data.get('inherited_results',[])}
        available = [contents[s['url']] for s in value['snapshot']['sources'] if s['url'] in contents and s['url'] not in assigned]
        if available:
            fid = content_manifest(value, available)
            # Construct each source with the existing snapshot/evidence builder, then pack
            # globally across Exa chunk boundaries. No extra AI calls for reranking.
            records = []; base = None
            for offset in range(0,len(available),20):
                base = public_assessment.create(value['job_id'],value['config_version'],value['search_id'],
                    [s['url'] for s in available[offset:offset+20]],content_fetch_id=fid,snapshot_only=True,enqueue=False,assessment_engine=value["snapshot"].get("assessment_engine","CURRENT"))
                records.extend(base['sources'])
            planned, excluded = plan_batches({**base,'sources':records})
            data['excluded'].update(excluded)
            for urls in planned:
                data['batches'].append({'urls':urls,'assessment_id':None,'fetch_id':fid})
                for u in urls: data['excluded'].pop(u,None)
            save(gid,data)
        if retrying and value['snapshot'].get('assessment_engine')=='JEV':
            for previous in data['batches']:
                if previous.get('assessment_id'):jev.prepare_retry(previous['assessment_id'])
        for batch in data['batches']:
            if value['snapshot'].get('assessment_engine')=='JEV' and any(
                step['status']=='FATAL' for previous in data['batches'] if previous.get('assessment_id')
                for step in jev.progress(previous['assessment_id'])):
                raise IntegrationError('Jev dừng vì lỗi key/schema/model. Kết quả đã validate được giữ nguyên; sửa cấu hình trước khi tạo lượt mới.')
            if not compatible(value): raise IntegrationError('Input đã đổi trong lúc chạy; giữ kết quả đã validate và tạo lượt mới để cập nhật.')
            if not batch['assessment_id']:
                batch['assessment_id'] = public_assessment.create(value['job_id'],value['config_version'],value['search_id'],batch['urls'],
                    content_fetch_id=batch['fetch_id'],enqueue=False,assessment_engine=value['snapshot'].get('assessment_engine','CURRENT'))['id']
                save(gid,data)
            child = public_assessment.detail(batch['assessment_id'])
            if child['status'] in ('PENDING','INTERRUPTED') or (retrying and child['status'] in ('FAILED','PARTIAL','WAITING_BUDGET') and child['attempts']<3):
                try: await public_assessment.assess(child['id'])
                except Exception: pass  # Child stores redacted failure; other batches continue.
            child=public_assessment.detail(batch['assessment_id'])
            if child['status']=='WAITING_BUDGET':
                db.execute("UPDATE assessment_groups SET status='WAITING_BUDGET',error=?,updated=? WHERE id=?",('Chờ xác nhận ngân sách bổ sung; giữ tiến trình đã validate.',db.now(),gid))
                return
            save(gid,data)
        status = 'COMPLETED' if detail(gid)['assessed_sources'] == len(value['snapshot']['sources']) else 'PARTIAL'
        db.execute('UPDATE assessment_groups SET status=?,error=NULL,updated=? WHERE id=?', (status, db.now(), gid))
    except Exception as error:
        db.execute("UPDATE assessment_groups SET status='PARTIAL',error=?,updated=? WHERE id=?", (redact_error(error), db.now(), gid))

@engine.serialized
def retry(gid):
    value = row(gid)
    if value['status'] in ('PENDING','RUNNING','COMPLETED'): return detail(gid)
    if not compatible(value): raise IntegrationError('Input đã đổi; tạo lượt theo yêu cầu hiện tại.')
    report = detail(gid)
    eligible = any(f['status'] in ('FAILED','PARTIAL','INTERRUPTED') and f.get('attempts',0)<3 for f in report['content_fetches']) or any(b['status'] in ('FAILED','PARTIAL','INTERRUPTED','WAITING_BUDGET') and b['attempts']<3 for b in report['batches'])
    if not eligible: raise IntegrationError('Không còn lượt con có thể thử lại; xem lý do và bổ sung hồ sơ hoặc xử lý riêng.')
    data = value['data']; data['retry_requested'] = True
    with db.LOCK, db.conn() as connection:
        connection.execute("UPDATE assessment_groups SET status='PENDING',data=?,error=NULL,updated=? WHERE id=?", (db.dumps(data), db.now(), gid))
        connection.execute('INSERT INTO tasks(kind,payload,status,created,updated) VALUES(?,?,?,?,?)', ('assessment_group',db.dumps({'group_id':gid}),'PENDING',db.now(),db.now()))
    return detail(gid)

def frozen_contents(value):
    contents={}
    for reference in [*value['data']['chunks'],*value['data']['batches']]:
        if reference.get('fetch_id'):
            contents.update({s['url']:s for s in content_fetch.detail(reference['fetch_id'])['results']})
    return contents

def continuation_preview(gid):
    """Read frozen content even after cache TTL. Never refetch or reinterpret JD."""
    value=row(gid)
    if value['snapshot'].get('assessment_engine')!='JEV':raise IntegrationError('Chấm tiếp phiên bản này chỉ hỗ trợ Jev.')
    report=detail(gid); done={r['url'] for r in report['source_results']}; contents=frozen_contents(value)
    records=[]; missing=[]
    for index,s in enumerate(value['snapshot']['sources'],1):
        if s['url'] in done:continue
        item=contents.get(s['url'])
        if not item or item.get('status')!='AVAILABLE' or not item.get('revision') or not item.get('text','').strip():
            missing.append(s['url']);continue
        if item.get('content_hash') and item['content_hash']!=engine.digest(item['text']):
            raise IntegrationError('Hash nội dung đã lưu không khớp; không dùng nguồn bị thay đổi.')
        sid=f'PUB{index:03d}'
        records.append({'id':sid,'url':s['url'],'title':s.get('title',''),
            **public_assessment.paragraph_catalog(content_fetch.blocks(item['text']),sid),
            'raw_text':item['text'],'revision':item['revision'],'provider_source':item.get('provider_source')})
    snap=value['snapshot']; criteria=[c for c in snap['config']['criteria'] if c['enabled']]
    pre=jev.preview({'sources':records,'criteria':criteria})
    from .candidate_ranking import identity
    return {'parent_group_id':gid,'retained_sources':len(done),'remaining_sources':len(records),
            'total_urls':len(snap['sources']),
            'total_candidates':len({identity(s['url']) for s in snap['sources']}),
            'remaining_candidates':len({identity(s['url']) for s in records}),
            'missing_content_urls':missing,'provider_called':False,'jev_preview':pre,
            'adapter_version':jev.VERSION,'new_exa_calls':0,'mixed_adapter_versions':bool(done)}

@engine.serialized
def continue_group(gid):
    value=row(gid); pre=continuation_preview(gid)
    if value['status'] in ('PENDING','RUNNING'):raise IntegrationError('Chờ lượt đang chạy hoàn tất.')
    c,jp=engine.checked_config(value['job_id'],value['config_version'])
    if jp['revision']!=value['snapshot']['job_revision']:raise IntegrationError('JD revision đã đổi; không chấm tiếp snapshot vào yêu cầu khác.')
    if not pre['remaining_sources']:raise IntegrationError('Không còn nguồn có text để chấm tiếp.')
    if not pre['jev_preview']['technical_fits']:raise IntegrationError(db.dumps(pre['jev_preview']['violations']))
    snap={**value['snapshot'],**jev.settings(),'parent_group_id':gid,'version':VERSION}
    report=detail(gid)
    contents=frozen_contents(value)
    snap['continuation_basis']=engine.digest(db.dumps({'results':report['results'],
        'revisions':{u:engine.digest(s.get('text','')) for u,s in contents.items()}}))
    fp=engine.digest(db.dumps(snap)); now=db.now(); new_id=str(uuid.uuid4())
    frozen_id=content_manifest(value,[contents[s['url']] for s in snap['sources'] if s['url'] in contents])
    data={'chunks':[{'urls':[s['url'] for s in snap['sources']],'fetch_id':frozen_id}],'batches':[],'excluded':{},
          'inherited_results':[{**r,'adapter_version':r.get('adapter_version') or value['snapshot'].get('adapter_version'),
                               'origin_group_id':r.get('origin_group_id',gid)} for r in report['source_results']]}
    with db.LOCK,db.conn() as conn:
        old=conn.execute('SELECT id FROM assessment_groups WHERE fingerprint=?',(fp,)).fetchone()
        if old:return detail(old['id'])
        conn.execute('INSERT INTO assessment_groups VALUES(?,?,?,?,?,?,?,?,?,?,?)',
          (new_id,fp,value['search_id'],value['job_id'],value['config_version'],db.dumps(snap),'PENDING',db.dumps(data),None,now,now))
        conn.execute('INSERT INTO tasks(kind,payload,status,created,updated) VALUES(?,?,?,?,?)',
          ('assessment_group',db.dumps({'group_id':new_id}),'PENDING',now,now))
    return detail(new_id)

def recover():
    # Only this orchestrator resumes automatically. Completed children remain immutable.
    db.execute("UPDATE assessment_groups SET status='PENDING' WHERE status IN ('RUNNING','INTERRUPTED')")
    db.execute("UPDATE tasks SET status='PENDING',error=NULL WHERE kind='assessment_group' AND status='INTERRUPTED'")
