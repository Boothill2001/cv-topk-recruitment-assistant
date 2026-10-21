"""Immutable typed artifacts; no queue, provider or billing ownership."""
import hashlib,json,uuid
from . import db
from .errors import IntegrationError

VERSION='pipeline-1'
SQL='''CREATE TABLE IF NOT EXISTS pipeline_runs (
 id TEXT PRIMARY KEY, origin_kind TEXT NOT NULL, origin_id TEXT NOT NULL,
 legacy INTEGER NOT NULL, metadata JSONB NOT NULL, created TEXT NOT NULL,
 UNIQUE(origin_kind,origin_id));
CREATE TABLE IF NOT EXISTS artifacts (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, schema_version TEXT NOT NULL,
 content_hash TEXT NOT NULL, payload JSONB, body TEXT, validated INTEGER NOT NULL,
 created TEXT NOT NULL, UNIQUE(kind,schema_version,content_hash,validated));
CREATE TABLE IF NOT EXISTS node_attempts (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES pipeline_runs(id),
 node_id TEXT NOT NULL, attempt_no INTEGER NOT NULL, fingerprint TEXT NOT NULL,
 status TEXT NOT NULL, versions JSONB NOT NULL, usage JSONB NOT NULL,
 error JSONB, started TEXT NOT NULL, finished TEXT,
 UNIQUE(run_id,node_id,attempt_no));
CREATE TABLE IF NOT EXISTS artifact_links (
 attempt_id TEXT NOT NULL REFERENCES node_attempts(id), artifact_id TEXT NOT NULL REFERENCES artifacts(id),
 direction TEXT NOT NULL CHECK(direction IN ('INPUT','OUTPUT')), role TEXT NOT NULL,
 PRIMARY KEY(attempt_id,direction,role));
CREATE INDEX IF NOT EXISTS pipeline_attempt_run ON node_attempts(run_id);
'''

def decode(value):return json.loads(value) if isinstance(value,str) and not db.database_url() else value
def canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
def digest(value):return hashlib.sha256(value.encode('utf-8')).hexdigest()
def wire(value):
    if db.database_url():
        from psycopg.types.json import Jsonb
        return Jsonb(value)
    return canonical(value)

def run(kind,origin,metadata=None,legacy=False):
    with db.LOCK,db.conn() as c:
        rid=str(uuid.uuid5(uuid.NAMESPACE_URL,'pipeline:'+kind+':'+origin))
        c.execute('INSERT INTO pipeline_runs VALUES(?,?,?,?,?,?) ON CONFLICT(origin_kind,origin_id) DO NOTHING',
          (rid,kind,origin,int(legacy),wire(metadata or {}),db.now()))
    return rid

def _artifact(c,kind,payload=None,body=None,validated=True):
    if (payload is None)==(body is None):raise IntegrationError('Artifact cần đúng một payload hoặc text.')
    encoded=body if body is not None else canonical(payload);h=digest(encoded)
    aid=str(uuid.uuid5(uuid.NAMESPACE_URL,canonical([kind,VERSION,h,validated])))
    c.execute('INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING',
      (aid,kind,VERSION,h,wire(payload) if payload is not None else None,body,int(validated),db.now()))
    return aid

def artifact(kind,payload=None,body=None,validated=True):
    with db.LOCK,db.conn() as c:return _artifact(c,kind,payload,body,validated)

