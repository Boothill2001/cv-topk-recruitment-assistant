import asyncio
import hashlib
from . import fastjson as json
import re
import uuid
from functools import wraps
from . import db
from .integrations import Drive, ai, IntegrationError, redact_error
from .models import Profile, CriteriaDraft, StrategyPlan, Evaluation, Comparison
from .prompts import VERSION,PROMPTS
from . import retrieval

WEIGHTS={'MUST':3,'NICE':1,'OPTIONAL':.5}
POINTS={'MET':100,'PARTIAL':50,'NOT_MET':0}

def identity(name,group):
    pattern=r'(?i)(?<![a-z0-9])CV\s*[-_]?\s*(\d+)(?!\d)' if group.startswith('candidate') else r'(?i)(?<![a-z0-9])IT\s*[-_]?\s*(\d+[a-z]?)(?![a-z0-9])'
    m=re.search(pattern,name)
    if not m:return None
    return ('CV'+m[1]) if group.startswith('candidate') else ('IT-'+m[1].lower())

def digest(text): return hashlib.sha256(text.encode()).hexdigest()
def normalize(s): return re.sub(r'\s+',' ',s).strip()

class EvidenceError(IntegrationError):
    def __init__(self,feedback):
        super().__init__('EVIDENCE_INVALID: quote không tồn tại trong nguồn đã gửi.')
        self.feedback=feedback

def serialized(fn):
    @wraps(fn)
    def guarded(*args,**kwargs):
        with db.LOCK:return fn(*args,**kwargs)
    return guarded

def validate_evidence(items,sources):
    lookup={s['source_id']:s['text'] for s in sources}
    for e in items:
        source=lookup.get(e['source_id'])
        if source is None:raise EvidenceError(e)
        # PDF extraction often joins words or inserts line breaks. Accept ONLY
        # whitespace differences, then replace the quote with the exact source span.
        indices=[i for i,c in enumerate(source) if not c.isspace()]
        compact=''.join(source[i] for i in indices)
        quote=''.join(c for c in e['quote'] if not c.isspace())
        record=next(s for s in sources if s['source_id']==e['source_id'])
        if 'quotes' in record and not any(quote in ''.join(c for c in q if not c.isspace()) for q in record['quotes']):
            raise EvidenceError(e) # Never accept a fabricated span crossing two stored excerpts.
        start=compact.find(quote)
        if len(quote)<2 or start<0:raise EvidenceError(e)
        e['quote']=source[indices[start]:indices[start+len(quote)-1]+1]

def sources(entity,kind):
    rows=db.rows('SELECT * FROM files WHERE entity_id=? AND group_name LIKE ? AND available=1 AND status=? ORDER BY id',
                 (entity,kind+'%','READY'))
    return [{'source_id':r['id'],'name':r['name'],'url':r['url'],'modified':r['modified'],
             'revision':r['revision'],'group':r['group_name'],'text':r['text'],
             'extraction':db.setting('extraction:'+r['id'],{'method':'text extraction','review_required':False})} for r in rows]

def compact_sources(entity,kind,profile=None,metadata=None):
    """Read metadata and already-validated quotes, never original PDF or full source text."""
    data=profile if profile is not None else json.loads(db.one('SELECT data FROM profiles WHERE id=? AND kind=?',(entity,kind))['data'])
    quotes={}
    for item in data.get('facts',[])+data.get('signals',[]):
        for e in item.get('evidence',[]):quotes.setdefault(e['source_id'],[]).append(e['quote'])
    if metadata is None:metadata=db.rows('SELECT id,name,url,modified,revision,group_name FROM files WHERE entity_id=? AND group_name LIKE ? AND available=1 AND status=? ORDER BY id',(entity,kind+'%','READY'))
    return [{'source_id':s['id'],'name':s['name'],'url':s['url'],'modified':s['modified'],'revision':s['revision'],
             'group':s['group_name'],'text':'\n'.join(dict.fromkeys(quotes.get(s['id'],[]))),
             'quotes':list(dict.fromkeys(quotes.get(s['id'],[])))} for s in metadata]

def persist_facts(eid,kind,revision,profile,fingerprint):
    with db.LOCK,db.conn() as c:
        c.execute('INSERT OR IGNORE INTO profile_revisions VALUES(?,?,?,?,?,?)',(eid,kind,revision,db.dumps(profile),fingerprint,db.now()))
        if kind!='candidate':return
        c.execute('DELETE FROM candidate_facts WHERE candidate_id=?',(eid,))
        for f in profile.get('facts',[]):
            c.execute('INSERT INTO candidate_facts VALUES(?,?,?,?,?,?,?,?)',(eid,f['id'],f['field'],retrieval.canonical(f['value']),f.get('numeric_value'),f.get('polarity','POSITIVE'),db.dumps(f['evidence']),revision))

