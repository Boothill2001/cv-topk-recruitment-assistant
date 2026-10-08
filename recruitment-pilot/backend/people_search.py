"""Job-scoped retrieval of public people documents; never candidate enrichment."""
import json,re,uuid
from datetime import datetime,timedelta,timezone
from . import db,engine,exa_search
from .errors import IntegrationError
from .models import ExaQueries

def public_sources(job_id):
    return [s for s in engine.sources(job_id,'job') if s['group']=='job_public']

def validate_query(query,job_id=None):
    if not 3<=len(query.strip())<=1500:raise IntegrationError('Truy vấn Exa cần từ 3 đến 1.500 ký tự.')
    checked=re.sub(r'(?i)(?:budget[- ](?:aware|constrained)|(?:advertising|marketing|campaign|ad|ads|token|tokens|compute|memory|latency|cost)\s+budget(?:s)?(?:\s+(?:management|optimization|allocation))?|budget\s+(?:management|optimization|allocation))','professional resource planning',query)
    if re.search(r'\S+@\S+|(?:\+?\d[\s().-]*){8,}|(?:\$|USD|VND|salary|budget|lương|ngân sách)',checked,re.I):
        raise IntegrationError('Truy vấn Exa không được chứa liên hệ, lương hoặc ngân sách.')
    if job_id is None:return
    words=lambda s:re.findall(r'\w+',s.lower())
    query_text=' '.join(words(query));public=' '.join(words(' '.join(s['text'] for s in public_sources(job_id))))
    for source in engine.sources(job_id,'job'):
        if source['group']=='job_public':continue
        tokens=words(source['text'])
        for i in range(max(0,len(tokens)-3)):
            phrase=' '.join(tokens[i:i+4])
            if phrase in query_text and phrase not in public:
                raise IntegrationError('Truy vấn có đoạn trùng private note; sửa thành yêu cầu chuyên môn công khai trước khi gửi Exa.')


def must_block(data,job_id=None):
    expected={c['id'] for c in data['criteria'] if c['enabled'] and c['type']=='MUST'}
    clauses=data.get('public_musts',[])
    ids=[c['criterion_id'] for c in clauses]
    if len(ids)!=len(set(ids)) or set(ids)!=expected:
        raise IntegrationError('Hướng tìm cũ chưa có đầy đủ MUST công khai. Soạn truy vấn, kiểm tra và duyệt lại.')
    for clause in clauses:validate_query(clause['query'],job_id)
    return 'Required professional experience: '+'; '.join(c['query'].strip() for c in clauses)+'. ' if clauses else ''

def composed_query(data,strategy,scope,job_id=None):
    from .location_scope import effective_query
    query=must_block(data,job_id)+strategy['exa_query'].strip()
    final=effective_query(query,scope)
    validate_query(final,job_id)
    return query,final

async def draft_queries(job_id,version,guidance=None):
    config,_=engine.checked_config(job_id,version)
    if not config['criteria_approved']:raise IntegrationError('Xác nhận criteria trước khi soạn truy vấn.')
    if config['strategy_hash']!=engine.criteria_hash(json.loads(config['data'])):
        raise IntegrationError('Criteria đã đổi; sinh lại strategy trước khi soạn truy vấn Exa.')
    data=json.loads(config['data']);strategies=data['strategies']
    if not strategies:raise IntegrationError('Chưa có strategy để soạn truy vấn.')
    expected={s['id'] for s in strategies}
    def validate(result):
        ids=[q['strategy_id'] for q in result['queries']]
        if len(ids)!=len(expected) or set(ids)!=expected:raise IntegrationError('AI trả sai tập strategy IDs.')
        for q in result['queries']:validate_query(q['exa_query'],job_id)
        planned={**data,'public_musts':result['public_musts']}
        must_block(planned,job_id)
        for q in result['queries']:
            for scope in ('VIETNAM','INTERNATIONAL','ANY'):composed_query(planned,q,scope,job_id)
    result=await engine.ai('exa_queries',{'public_jd':public_sources(job_id),
        **({'recruiter_guidance':guidance['content']} if guidance else {}),
        'criteria':data['criteria'],'strategies':[{'id':s['id'],'name':s['name'],'description':s['description']} for s in strategies]},ExaQueries,validate,
        **({'prompt_override':guidance['prompt']} if guidance else {}))
    queries={q['strategy_id']:q['exa_query'].strip() for q in result['queries']}
    with db.LOCK:
        engine.checked_config(job_id,version)
        for s in strategies:s['exa_query']=queries[s['id']]
        data['public_musts']=result['public_musts']
        if guidance:
            from .scouting_guidance import metadata
            data['scouting_guidance']=metadata(guidance)
        db.execute('UPDATE configs SET version=version+1,data=?,approved=0,updated=? WHERE job_id=?',(db.dumps(data),db.now(),job_id))
        engine.invalidate('job',job_id,False)