def freeze(value):
    """Reference full text once; paragraph catalogs point to exact text offsets."""
    if isinstance(value,list):return [freeze(v) for v in value]
    if not isinstance(value,dict):return value
    full_key=next((k for k in ('full_source_text','raw_text','text') if isinstance(value.get(k),str) and value[k]),None)
    result={}
    if full_key:
        text=value[full_key];aid=artifact('source-text',body=text)
        result[full_key]={'$text':aid}
        for key in ('evidence','professional_paragraphs'):
            if isinstance(value.get(key),dict):
                catalog={}
                for ref,part in value[key].items():
                    offset=value.get('evidence_offsets',{}).get(ref)
                    pos=offset['start'] if offset and isinstance(part,str) else text.find(part) if isinstance(part,str) else -1
                    if offset and (text[pos:offset['end']]!=part or offset['end']!=pos+len(part)):
                        raise IntegrationError('Evidence sai offset khi lưu artifact.')
                    if pos>=0:catalog[ref]={'$slice':aid,'start':pos,'end':pos+len(part)}
                    else:catalog[ref]=freeze(part)
                result[key]=catalog
    def reference(part):
        if isinstance(part,str) and len(part)>40:
            pos=text.find(part)
            if pos>=0:return {'$slice':aid,'start':pos,'end':pos+len(part)}
        if isinstance(part,list):return [reference(v) for v in part]
        if isinstance(part,dict):return {k:reference(v) for k,v in part.items()}
        return part
    for key,part in value.items():
        if key not in result:result[key]=reference(part) if full_key else freeze(part)
    return result

def thaw(value):
    if isinstance(value,list):return [thaw(v) for v in value]
    if not isinstance(value,dict):return value
    if set(value)=={'$text'}:return ingest(value['$text'],'source-text')
    if set(value)=={'$slice','start','end'}:
        text=ingest(value['$slice'],'source-text');start=value['start'];end=value['end']
        if not isinstance(start,int) or not isinstance(end,int) or not 0<=start<=end<=len(text):raise IntegrationError('Sai offset evidence.')
        return text[start:end]
    return {k:thaw(v) for k,v in value.items()}

def refs(value):
    if isinstance(value,list):return set().union(*(refs(v) for v in value)) if value else set()
    if not isinstance(value,dict):return set()
    found={value[k] for k in ('$text','$slice') if k in value}
    return found.union(*(refs(v) for v in value.values()))

def link_texts(attempt,value):
    row=db.one('SELECT status FROM node_attempts WHERE id=?',(attempt,))
    if row and row['status']!='RUNNING':return
    for aid in refs(value):ingest(aid,'source-text')
    with db.LOCK,db.conn() as c:
        for aid in refs(value):
            c.execute("INSERT INTO artifact_links VALUES(?,?,'INPUT',?) ON CONFLICT DO NOTHING",(attempt,aid,'text:'+aid))

def ingest(aid,kind=None):
    row=db.one('SELECT * FROM artifacts WHERE id=?',(aid,))
    if not row or not row['validated'] or row['schema_version']!=VERSION:raise IntegrationError('Artifact thiếu hoặc chưa validate/khác schema.')
    value=decode(row['payload']) if row['payload'] is not None else row['body']
    if digest(row['body'] if row['body'] is not None else canonical(value))!=row['content_hash']:raise IntegrationError('Hash artifact không khớp; không ingest.')
    if kind and row['kind']!=kind:raise IntegrationError('Sai loại artifact; không ingest.')
    return value

def begin(rid,node,inputs,versions):
    # Hash and schema validation before accepting dependencies.
    for aid in inputs.values():ingest(aid)
    fp=digest(canonical({'inputs':inputs,'versions':versions}))
    with db.LOCK,db.conn() as c:
        if db.database_url():c.execute('SELECT id FROM pipeline_runs WHERE id=? FOR UPDATE',(rid,))
        rows=c.execute('SELECT * FROM node_attempts WHERE run_id=? AND node_id=? ORDER BY attempt_no DESC',(rid,node)).fetchall()
        for row in rows:
            if row['fingerprint']==fp and row['status'] in ('RUNNING','COMPLETED'):return row['id']
        aid=str(uuid.uuid4());number=max((r['attempt_no'] for r in rows),default=0)+1
        c.execute('INSERT INTO node_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?)',
          (aid,rid,node,number,fp,'RUNNING',wire(versions),wire({}),None,db.now(),None))
        for role,artifact_id in inputs.items():c.execute('INSERT INTO artifact_links VALUES(?,?,?,?)',(aid,artifact_id,'INPUT',role))
    return aid

