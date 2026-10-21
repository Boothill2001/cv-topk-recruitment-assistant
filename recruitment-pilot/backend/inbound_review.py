"""Explicit, frozen local CV review sets. No retrieval filter and no ingestion calls."""
import copy
import json
import uuid
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import Field
from typing import Literal
from . import db, engine, jev, pipeline, assessment_scoring, internal_jev
from .models import Strict
from .errors import IntegrationError

VERSION = 'inbound-review-1'
SQL = '''CREATE TABLE IF NOT EXISTS inbound_review_sets (
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL, fingerprint TEXT NOT NULL UNIQUE,
 snapshot TEXT NOT NULL, created TEXT NOT NULL, search_id TEXT UNIQUE,
 revealed TEXT);
 CREATE TABLE IF NOT EXISTS inbound_review_labels (
 id TEXT PRIMARY KEY, set_id TEXT NOT NULL, candidate_id TEXT NOT NULL,
 version INTEGER NOT NULL, phase TEXT NOT NULL, data TEXT NOT NULL, created TEXT NOT NULL,
 UNIQUE(set_id,candidate_id,version));'''
router = APIRouter(prefix='/api/v1', tags=['Local inbound review'])


def local():
    from .online import enabled
    if enabled():
        raise HTTPException(403, 'Kho CV và Jev inbound chưa mở trên bản online.')


def row(rid):
    local()
    r = db.one('SELECT * FROM inbound_review_sets WHERE id=?', (rid,))
    if not r:
        raise HTTPException(404, 'Không tìm thấy tập kiểm chứng.')
    return r


def stored_snapshot(r):
    # CV bodies live once in validated, content-addressed artifacts.
    return pipeline.thaw(json.loads(r['snapshot']))


def catalog():
    local()
    records = {}
    for f in db.rows("SELECT id,entity_id,name,revision,status,hash,url FROM files WHERE group_name='candidate_public' AND available=1 ORDER BY entity_id,id"):
        if f['entity_id']:
            item = records.setdefault(f['entity_id'], {'candidate_id': f['entity_id'], 'title': f['name'] or f['entity_id'], 'sources': []})
            item['sources'].append(f)
    # Structured profiles can contain note-derived summaries: never copy them.
    for p in db.rows("SELECT id FROM profiles WHERE kind='candidate'"):
        records.setdefault(p['id'], {'candidate_id': p['id'], 'title': p['id'], 'sources': []})
    hashes = {}
    for item in records.values():
        for f in item['sources']:
            if f['hash']:
                hashes.setdefault(f['hash'], set()).add(item['candidate_id'])
    for item in records.values():
        item['duplicate_candidate_ids'] = sorted({cid for f in item['sources'] for cid in hashes.get(f['hash'], []) if cid != item['candidate_id']})
    return sorted(records.values(), key=lambda v: v['candidate_id'])


def snapshot(job_id, version, ids):
    local()
    if not 1 <= len(ids) <= 20 or len(ids) != len(set(ids)):
        raise HTTPException(422, 'Chọn 1–20 ID CV khác nhau; đây là tập kiểm chứng, không phải giới hạn kho.')
    cfg = db.one('SELECT * FROM configs WHERE job_id=?', (job_id,))
    job = db.one("SELECT * FROM profiles WHERE id=? AND kind='job'", (job_id,))
    if not cfg or not job or cfg['version'] != version or not cfg['criteria_approved'] or cfg['source_revision'] != job['revision']:
        raise HTTPException(409, 'JD/config đã đổi hoặc chưa duyệt yêu cầu. Tải lại trước khi chọn tập.')
    candidates = {v['candidate_id']: v for v in catalog()}
    if any(cid not in candidates for cid in ids):
        raise HTTPException(422, 'Có ID CV không thuộc kho hiện tại.')
    group = {}
    for rank, cid in enumerate(ids, 1):
        sources = [{'source_id': f['id'], 'revision': f['revision'], 'group': 'candidate_public', 'hash': f['hash'], 'status': f['status'], 'url':f['url']}
                   for f in candidates[cid]['sources']]
        group[cid] = {'profile': {'title': candidates[cid]['title']}, 'revision': None,
                      'sources': sources, 'be_rank': rank, 'be_evaluation': {'score': 0},
                      'duplicate_candidate_ids': candidates[cid]['duplicate_candidate_ids']}
    # No private notes or structured CV facts are required for this entry point.
    snap = {'job': {'id': job_id, 'revision': job['revision']}, 'config_version': version,
            'config': json.loads(cfg['data']), 'group': group, 'pool_size': len(candidates),
            'scope': 'inbound-review-set', 'assessment_engine': 'JEV',
            'internal_adapter_version': internal_jev.VERSION,
            'review_version': VERSION, 'scorer_version': assessment_scoring.VERSION}
    snap['jev'] = internal_jev.build(snap, exact_offsets=True)
    return snap