def backfill_preview():
    pending=[{'id':p['id'],'kind':p['kind']} for p in db.rows("SELECT * FROM profiles WHERE status IN ('READY','NEEDS_BACKFILL','FAILED')")
             if json.loads(p['data']).get('_meta',{}).get('schema_version')!=2]
    return {'profiles':pending,'extraction_calls':len(pending),'candidate_calls':sum(p['kind']=='candidate' for p in pending),
            'criteria_calls':sum(p['kind']=='job' for p in pending),'note':'Strategies chỉ sinh sau khi duyệt criteria; retry có thể tăng calls.'}

async def backfill():
    items=backfill_preview()['profiles'];errors=[]
    # Separate sync worker keeps criteria/strategies/comparison queue responsive.
    pending=list(items)
    async def consumer():
        while pending:
            p=pending.pop(0)
            try:await normalize_profile(p['kind'],p['id'])
            except Exception as e:
                db.execute('UPDATE profiles SET status=? WHERE id=? AND kind=?',('FAILED',p['id'],p['kind']))
                errors.append({'id':p['id'],'error':redact_error(e)})
    await asyncio.gather(consumer(),consumer())
    db.set_setting('backfill_last',{'at':db.now(),'profiles':len(items),'errors':errors})
    if errors:raise IntegrationError('Backfill có '+str(len(errors))+' hồ sơ lỗi; xem trạng thái và thử lại.')

def calculate(config,evaluation):
    return retrieval.calculate(config,evaluation)

def enqueue(kind,payload):
    # Single SQLite transaction: concurrent clicks cannot enqueue duplicate work.
    serialized=db.dumps(payload)
    with db.LOCK,db.conn() as c:
        old=c.execute("SELECT id FROM tasks WHERE kind=? AND payload=? AND status IN ('PENDING','RUNNING')",(kind,serialized)).fetchone()
        if old:return old['id']
        return c.execute('INSERT INTO tasks(kind,payload,status,created,updated) VALUES(?,?,?,?,?)',
                         (kind,serialized,'PENDING',db.now(),db.now())).lastrowid

def invalidate(kind,entity,reset_criteria=True):
    if kind=='job':
        db.execute('UPDATE configs SET approved=0 WHERE job_id=?',(entity,))
        if reset_criteria:db.execute('UPDATE configs SET criteria_approved=0,strategy_hash=NULL WHERE job_id=?',(entity,))
        db.execute("UPDATE runs SET status='STALE',updated=? WHERE job_id=? AND status IN ('COMPLETED','PARTIAL')",(db.now(),entity))
    else:
        for r in db.rows("SELECT id,snapshot FROM runs WHERE status IN ('COMPLETED','PARTIAL')"):
            if entity in json.loads(r['snapshot'])['candidates']:
                db.execute("UPDATE runs SET status='STALE',updated=? WHERE id=?",(db.now(),r['id']))