def finish(attempt,kind,payload,validator,usage=None,error=None):
    existing=db.one('SELECT status FROM node_attempts WHERE id=?',(attempt,))
    if existing and existing['status']=='COMPLETED':
        return db.one("SELECT artifact_id FROM artifact_links WHERE attempt_id=? AND direction='OUTPUT' AND role='result'",(attempt,))['artifact_id']
    if existing and existing['status']=='FAILED':raise IntegrationError('Attempt đã kết thúc; retry cần attempt mới.')
    link_texts(attempt,payload)
    for link in db.rows("SELECT artifact_id FROM artifact_links WHERE attempt_id=? AND direction='INPUT'",(attempt,)):ingest(link['artifact_id'])
    valid=True
    try:validator(payload)
    except Exception as e:valid=False;error={'code':'VALIDATION_FAILED','message':str(e)}
    with db.LOCK,db.conn() as c:
        row=c.execute('SELECT status FROM node_attempts WHERE id=?',(attempt,)).fetchone()
        if not row:raise IntegrationError('Không tìm thấy node attempt.')
        if row['status']=='COMPLETED':
            return c.execute("SELECT artifact_id FROM artifact_links WHERE attempt_id=? AND direction='OUTPUT' AND role='result'",(attempt,)).fetchone()['artifact_id']
        output=_artifact(c,kind,payload=payload,validated=valid and not error)
        c.execute("INSERT INTO artifact_links VALUES(?,?,'OUTPUT','result') ON CONFLICT DO NOTHING",(attempt,output))
        c.execute('UPDATE node_attempts SET status=?,usage=?,error=?,finished=? WHERE id=?',
          ('COMPLETED' if valid and not error else 'FAILED',wire(usage or {}),wire(error) if error else None,db.now(),attempt))
    if not valid:raise IntegrationError('Output không hợp lệ; đã giữ artifact để debug, không ingest.')
    return output

def capture(rid,node,inputs,versions,kind,payload,validator=lambda _:None,usage=None,error=None):
    frozen_inputs={role:artifact('node-input',payload=freeze(v)) for role,v in inputs.items()}
    if versions.get('legacy'):
        fp=digest(canonical({'inputs':frozen_inputs,'versions':versions}))
        saved=db.one("SELECT n.id,l.artifact_id FROM node_attempts n JOIN artifact_links l ON l.attempt_id=n.id WHERE n.run_id=? AND n.node_id=? AND n.fingerprint=? AND n.status='FAILED' AND l.direction='OUTPUT' AND l.role='result' ORDER BY n.attempt_no DESC LIMIT 1",(rid,node,fp))
        if saved:return saved['id'],saved['artifact_id']
    attempt=begin(rid,node,frozen_inputs,versions)
    for value in inputs.values():link_texts(attempt,freeze(value))
    frozen=freeze(payload);link_texts(attempt,frozen)
    result=finish(attempt,kind,frozen,lambda p:validator(thaw(p)),usage,error)
    return attempt,result

def output(attempt,kind=None):
    row=db.one("SELECT l.artifact_id FROM artifact_links l JOIN node_attempts n ON n.id=l.attempt_id WHERE n.id=? AND n.status='COMPLETED' AND l.direction='OUTPUT' AND l.role='result'",(attempt,))
    return ingest(row['artifact_id'],kind) if row else None

def validate_scores(value,config):
    from . import assessment_scoring
    items=value if isinstance(value,list) else [value]
    ids=set()
    for item in items:
        if item['candidate_id'] in ids:raise IntegrationError('Trùng nguồn trong output điểm.')
        ids.add(item['candidate_id'])
        expected=assessment_scoring.score(config,item,item['be_rank'])
        for field in ('score','coverage','lane','scorer_version'):
            if item.get(field)!=expected[field]:raise IntegrationError('Output điểm không khớp công thức: '+field)