def fingerprint(snap):
    # Growth elsewhere in the pool does not change this explicitly selected set.
    return pipeline.digest(pipeline.canonical({k:v for k,v in snap.items() if k!='pool_size'}))


def preflight(snap):
    from .jev_planner import atomic_criteria
    budget = jev.preview(snap['jev'])
    _, layout = atomic_criteria(snap['jev']['criteria'])
    rubric = [{'criterion_id': cid, 'reason': v['reason']} for cid, v in layout.items() if v.get('requires_review')]
    return {'fingerprint': fingerprint(snap), 'selected_count': len(snap['group']),
            'text_count': len(snap['jev']['sources']), 'missing_sources': snap['jev']['missing'],
            'rubric_review': rubric, 'budget': budget,
            'can_assess': bool(snap['jev']['sources']) and not rubric and budget['fits'],
            'warning': 'Text CV gửi tới TypeSafe/Jev; không gồm private note. Localhost không có nghĩa là xử lý hoàn toàn offline.'}


def versions_current(snap):
    return (snap['review_version']==VERSION and snap['internal_adapter_version']==internal_jev.VERSION
            and snap['jev']['adapter_version']==jev.VERSION and snap['scorer_version']==assessment_scoring.VERSION
            and snap['jev']['model']==jev.MODEL
            and snap['jev']['prompt_hash']==engine.digest(db.dumps([jev.POLICY,jev.RUBRIC])))


@engine.serialized
def freeze(job_id, version, ids, expected):
    snap = snapshot(job_id, version, ids)
    fp = fingerprint(snap)
    if fp != expected:
        raise HTTPException(409, 'Đầu vào đã đổi; preview lại. Chưa tạo tác vụ hoặc gọi provider.')
    existing = db.one('SELECT id FROM inbound_review_sets WHERE fingerprint=?', (fp,))
    if existing:
        return detail(existing['id'])
    rid = str(uuid.uuid4())
    db.execute('INSERT INTO inbound_review_sets VALUES(?,?,?,?,?,?,?)',
               (rid, job_id, fp, db.dumps(pipeline.freeze(snap)), db.now(), None, None))
    return detail(rid)


def input_current(r, snap):
    try:
        return fingerprint(snapshot(r['job_id'], snap['config_version'], list(snap['group']))) == r['fingerprint']
    except (HTTPException, IntegrationError):
        return False