async def ingest(inventory,read_text,authoritative=False):
    seen=[]; touched=set();errors=0
    for f in inventory:
        file_id=f['id'];seen.append(file_id);group=f['group'];old=db.one('SELECT * FROM files WHERE id=?',(file_id,))
        entity=identity(f['name'],group) if group!='reference' else None
        override=db.setting('mapping:'+file_id)
        if override: entity=override
        if old and old['modified']==f.get('modifiedTime','') and old['available'] and old['status'] in ('READY','REFERENCE','NEEDS_MAPPING','DUPLICATE') and old['entity_id']==entity and old['group_name']==group:
            continue
        try:
            text=await read_text(f)
            if len(text.strip())<20:raise IntegrationError('NEEDS_OCR: chưa trích xuất được đủ nội dung.')
            h=digest(text)
            changed=not old or old['hash']!=h or old['entity_id']!=entity or old['group_name']!=group or not old['available']
            rev=(old['revision']+1 if changed else old['revision']) if old else 1
            status='REFERENCE' if group=='reference' else ('READY' if entity else 'NEEDS_MAPPING')
            db.execute('INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (file_id,f['name'],f['mimeType'],f.get('webViewLink',''),group,entity,f.get('modifiedTime',''),h,text,status,None,1,rev,db.now()))
            if changed:
                for eid,g in [(entity,group),(old['entity_id'],old['group_name']) if old else (None,None)]:
                    if eid and g!='reference':touched.add(('candidate' if g.startswith('candidate') else 'job',eid))
        except Exception as e:
            errors+=1
            err=redact_error(e);status='NEEDS_OCR' if 'NEEDS_OCR' in err else ('UNSUPPORTED' if 'UNSUPPORTED' in err else 'PARSE_FAILED')
            db.execute('INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (file_id,f['name'],f['mimeType'],f.get('webViewLink',''),group,entity,f.get('modifiedTime',''),None,None,status,err,1,(old['revision']+1 if old else 1),db.now()))
            if entity:touched.add(('candidate' if group.startswith('candidate') else 'job',entity))
        db.set_setting('sync_progress',{'processed':len(seen),'total':len(inventory),'current':f['name']})
    if authoritative:
        for old in db.rows('SELECT * FROM files WHERE available=1'):
            if old['id'] not in seen and not old['id'].startswith('local:'):
                db.execute("UPDATE files SET available=0,status='UNAVAILABLE',updated=? WHERE id=?",(db.now(),old['id']))
                if old['entity_id']:touched.add(('candidate' if old['group_name'].startswith('candidate') else 'job',old['entity_id']))
    # Multiple public files sharing an ID must be resolved rather than selected arbitrarily.
    for kind in ('candidate','job'):
        for r in db.rows("SELECT entity_id,COUNT(*) n FROM files WHERE group_name=? AND available=1 AND entity_id IS NOT NULL AND status IN ('READY','DUPLICATE') GROUP BY entity_id",(kind+'_public',)):
            db.execute("UPDATE files SET status=? WHERE entity_id=? AND group_name=? AND status IN ('READY','DUPLICATE')",('DUPLICATE' if r['n']>1 else 'READY',r['entity_id'],kind+'_public'))
            if r['n']>1:touched.add((kind,r['entity_id']))
    # A new pool member changes the set a previous exhaustive run covered.
    if any(kind=='candidate' for kind,_ in touched):
        for run in db.rows("SELECT id,snapshot FROM runs WHERE status IN ('COMPLETED','PARTIAL')"):
            if json.loads(run['snapshot']).get('scope')!='single-cv':
                db.execute("UPDATE runs SET status='STALE',updated=? WHERE id=?",(db.now(),run['id']))
    for kind,eid in touched:invalidate(kind,eid)
    db.set_setting('sync_progress',{'processed':len(inventory),'total':len(inventory),'current':'Đang chuẩn hóa hồ sơ và tạo criteria bằng DeepSeek'})
    # Failed profiles are retried on the next successful sync; no reparse for unchanged ready ones.
    # New JD sync must not implicitly backfill unchanged old CVs. Backfill is an explicit task.
    entities=set()
    entities |= {(r['group_name'].split('_')[0],r['entity_id']) for r in db.rows("SELECT group_name,entity_id FROM files WHERE entity_id IS NOT NULL AND available=1 AND group_name!='reference'") if not db.one('SELECT id FROM profiles WHERE id=? AND kind=?',(r['entity_id'],r['group_name'].split('_')[0]))}
    pending=sorted(entities | touched)
    async def normalize_one(kind,eid):
        nonlocal errors
        try:await normalize_profile(kind,eid)
        except Exception as e:
            errors+=1;old=db.one('SELECT revision FROM profiles WHERE id=? AND kind=?',(eid,kind))
            db.execute('INSERT OR REPLACE INTO profiles VALUES(?,?,?,?,?,?,?)',(eid,kind,old['revision'] if old else 1,db.dumps({'error':redact_error(e)}),'','FAILED',db.now()))
    async def consumer():
        while pending:
            kind,eid=pending.pop(0);await normalize_one(kind,eid)
    await asyncio.gather(consumer(),consumer())
    db.set_setting('last_sync',{'at':db.now(),'files':len(seen),'errors':errors,'mode':'oauth' if authoritative else 'connector_snapshot'})
    db.set_setting('sync_progress',None)

async def normalize_profile(kind,eid):
    src=sources(eid,kind)
    public=[s for s in src if s['group']==kind+'_public']
    if len(public)!=1:
        old=db.one('SELECT * FROM profiles WHERE id=? AND kind=?',(eid,kind))
        if old:db.execute('UPDATE profiles SET status=? WHERE id=? AND kind=?',('NEEDS_SOURCE',eid,kind))
        else:db.execute('INSERT INTO profiles VALUES(?,?,?,?,?,?,?)',(eid,kind,1,db.dumps({'summary':'Chưa có public file đọc được; xem file cần kiểm tra.'}),'','NEEDS_SOURCE',db.now()))
        return
    fingerprint=digest(db.dumps([(s['source_id'],s['revision']) for s in src]))
    old=db.one('SELECT * FROM profiles WHERE id=? AND kind=?',(eid,kind))
    if old and old['source_hash']==fingerprint and old['status']=='READY' and json.loads(old['data']).get('_meta',{}).get('schema_version')==2:
        if kind=='job' and not db.one('SELECT job_id FROM configs WHERE job_id=?',(eid,)):
            try:await propose(eid)
            except Exception as e:db.set_setting('config_error:'+eid,redact_error(e))
        return
    def validate(p):
        for signal in p['signals']:validate_evidence(signal['evidence'],src)
        ids=[f['id'] for f in p.get('facts',[])]
        if len(ids)!=len(set(ids)):raise IntegrationError('Fact ID trùng.')
        if kind=='candidate' and not p.get('facts'):raise IntegrationError('Candidate cần facts có evidence; không trả hồ sơ rỗng.')
        for fact in p.get('facts',[]):validate_evidence(fact['evidence'],src)
    profile=await ai('profile',{'kind':kind,'id':eid,'sources':src},Profile,validate)
    fresh=db.rows('SELECT id,revision FROM files WHERE entity_id=? AND group_name LIKE ? AND available=1 AND status=? ORDER BY id',(eid,kind+'%','READY'))
    if digest(db.dumps([(s['id'],s['revision']) for s in fresh]))!=fingerprint:
        raise IntegrationError('Nguồn đổi trong khi chuẩn hóa; không ghi đè bằng kết quả cũ.')
    latest=db.one('SELECT revision FROM profiles WHERE id=? AND kind=?',(eid,kind))
    if latest and latest['revision']!=(old['revision'] if old else None):
        # Human corrections/newer ingestion always win over an in-flight extraction.
        return
    profile['_meta']={'provider':db.setting('provider','deepseek'),'model':db.setting('model','deepseek-flash'),'prompt_version':VERSION,'parsed_at':db.now(),
                      'schema_version':2,'taxonomy_version':'pilot-tags-1','prompt_hash':digest(PROMPTS['profile']),
                      'sources':[{k:s[k] for k in ('source_id','revision','modified')} for s in src]}
    # A saved human correction wins, but is flagged for review after a source change.
    corrections=db.setting('facts_override:'+kind+':'+eid)
    if corrections:
        profile['facts']=corrections['facts'];profile['_meta']['correction_review_required']=corrections['source_hash']!=fingerprint
    rev=old['revision']+1 if old else 1
    state='NEEDS_REVIEW' if profile['_meta'].get('correction_review_required') else 'READY'
    db.execute('INSERT OR REPLACE INTO profiles VALUES(?,?,?,?,?,?,?)',(eid,kind,rev,db.dumps(profile),fingerprint,state,db.now()))
    persist_facts(eid,kind,rev,profile,fingerprint)
    invalidate(kind,eid)
    if kind=='job':
        try:await propose(eid)
        except Exception as e:
            db.set_setting('config_error:'+eid,redact_error(e))

def validate_config(config,require_strategies=True):
    try:retrieval.policy(config)
    except ValueError as e:raise IntegrationError(str(e))
    for k in ('criteria','strategies'):
        ids=[x['id'] for x in config.get(k,[])]
        if len(ids)!=len(set(ids)):raise IntegrationError('ID criteria/strategy trùng.')
    if not any(c['enabled'] for c in config['criteria']):raise IntegrationError('Cần ít nhất một criterion đang bật.')
    if require_strategies and len(config.get('strategies',[]))<2:
        raise IntegrationError('Cần sinh ít nhất hai strategy; có thể tắt tất cả để so sánh baseline.')
    for c in config['criteria']:
        if c.get('rule'):
            try:retrieval.validate_rule(c['rule'])
            except ValueError as e:raise IntegrationError(str(e))
            if c['type']=='MUST' and any(p['field']=='titles' for p in c['rule']['predicates']):
                raise IntegrationError('Title không được dùng làm MUST gate; dùng năng lực có evidence.')
    for s in config.get('strategies',[]):
        if s['enabled'] and require_strategies:
            if not s.get('rule'):raise IntegrationError('Strategy '+s['id']+' chưa có rule thực thi; sửa hoặc tắt.')
            try:retrieval.validate_rule(s['rule'])
            except ValueError as e:raise IntegrationError(str(e))
    banned=r'(?i)(\bsalary\b|lương|\bremote\b|\brelocat\w*|giới tính|\bgender\b|\breligion\b|tôn giáo|\brace\b|chủng tộc|\btuổi\b|\bage\b|\bdisability\b|khuyết tật|\bsexual\b|\bnationality\b|quốc tịch|\bmotivat\w*|động lực)'
    if any(re.search(banned,c['name']+' '+c['description']) for c in config['criteria']):
        raise IntegrationError('Criteria chỉ dùng năng lực chuyên môn, không salary/remote/động lực/thuộc tính nhạy cảm.')

async def propose(job_id):
    p=db.one("SELECT * FROM profiles WHERE id=? AND kind='job' AND status='READY'",(job_id,))
    if not p:raise IntegrationError('Job chưa có profile hợp lệ.')
    old=db.one('SELECT version FROM configs WHERE job_id=?',(job_id,))
    result=await ai('criteria',{'profile':json.loads(p['data']),'sources':sources(job_id,'job')},CriteriaDraft,lambda d:validate_config(d,False))
    result['strategies']=[]
    result['policy']=retrieval.policy({})
    with db.LOCK:
        fresh=db.one("SELECT revision,status FROM profiles WHERE id=? AND kind='job'",(job_id,))
        latest=db.one('SELECT version FROM configs WHERE job_id=?',(job_id,))
        if not fresh or fresh['status']!='READY' or fresh['revision']!=p['revision'] or latest!=old:
            raise IntegrationError('JD/config đã đổi trong khi AI chạy; không ghi đè bản mới.')
        db.execute('INSERT OR REPLACE INTO configs(job_id,version,data,approved,source_revision,updated,criteria_approved,strategy_hash) VALUES(?,?,?,?,?,?,0,NULL)',(job_id,(old['version']+1 if old else 1),db.dumps(result),0,p['revision'],db.now()))
        invalidate('job',job_id)
    db.set_setting('config_meta:'+job_id,{'provider':db.setting('provider','deepseek'),'model':db.setting('model','deepseek-flash'),'prompt_version':VERSION,'generated_at':db.now()})
    db.set_setting('config_error:'+job_id,None)

def criteria_hash(config):return digest(db.dumps(config['criteria']))

def checked_config(job_id,version):
    c=db.one('SELECT * FROM configs WHERE job_id=?',(job_id,))
    p=db.one("SELECT * FROM profiles WHERE id=? AND kind='job'",(job_id,))
    if not c or c['version']!=version or not p or p['status']!='READY' or p['revision']!=c['source_revision']:
        raise IntegrationError('JD/config đã đổi hoặc chưa hợp lệ. Tải lại.')
    return c,p

async def generate_strategies(job_id,version,count):
    config,p=checked_config(job_id,version)
    if not config['criteria_approved']:raise IntegrationError('Duyệt criteria trước khi sinh strategy.')
    data=json.loads(config['data']);validate_config(data,False)
    def validate(plan):
        if len(plan['strategies'])!=count:raise IntegrationError('AI trả sai số strategy yêu cầu.')
        validate_config({**data,'strategies':plan['strategies']})
        from .people_search import validate_query
        for strategy in plan['strategies']:validate_query(strategy.get('exa_query',''),job_id)
    result=await ai('strategies',{'criteria':data['criteria'],'strategy_count':count,
        'job':{'profile':json.loads(p['data']),'sources':sources(job_id,'job')},'public_jd':[s for s in sources(job_id,'job') if s['group']=='job_public']},StrategyPlan,validate)
    with db.LOCK:
        fresh,_=checked_config(job_id,version)
        if not fresh['criteria_approved']:raise IntegrationError('Criteria cần duyệt lại; không ghi strategy cũ.')
        data['strategies']=result['strategies']
        db.execute('UPDATE configs SET version=version+1,data=?,approved=0,strategy_hash=?,updated=? WHERE job_id=?',
            (db.dumps(data),criteria_hash(data),db.now(),job_id))
        invalidate('job',job_id,False)
    db.set_setting('strategy_meta:'+job_id,{'provider':db.setting('provider','deepseek'),
        'model':db.setting('model'),'prompt_version':VERSION,'generated_at':db.now(),'criteria_version':version})

@serialized
def create_run(job_id,version,candidate_ids=None):
    config=db.one('SELECT * FROM configs WHERE job_id=?',(job_id,))
    single=candidate_ids is not None
    if single and (len(candidate_ids)!=1 or len(set(candidate_ids))!=1):raise IntegrationError('Đánh giá trực tiếp chỉ nhận đúng một CV.')
    if not config or not (config['criteria_approved'] if single else config['approved']) or config['version']!=version:raise IntegrationError('Duyệt đúng phiên bản criteria trước khi chạy.')
    data=json.loads(config['data'])
    if not config['criteria_approved'] or (not single and config['strategy_hash']!=criteria_hash(data)):raise IntegrationError('Criteria/strategy cần duyệt lại.')
    validate_config(data,not single)
    job=db.one("SELECT * FROM profiles WHERE kind='job' AND id=? AND status='READY'",(job_id,))
    if not job or job['revision']!=config['source_revision']:raise IntegrationError('JD đã đổi; cần tạo/duyệt criteria lại.')
    busy=db.one("SELECT id FROM runs WHERE job_id=? AND status IN ('PENDING','RUNNING')",(job_id,))
    if busy and not single:return busy['id']
    pool=db.rows("SELECT * FROM profiles WHERE kind='candidate' AND status='READY'"+(" AND id=?" if single else '')+" ORDER BY id",(candidate_ids[0],) if single else ())
    if not pool:raise IntegrationError('Chưa có candidate profile hợp lệ.')
    pool=[p for p in pool if json.loads(p['data']).get('_meta',{}).get('schema_version')==2]
    if not pool:raise IntegrationError('Cần backfill structured profiles trước khi retrieval.')
    source_metadata={}
    for s in db.rows("SELECT id,name,url,modified,revision,group_name,entity_id FROM files WHERE available=1 AND status='READY' ORDER BY id"):
        source_metadata.setdefault((s['entity_id'],s['group_name'].split('_')[0]),[]).append(s)
    candidates={}
    scoring_profiles={}
    for p in pool:
        profile=json.loads(p['data'])
        scoring_profiles[p['id']]=profile
        summary={key:profile.get(key) for key in ('title','skills','domains','signals','_meta')}
        metadata=[{'source_id':s['id'],'name':s['name'],'url':s['url'],'modified':s['modified'],'revision':s['revision'],'group':s['group_name']} for s in source_metadata.get((p['id'],'candidate'),[])]
        candidates[p['id']]={'profile':summary,'revision':p['revision'],'sources':metadata}
    jp=json.loads(job['data'])
    snapshot={'engine':retrieval.VERSION,'mapping_version':retrieval.MAPPING_VERSION,'job':{'profile':jp,'revision':job['revision'],'sources':compact_sources(job_id,'job',jp,source_metadata.get((job_id,'job'),[]))},
              'config':json.loads(config['data']),'candidates':candidates}
    if single:snapshot['scope']='single-cv'
    for previous in db.rows("SELECT id,snapshot FROM runs WHERE job_id=? AND config_version=? AND status='COMPLETED' ORDER BY created DESC",(job_id,version)):
        prior=json.loads(previous['snapshot'])
        if prior.get('scope')==snapshot.get('scope') and not prior.get('assessment_engine') and prior.get('mapping_version')==retrieval.MAPPING_VERSION and prior.get('engine')==retrieval.VERSION and {k:v['revision'] for k,v in prior['candidates'].items()}=={k:v['revision'] for k,v in candidates.items()} and prior['job']['revision']==job['revision']:
            return previous['id']
    rid=str(uuid.uuid4())
    db.execute('INSERT INTO runs(id,job_id,status,config_version,snapshot,model,prompt_version,error,created,updated,provider) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
        (rid,job_id,'PENDING',version,db.dumps(snapshot),'deterministic',VERSION,None,db.now(),db.now(),'postgres'))
    # Round 1 bypasses the AI task queue; it must never wait behind ingestion.
    match_structured(rid,scoring_profiles)
    return rid

def current(run):
    snap=json.loads(run['snapshot']) if isinstance(run['snapshot'],str) else run['snapshot']
    config=db.one('SELECT * FROM configs WHERE job_id=?',(run['job_id'],))
    job=db.one("SELECT revision,status FROM profiles WHERE kind='job' AND id=?",(run['job_id'],))
    single=snap.get('scope')=='single-cv'
    if not config or not (config['criteria_approved'] if single else config['approved']) or config['version']!=run['config_version'] or not job or job['status']!='READY' or job['revision']!=snap['job']['revision']:return False
    current_pool={p['id']:p['revision'] for p in db.rows("SELECT id,revision FROM profiles WHERE kind='candidate' AND status='READY'")}
    if single:current_pool={eid:current_pool[eid] for eid in snap['candidates'] if eid in current_pool}
    return current_pool=={eid:p['revision'] for eid,p in snap['candidates'].items()}

def match_structured(run_id,profiles=None):
    from time import perf_counter
    started=perf_counter()
    run=db.one('SELECT * FROM runs WHERE id=?',(run_id,));snap=json.loads(run['snapshot'])
    if snap.get('engine')!=retrieval.VERSION:raise IntegrationError('Legacy full-CV matching đã bị vô hiệu hóa.')
    if not current(run):raise IntegrationError('Input đã đổi; tạo run mới.')
    config=snap['config']
    if profiles is None:profiles={p['id']:json.loads(p['data']) for p in db.rows("SELECT id,data FROM profiles WHERE kind='candidate' AND status='READY'")}
    results=[]
    for eid in snap['candidates']:
        result=retrieval.evaluate(config,profiles[eid])
        results.append((run_id,eid,db.dumps(result),'COMPLETED',None))
    with db.LOCK,db.conn() as c:
        c.execute("UPDATE runs SET status='RUNNING' WHERE id=?",(run_id,))
        c.executemany('INSERT INTO evaluations(run_id,candidate_id,data,status,error) VALUES(?,?,?,?,?) ON CONFLICT(run_id,candidate_id) DO UPDATE SET data=excluded.data,status=excluded.status,error=excluded.error',results)
        c.execute('UPDATE runs SET status=?,updated=? WHERE id=?',('COMPLETED' if current(run) else 'STALE',db.now(),run_id))
    db.set_setting('retrieval_metrics:'+run_id,{'seconds':round(perf_counter()-started,4),'llm_calls':0,'pdf_reads':0,'pool':len(snap['candidates']),'scorer_version':retrieval.VERSION})

async def match(run_id):
    match_structured(run_id)

def eligible(data,threshold=None,config=None):
    if not data:return False
    if config and 'policy' in config:
        return retrieval.lane(data,config,threshold)=='QUALIFIED'
    # Historical exports remain readable, but can never run through the new engine.
    return bool(data.get('must_passed',data.get('must_complete',False)) and data.get('score') is not None and data['score']>(85 if threshold is None else threshold))

@serialized
def create_report(run_id,selected,limit,threshold):
    run=db.one('SELECT * FROM runs WHERE id=?',(run_id,))
    if not run or run['status'] not in ('COMPLETED','PARTIAL') or not current(run):raise IntegrationError('Run chưa hoàn tất hoặc đã cũ; chạy lại trước Round 2.')
    if not selected or limit not in (5,10,20) or len(selected)!=len(set(selected)) or len(selected)>limit:raise IntegrationError('Nhóm chọn trùng hoặc vượt giới hạn 5/10/20.')
    for eid in selected:
        e=db.one("SELECT * FROM evaluations WHERE run_id=? AND candidate_id=? AND status='COMPLETED'",(run_id,eid))
        if not e:raise IntegrationError('ID được chọn không có kết quả hợp lệ trong run.')
    signature='comparison_request:'+digest(db.dumps({'run_id':run_id,'selected':sorted(selected),
        'provider':db.setting('provider','deepseek'),'model':db.setting('model','deepseek-flash'),
        'prompt_version':VERSION,'prompt_hash':digest(PROMPTS['comparison'])}))
    existing_id=db.setting(signature)
    existing=db.one('SELECT * FROM reports WHERE id=?',(existing_id,)) if existing_id else None
    if existing:
        if existing['status'] in ('FAILED','INTERRUPTED'):
            db.execute("UPDATE reports SET status='PENDING',error=NULL WHERE id=?",(existing_id,))
        if existing['status']!='COMPLETED':enqueue('compare',{'report_id':existing_id})
        return existing_id
    rid=str(uuid.uuid4())
    with db.conn() as c:
        c.execute('INSERT INTO reports(id,run_id,selected,data,status,error,model,prompt_version,created,provider) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (rid,run_id,db.dumps(selected),None,'PENDING',None,db.setting('model','deepseek-flash'),VERSION,db.now(),db.setting('provider','deepseek')))
        c.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)',(signature,db.dumps(rid)))
    enqueue('compare',{'report_id':rid});return rid

async def compare(report_id):
    report=db.one('SELECT * FROM reports WHERE id=?',(report_id,));run=db.one('SELECT * FROM runs WHERE id=?',(report['run_id'],))
    if not current(run):raise IntegrationError('Input đã đổi, cần chạy matching lại.')
    if report['prompt_version']!=VERSION:raise IntegrationError('Prompt đã đổi, tạo comparison mới.')
    if report['model']!=db.setting('model','deepseek-flash') or report['provider']!=db.setting('provider','deepseek'):
        raise IntegrationError('Provider/model đã đổi, tạo comparison mới.')
    snap=json.loads(run['snapshot']);selected=json.loads(report['selected'])
    if snap.get('engine')!=retrieval.VERSION:raise IntegrationError('Legacy run chỉ dùng tra cứu.')
    for eid in selected:
        p=snap['candidates'][eid]
        revision=db.one("SELECT data FROM profile_revisions WHERE id=? AND kind='candidate' AND revision=?",(eid,p['revision']))
        if not revision:raise IntegrationError('Thiếu profile revision bất biến; không dùng hồ sơ mới thay thế.')
        p['profile']=json.loads(revision['data'])
        p['sources']=compact_sources(eid,'candidate',p['profile'],[{'id':s['source_id'],'name':s['name'],'url':s['url'],'modified':s['modified'],'revision':s['revision'],'group_name':s['group']} for s in p['sources']])
    payload={'job':snap['job'],'criteria':snap['config']['criteria'],'selected':{
        eid:{**snap['candidates'][eid],'evaluation':json.loads(db.one('SELECT data FROM evaluations WHERE run_id=? AND candidate_id=?',(run['id'],eid))['data'])} for eid in selected}}
    if len(db.dumps(payload))>100000:raise IntegrationError('Nhóm vượt context budget pilot (100.000 ký tự); giảm số người. Chưa gửi AI.')
    db.execute("UPDATE reports SET status='RUNNING' WHERE id=?",(report_id,))
    def validate(result):
        items=result['items']
        if sorted(i['candidate_id'] for i in items)!=sorted(selected) or sorted(i['rank'] for i in items)!=list(range(1,len(selected)+1)):
            raise IntegrationError('Round 2 trả sai tập ứng viên hoặc thứ hạng.')
        for item in items:
            p=snap['candidates'][item['candidate_id']];rec=item['recruitability'];combined=p['sources']+snap['job']['sources']
            validate_evidence(item['technical_evidence'],p['sources'])
            validate_evidence(rec['evidence'],combined)
            for s in rec['attractions']+rec['barriers']:validate_evidence(s['evidence'],combined)
            # Conservative evidence gate: no explicit preference signals => no willingness claim.
            direct=[s for s in p['profile']['signals'] if s['topic'] in ('salary','location','remote','availability','motivation','scope')]
            if not direct and (rec['conclusion']!='UNKNOWN' or rec['attractions'] or rec['barriers']):
                raise IntegrationError('Không có tín hiệu trực tiếp; Recruitability phải UNKNOWN.')
            if rec['conclusion']!='UNKNOWN':
                direct_quotes={normalize(e['quote']) for s in direct for e in s['evidence']}
                if not any(e['source_id'] in {s['source_id'] for s in p['sources']} and normalize(e['quote']) in direct_quotes for e in rec['evidence']):
                    raise IntegrationError('Recruitability cần quote sở thích/constraint trực tiếp của ứng viên.')
            if re.search(r'\d\s*%',rec['summary']):raise IntegrationError('Không trả xác suất recruitability.')
    result=await ai('comparison',payload,Comparison,validate)
    db.execute('UPDATE reports SET status=?,data=? WHERE id=?',('COMPLETED' if current(run) else 'STALE',db.dumps(result),report_id))

async def sync():
    drive=Drive()
    inventory=await drive.inventory()
    await ingest(inventory,drive.text,authoritative=True)

async def bootstrap():
    p=db.RUNTIME/'connector-snapshot.json'
    if not p.exists():raise IntegrationError('Chưa có snapshot connector. Kết nối Google rồi đồng bộ.')
    data=json.loads(p.read_text(encoding='utf-8'))
    async def read(f):
        if f.get('fetch_error'):raise IntegrationError(f['fetch_error'])
        return f.get('text','')
    await ingest(data,read,authoritative=False)

async def worker(sync_only=False):
    while True:
        clause="kind IN ('sync','bootstrap','backfill')" if sync_only else "kind NOT IN ('sync','bootstrap','backfill')"
        task=db.one("SELECT * FROM tasks WHERE status='PENDING' AND "+clause+" ORDER BY id LIMIT 1")
        if not task:await asyncio.sleep(1);continue
        db.execute("UPDATE tasks SET status='RUNNING',updated=? WHERE id=?",(db.now(),task['id']))
        payload=json.loads(task['payload'])
        try:
            if task['kind']=='sync':await sync()
            elif task['kind']=='bootstrap':await bootstrap()
            elif task['kind']=='propose':await propose(payload['job_id'])
            elif task['kind']=='normalize':await normalize_profile(payload['kind'],payload['id'])
            elif task['kind']=='backfill':await backfill()
            elif task['kind']=='exa_queries':
                from .people_search import draft_queries
                await draft_queries(payload['job_id'],payload['version'])
            elif task['kind']=='strategies':await generate_strategies(payload['job_id'],payload['version'],payload['count'])
            elif task['kind']=='sheet_export':
                from .sheets import publish
                await publish(payload['export_id'])
            elif task['kind']=='match':await match(payload['run_id'])
            elif task['kind']=='compare':await compare(payload['report_id'])
            elif task['kind']=='batch_assessment':
                from .search_orchestration import assess
                await assess(payload['search_id'])
            elif task['kind']=='single_cv':
                from .single_cv import process
                await process(payload['ticket_id'])
            elif task['kind']=='web_search':
                from .exa_search import run
                await run(payload['search_id'])
                state=db.one('SELECT status,error FROM web_searches WHERE id=?',(payload['search_id'],))
                if state['status']=='FAILED':raise IntegrationError(state['error'])
            elif task['kind']=='public_assessment':
                from .public_assessment import assess
                await assess(payload['assessment_id'])
            else:raise IntegrationError('Tác vụ không hợp lệ.')
            db.execute("UPDATE tasks SET status='COMPLETED',updated=? WHERE id=?",(db.now(),task['id']))
        except Exception as e:
            err=redact_error(e)
            db.execute("UPDATE tasks SET status='FAILED',error=?,updated=? WHERE id=?",(err,db.now(),task['id']))
            if payload.get('run_id'):db.execute("UPDATE runs SET status='FAILED',error=?,updated=? WHERE id=?",(err,db.now(),payload['run_id']))
            if payload.get('report_id'):db.execute("UPDATE reports SET status='FAILED',error=? WHERE id=?",(err,payload['report_id']))
        await asyncio.sleep(.1)

async def scheduler():
    while True:
        if (db.RUNTIME/'google-token.json').exists():enqueue('sync',{})
        await asyncio.sleep(120)