@engine.serialized
def create(job_id,version,ids,refresh=False,location_scope=None,search_type='auto'):
    if search_type not in ('auto','deep'):raise IntegrationError('Chọn chế độ tìm kiếm Auto hoặc Deep.')
    c,p=engine.checked_config(job_id,version);data=json.loads(c['data'])
    if not c['approved'] or not c['criteria_approved'] or c['strategy_hash']!=engine.criteria_hash(data):
        raise IntegrationError('Lưu và duyệt hướng tìm kiếm trước khi gửi Exa.')
    if not ids or len(ids)!=len(set(ids)):raise IntegrationError('Chọn strategy, không gửi ID trùng.')
    strategies={s['id']:s for s in data['strategies']}
    for sid in ids:
        s=strategies.get(sid)
        if not s or not s['enabled']:raise IntegrationError('Strategy không tồn tại hoặc đã tắt.')
        validate_query(s.get('exa_query',''),job_id)
    from .location_scope import effective_query
    chosen=[]
    for sid in sorted(ids):
        query,final=composed_query(data,strategies[sid],location_scope,job_id)
        chosen.append({**strategies[sid],'composed_query':query,'effective_query':final})
    for s in chosen:validate_query(s['effective_query'],job_id)
    snapshot={'job_id':job_id,'job_revision':p['revision'],'job':json.loads(p['data']),
        'config_version':version,'config':data,'scouting_guidance':data.get('scouting_guidance'),'strategies':chosen,'location_scope':location_scope,'search_type':search_type,'adapter_version':exa_search.VERSION}
    fingerprint=engine.digest(db.dumps(snapshot));cutoff=(datetime.now(timezone.utc)-timedelta(hours=24)).isoformat()
    with db.LOCK,db.conn() as conn:
        old=conn.execute('SELECT id FROM people_searches WHERE fingerprint=? AND created>=? ORDER BY created DESC LIMIT 1',(fingerprint,cutoff)).fetchone()
        if old and not refresh:return detail(old['id'])
        gid=str(uuid.uuid4())
        conn.execute('INSERT INTO people_searches VALUES(?,?,?,?,?)',(gid,job_id,fingerprint,db.dumps(snapshot),db.now()))
        for s in chosen:
            sid=exa_search.ensure_search(conn,s['composed_query'],refresh,'people',location_scope,search_type)
            conn.execute('INSERT INTO people_search_items VALUES(?,?,?)',(gid,s['id'],sid))
    return detail(gid)

def detail(gid):
    row=db.one('SELECT * FROM people_searches WHERE id=?',(gid,))
    if not row:raise KeyError(gid)
    snap=json.loads(row.pop('snapshot'));items=[];sources={};costs={}
    for link in db.rows('SELECT * FROM people_search_items WHERE group_id=? ORDER BY strategy_id',(gid,)):
        s=next(s for s in snap['strategies'] if s['id']==link['strategy_id']);search=exa_search.detail(link['search_id'])
        items.append({'strategy':s,'search':search});costs[search['id']]=search
        for source in search.get('results',[]):
            key=source['url'].split('#',1)[0]
            if key not in sources:sources[key]={**source,'strategy_ids':[],'search_ids':[],'retrieved_at':search.get('retrieved_at')}
            sources[key]['strategy_ids'].append(s['id']);sources[key]['search_ids'].append(search['id'])
    states=[i['search']['status'] for i in items]
    status='RUNNING' if any(s in ('PENDING','RUNNING') for s in states) else 'COMPLETED' if all(s=='COMPLETED' for s in states) else 'PARTIAL' if 'COMPLETED' in states else 'FAILED'
    c=db.one('SELECT version,source_revision FROM configs WHERE job_id=?',(row['job_id'],))
    p=db.one("SELECT revision,status FROM profiles WHERE id=? AND kind='job'",(row['job_id'],))
    current=bool(c and p and p['status']=='READY' and c['version']==snap['config_version'] and p['revision']==snap['job_revision'])
    return {**row,'snapshot':snap,'items':items,'results':list(sources.values()),'status':status,'is_current':current,
        'known_cost_dollars':sum(s['known_cost_dollars'] for s in costs.values()),
        'has_unreported_cost':any(s['has_unreported_cost'] for s in costs.values())}