@engine.serialized
def start(rid, confirmed, continuation=False):
    r = row(rid)
    if not confirmed:
        raise HTTPException(422, 'Cần xác nhận gửi text CV và phí Jev.')
    snap = stored_snapshot(r)
    if fingerprint(snap)!=r['fingerprint']:
        raise HTTPException(409,'Snapshot không khớp hash đã đóng băng.')
    if not versions_current(snap):
        raise HTTPException(409, 'Phiên bản xử lý đã đổi. Tạo tập mới, giữ lịch sử cũ.')
    if r['search_id']:
        s = db.one('SELECT * FROM searches WHERE id=?', (r['search_id'],))
        if not continuation or s['status'] in ('PENDING', 'RUNNING', 'COMPLETED'):
            return {'search_id': s['id']}
        if db.one("SELECT id FROM jev_steps WHERE assessment_id=? AND status IN ('UNCERTAIN','RUNNING','FATAL') LIMIT 1", (s['id'],)):
            raise HTTPException(409, 'Có request chưa rõ kết quả/phí hoặc lỗi key/schema/model; cần kiểm tra, không tự gửi lại.')
    else:
        if not input_current(r, snap):
            raise HTTPException(409, 'JD/config/CV đã đổi từ lúc preview; tạo tập mới trước khi xác nhận.')
    plan = preflight(snap)
    if not plan['can_assess'] and not continuation:
        raise HTTPException(409, {'message': 'Đầu vào/ngân sách chưa sẵn sàng.', 'preview': plan})
    if continuation:
        from .jev import trial_budget
        if trial_budget()['remaining_estimate_dollars'] <= 0:
            raise HTTPException(409, 'Chờ xác nhận ngân sách. Ledger cũ được giữ nguyên.')
        done={v['candidate_id'] for v in results_for(r)}
        pending={**snap,'jev':{**snap['jev'],'sources':[v for v in snap['jev']['sources'] if v['url'] not in done]}}
        if not pending['jev']['sources'] or not preflight(pending)['can_assess']:
            raise HTTPException(409,'Phần còn thiếu chưa đủ dữ liệu/budget hoặc cần review rubric. Preview lại trước khi tiếp tục.')
    sid = r['search_id'] or str(uuid.uuid4())
    with db.LOCK, db.conn() as c:
        if not r['search_id']:
            full = {**snap, 'inbound_review_set': rid}
            now = db.now()
            c.execute('INSERT INTO searches(id,fingerprint,job_id,retrieval_run_id,config_version,candidate_limit,snapshot,status,provider,model,prompt_version,scorer_version,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                      (sid, r['fingerprint'], r['job_id'], 'inbound-review-set:'+rid, snap['config_version'], len(snap['group']), db.dumps(pipeline.freeze(full)), 'PENDING', 'typesafe', jev.MODEL, internal_jev.VERSION, assessment_scoring.VERSION, now, now))
            c.execute('UPDATE inbound_review_sets SET search_id=? WHERE id=?', (sid, rid))
        else:
            c.execute("UPDATE searches SET status='PENDING',error=NULL,updated=? WHERE id=?", (db.now(), sid))
        # One transaction for the command and queue insertion, no duplicate worker.
        payload = db.dumps({'search_id': sid})
        if not c.execute("SELECT id FROM tasks WHERE kind='batch_assessment' AND payload=? AND status IN ('PENDING','RUNNING')", (payload,)).fetchone():
            c.execute('INSERT INTO tasks(kind,payload,status,created,updated) VALUES(?,?,?,?,?)', ('batch_assessment', payload, 'PENDING', db.now(), db.now()))
    return {'search_id': sid}


def results_for(r):
    if not r['search_id']:
        return []
    from urllib.parse import urlsplit
    snap=stored_snapshot(r)
    items=[json.loads(v['data']) for v in db.rows('SELECT data FROM search_assessments WHERE search_id=?', (r['search_id'],))]
    for item in items:
        item['source_urls']=list(dict.fromkeys(s['url'] for s in snap['group'][item['candidate_id']]['sources']
            if s.get('url') and urlsplit(s['url']).scheme in ('http','https') and urlsplit(s['url']).hostname and not urlsplit(s['url']).username))
    return assessment_scoring.ordered(items)


def source_states(r, snap):
    from .jev_planner import atomic_criteria
    _,layout=atomic_criteria(snap['jev']['criteria'])
    needs_rubric=[cid for cid,v in layout.items() if v.get('requires_review')]
    scored = {v['candidate_id'] for v in results_for(r)}
    s = db.one('SELECT status,error FROM searches WHERE id=?', (r['search_id'],)) if r['search_id'] else None
    report = db.setting('internal_jev_report:' + str(r['search_id']), {})
    states = []
    for cid, item in snap['group'].items():
        steps = db.rows('SELECT status,error FROM jev_steps WHERE assessment_id=? AND source_id=?', (r['search_id'], f"PUB{item['be_rank']:03d}")) if s else []
        reason = report.get('source_errors', {}).get(cid)
        if cid in snap['jev']['missing']:
            status, reason = 'MISSING_TEXT', snap['jev']['missing'][cid]
        elif cid in scored:
            status, reason = 'SCORED', None
        elif needs_rubric:
            status, reason = 'RUBRIC_REVIEW', 'Cần review rubric đã duyệt: '+', '.join(needs_rubric)
        elif any(v['status'] in ('UNCERTAIN', 'RUNNING') for v in steps) and (not s or s['status'] != 'RUNNING'):
            status, reason = 'UNCERTAIN', 'Chưa rõ kết quả/phí; không tự gửi lại.'
        elif s and s['status'] in ('PENDING', 'RUNNING'):
            status = s['status']
        elif reason and ('ngân sách' in reason.lower() or 'budget' in reason.lower()):
            status = 'WAITING_BUDGET'
        elif s:
            status, reason = 'ERROR', reason or s['error'] or 'Chưa hoàn tất; kiểm tra trace.'
        else:
            status = 'NOT_STARTED'
        states.append({'candidate_id': cid, 'title': item['profile']['title'], 'status': status, 'reason': reason})
    return states


def detail(rid):
    r = row(rid)
    snap = stored_snapshot(r)
    states = source_states(r, snap)
    return {'id': rid, 'job_id': r['job_id'], 'config_version': snap['config_version'],
            'created': r['created'], 'search_id': r['search_id'], 'fingerprint': r['fingerprint'],
            'input_current': input_current(r, snap), 'revealed': bool(r['revealed']),
            'selected_count': len(states), 'text_count': len(snap['jev']['sources']),
            'scored_count': sum(s['status'] == 'SCORED' for s in states), 'candidates': states,
            'criteria': snap['jev']['criteria'], 'model': snap['jev']['model'],
            'versions': {'adapter': snap['jev']['adapter_version'], 'review': VERSION, 'scorer': snap['scorer_version']},
            'usage': db.setting('internal_jev_report:' + str(r['search_id']), {}).get('usage'),
            'trial_budget': jev.trial_budget()}


async def assess(s):
    """Persist each completed CV immediately, then continue only missing steps."""
    sid = s['id']
    snap = stored_snapshot(s)
    r = row(snap['inbound_review_set'])
    if r['search_id'] != sid:
        raise IntegrationError('Sai liên kết tập kiểm chứng.')
    frozen={k:v for k,v in snap.items() if k!='inbound_review_set'}
    if fingerprint(frozen)!=r['fingerprint']:
        raise IntegrationError('Snapshot sai hash; không gọi provider.')
    if not versions_current(snap) or s['scorer_version'] != assessment_scoring.VERSION or s['model']!=jev.MODEL:
        raise IntegrationError('Adapter/scorer đã đổi; giữ lịch sử và tạo tập mới.')
    db.execute("UPDATE searches SET status='RUNNING',attempts=attempts+1,updated=? WHERE id=?", (db.now(), sid))
    known = {v['candidate_id']: v for v in results_for(r)}
    remaining = [source for source in snap['jev']['sources'] if source['url'] not in known]
    previous = db.setting('internal_jev_report:' + sid, {})

    def progress(report):
        mapped = internal_jev.remap(copy.deepcopy(report), snap)
        merged = {**known, **{v['candidate_id']: v for v in mapped['results']}}
        for item in merged.values():
            item.pop('be_score',None);item.pop('be_retrieval_score',None)
        final = {**mapped, 'results': assessment_scoring.ordered(list(merged.values()))}
        # Results and persisted progress commit together; no terminal overwrite.
        with db.LOCK, db.conn() as c:
            for item in final['results']:
                c.execute('INSERT OR REPLACE INTO search_assessments VALUES(?,?,?)', (sid, item['candidate_id'], db.dumps(item)))
                run=c.execute('SELECT result_run_id FROM searches WHERE id=?',(sid,)).fetchone()
                if run and run['result_run_id']:
                    c.execute('INSERT OR REPLACE INTO evaluations VALUES(?,?,?,?,?)',(run['result_run_id'],item['candidate_id'],db.dumps(item),'COMPLETED',None))
            c.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', ('internal_jev_report:' + sid, db.dumps(final)))
        return final

    report = await jev.judge(sid, {**snap['jev'], 'sources': remaining}, on_progress=progress) if remaining else previous
    final = progress(report) if remaining else previous
    count = len(results_for(r))
    status = 'COMPLETED' if count == len(snap['group']) else 'PARTIAL'
    db.execute('UPDATE searches SET status=?,usage=?,error=?,updated=? WHERE id=?',
               (status, db.dumps(final.get('usage', {})), None if status == 'COMPLETED' else 'Còn CV chưa hoàn tất; xem lý do từng hồ sơ.', db.now(), sid))
    result_run=db.one('SELECT result_run_id FROM searches WHERE id=?',(sid,))
    if result_run['result_run_id']:
        db.execute('UPDATE runs SET status=?,updated=? WHERE id=?',(status,db.now(),result_run['result_run_id']))


def labels(rid):
    latest = {}
    for v in db.rows('SELECT * FROM inbound_review_labels WHERE set_id=? ORDER BY version', (rid,)):
        latest[v['candidate_id']] = {**json.loads(v['data']), 'version': v['version'], 'phase': v['phase']}
    return latest


class Selection(Strict):
    config_version: int = Field(ge=1)
    candidate_ids: list[str] = Field(min_length=1, max_length=20)


class Freeze(Selection):
    fingerprint: str


class Confirm(Strict):
    confirmed: bool


class Label(Strict):
    candidate_id: str
    version: int = Field(ge=0)
    decision: Literal['CONTACT', 'VERIFY', 'NOT_FIT']
    reason: str = Field(min_length=1, max_length=3000)
    criterion_ids: list[str] = Field(default_factory=list, max_length=100)


@router.get('/inbound-candidates')
def candidates():
    return catalog()


@router.post('/jobs/{job_id}/inbound-review-sets/preview')
def preview(job_id: str, body: Selection):
    return preflight(snapshot(job_id, body.config_version, body.candidate_ids))


@router.post('/jobs/{job_id}/inbound-review-sets', status_code=201)
def create(job_id: str, body: Freeze):
    return freeze(job_id, body.config_version, body.candidate_ids, body.fingerprint)


@router.get('/jobs/{job_id}/inbound-review-sets')
def history(job_id: str):
    local()
    return db.rows('SELECT id,job_id,created,search_id,revealed FROM inbound_review_sets WHERE job_id=? ORDER BY created DESC', (job_id,))


@router.get('/inbound-review-sets/{rid}')
def get(rid: str):
    return detail(rid)


@router.post('/inbound-review-sets/{rid}/assess', status_code=202)
def run(rid: str, body: Confirm):
    return start(rid, body.confirmed)


@router.post('/inbound-review-sets/{rid}/continue', status_code=202)
def resume(rid: str, body: Confirm):
    return start(rid, body.confirmed, continuation=True)


@router.get('/inbound-review-sets/{rid}/continuation-preview')
def continuation_preview(rid:str):
    r=row(rid);snap=stored_snapshot(r);done={v['candidate_id'] for v in results_for(r)}
    return preflight({**snap,'jev':{**snap['jev'],'sources':[v for v in snap['jev']['sources'] if v['url'] not in done]}})


@router.get('/inbound-review-sets/{rid}/sources/{cid}')
def source(rid: str, cid: str):
    snap = stored_snapshot(row(rid))
    if cid not in snap['group']:
        raise HTTPException(404, 'CV không thuộc tập.')
    found = next((s for s in snap['jev']['sources'] if s['url'] == cid), None)
    return {'candidate_id': cid, 'text': found['raw_text'] if found else '', 'sources': snap['group'][cid]['sources'],
            'evidence_catalog': found['evidence'] if found else {}, 'provenance': found['provenance'] if found else {}}


@router.get('/inbound-review-sets/{rid}/review')
def review(rid: str):
    r = row(rid)
    snap = stored_snapshot(r)
    return {'blind': not bool(r['revealed']), 'labels': labels(rid),
            'items': [{'candidate_id': cid, 'title': v['profile']['title']} for cid, v in sorted(snap['group'].items(), key=lambda item: engine.digest(rid + item[0]))]}


@router.post('/inbound-review-sets/{rid}/review')
@engine.serialized
def save_label(rid: str, body: Label):
    r = row(rid)
    snap = stored_snapshot(r)
    if body.candidate_id not in snap['group'] or not set(body.criterion_ids) <= {c['id'] for c in snap['jev']['criteria']}:
        raise HTTPException(422, 'Sai CV/tiêu chí trong tập review.')
    old = labels(rid).get(body.candidate_id, {})
    if old.get('version', 0) != body.version:
        raise HTTPException(409, 'Nhãn đã đổi ở tab khác; tải lại, giữ nháp để đối chiếu.')
    db.execute('INSERT INTO inbound_review_labels VALUES(?,?,?,?,?,?,?)',
               (str(uuid.uuid4()), rid, body.candidate_id, body.version + 1, 'AFTER' if r['revealed'] else 'BEFORE', db.dumps(body.model_dump()), db.now()))
    return review(rid)


@router.post('/inbound-review-sets/{rid}/review/reveal')
@engine.serialized
def reveal(rid: str):
    r = row(rid)
    snap = stored_snapshot(r)
    if set(labels(rid)) != set(snap['group']):
        raise HTTPException(409, 'Review đủ tập trước khi mở điểm/thứ hạng.')
    if not r['search_id'] or not results_for(r):
        raise HTTPException(409,'Chưa có kết quả AI đã validate để đối chiếu. Nhãn độc lập vẫn được giữ.')
    if not r['revealed']:
        with db.LOCK,db.conn() as c:
            c.execute('UPDATE inbound_review_sets SET revealed=? WHERE id=?', (db.now(), rid))
            if r['search_id']:
                run_id=str(uuid.uuid4());now=db.now()
                saved_run={'inbound_review_set':rid,'assessment_engine':VERSION,'scope':'inbound-review-set',
                           'config':snap['config'],'job':{**snap['job'],'sources':[]},'candidates':snap['group']}
                state=c.execute('SELECT status FROM searches WHERE id=?',(r['search_id'],)).fetchone()['status']
                c.execute('INSERT INTO runs(id,job_id,status,config_version,snapshot,model,prompt_version,created,updated,provider) VALUES(?,?,?,?,?,?,?,?,?,?)',
                          (run_id,r['job_id'],state,snap['config_version'],db.dumps(saved_run),snap['jev']['model'],VERSION,now,now,'typesafe'))
                c.execute('UPDATE searches SET result_run_id=? WHERE id=?',(run_id,r['search_id']))
                for item in results_for(r):
                    c.execute('INSERT INTO evaluations VALUES(?,?,?,?,?)',(run_id,item['candidate_id'],db.dumps(item),'COMPLETED',None))
    return detail(rid)


@router.get('/inbound-review-sets/{rid}/results')
def results(rid: str, limit: int = 10):
    r = row(rid)
    if not r['revealed']:
        raise HTTPException(409, 'Điểm/thứ hạng ẩn đến khi hoàn tất review mù.')
    all_rows = results_for(r)
    before = {}
    for l in db.rows("SELECT candidate_id,data FROM inbound_review_labels WHERE set_id=? AND phase='BEFORE' ORDER BY version", (rid,)):
        before[l['candidate_id']] = json.loads(l['data'])
    top = {v['candidate_id'] for v in all_rows[:10]}
    return {'results': all_rows[:max(1, min(limit, 20))], 'total_scored': len(all_rows),
            'metrics': {'reviewed': len(before), 'sample_size': len(json.loads(r['snapshot'])['group']),
                        'top10_contact': sum(cid in top and l['decision'] == 'CONTACT' for cid, l in before.items()),
                        'outside_top10_contact': sum(cid in {v['candidate_id'] for v in all_rows} and cid not in top and l['decision'] == 'CONTACT' for cid, l in before.items()),
                        'unscored_contact':sum(cid not in {v['candidate_id'] for v in all_rows} and l['decision']=='CONTACT' for cid,l in before.items()),
                        'quality_accepted': False, 'note': 'Nhãn vòng đầu trong tập đã chọn; không đo recall toàn kho.'}}


@router.get('/inbound-review-sets/{rid}/export')
def export(rid: str):
    r = row(rid)
    if not r['revealed']:
        raise HTTPException(409, 'Hoàn tất review mù trước khi xuất kết quả AI.')
    value = {'manifest': detail(rid), 'snapshot': stored_snapshot(r), 'result': results(rid, 20),
             'label_history': db.rows('SELECT candidate_id,version,phase,data,created FROM inbound_review_labels WHERE set_id=? ORDER BY created', (rid,)),
             'contains_cv_data': True}
    return Response(db.dumps(pipeline.safe_export(value)), media_type='application/json',
                    headers={'Content-Disposition': f'attachment; filename="inbound-{rid}.json"'})