def manifest(rid):
    row=db.one('SELECT * FROM pipeline_runs WHERE id=?',(rid,))
    if not row:raise KeyError(rid)
    attempts=db.rows('SELECT * FROM node_attempts WHERE run_id=? ORDER BY started,node_id,attempt_no',(rid,))
    for attempt in attempts:
        for key in ('versions','usage','error'):attempt[key]=decode(attempt[key])
        from datetime import datetime
        attempt['elapsed_seconds']=(datetime.fromisoformat(attempt['finished'])-datetime.fromisoformat(attempt['started'])).total_seconds() if attempt['finished'] else None
        attempt['links']=db.rows('SELECT l.direction,l.role,a.id,a.kind,a.schema_version,a.content_hash,a.validated FROM artifact_links l JOIN artifacts a ON a.id=l.artifact_id WHERE l.attempt_id=? ORDER BY l.direction,l.role',(attempt['id'],))
    table={'content-fetch':'content_fetches','assessment-group':'assessment_groups','people-search':'people_searches'}.get(row['origin_kind'],'public_assessments')
    if row['origin_kind']=='people-search':
        from . import people_search
        origin={'status':people_search.detail(row['origin_id'])['status']}
    else:origin=db.one('SELECT status FROM '+table+' WHERE id=?',(row['origin_id'],))
    if not origin and row['origin_kind']=='assessment':origin=db.one('SELECT status FROM searches WHERE id=?',(row['origin_id'],))
    children=[]
    for aid in {l['id'] for n in attempts for l in n['links'] if l['kind']=='upstream-run' and l['validated']}:
        child=db.one('SELECT id,origin_kind,origin_id FROM pipeline_runs WHERE id=?',(ingest(aid,'upstream-run')['pipeline_run_id'],))
        if child:children.append(child)
    reviews=[]
    if row['origin_kind']=='assessment':
        reviews=db.rows('SELECT * FROM public_source_labels WHERE assessment_id=? ORDER BY created',(row['origin_id'],))
        internal=db.one('SELECT result_run_id FROM searches WHERE id=?',(row['origin_id'],))
        if internal and internal['result_run_id']:reviews+=db.rows('SELECT * FROM feedback WHERE run_id=? ORDER BY id',(internal['result_run_id'],))
    return {**row,'metadata':decode(row['metadata']),'attempts':attempts,'upstream_runs':children,'recruiter_reviews':reviews,'schema_version':VERSION,'status':origin['status'] if origin else 'UNKNOWN'}

def read_artifact(rid,aid):
    if not db.one('SELECT l.artifact_id FROM artifact_links l JOIN node_attempts n ON n.id=l.attempt_id WHERE n.run_id=? AND l.artifact_id=?',(rid,aid)):raise KeyError(aid)
    row=db.one('SELECT * FROM artifacts WHERE id=?',(aid,));row['payload']=decode(row['payload'])
    # Unvalidated output is readable for debug, never ingested.
    row['integrity_ok']=digest(row['body'] if row['body'] is not None else canonical(row['payload']))==row['content_hash']
    return row

def safe_export(value):
    if isinstance(value,list):return [safe_export(v) for v in value]
    if not isinstance(value,dict):return value
    private={'authorization','headers','credentials','api_key','apikey','password','access_token','refresh_token','private_note','private_notes'}
    return {k:('[REDACTED]' if k.lower() in private else safe_export(v)) for k,v in value.items()}

def export(rid,visited=None):
    visited=set() if visited is None else visited
    if rid in visited:return {'pipeline_run_id':rid,'already_included':True}
    visited.add(rid)
    value=manifest(rid);ids={link['id'] for n in value['attempts'] for link in n['links']}
    artifacts=[read_artifact(rid,aid) for aid in sorted(ids)]
    children={a['payload']['pipeline_run_id'] for a in artifacts if a['kind']=='upstream-run' and a['validated']}
    return safe_export({'manifest':value,'artifacts':artifacts,'upstream_runs':[export(child,visited) for child in sorted(children)],
      'export_policy':'Sensitive metadata keys redacted; hashes refer to original stored artifacts, not redacted export. Not an import format.',
      'notice':'Contains CV/JD data. Local authorized review only. No provider calls performed.'})
