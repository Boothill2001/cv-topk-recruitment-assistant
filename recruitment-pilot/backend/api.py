import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4
from fastapi import FastAPI, HTTPException, Request, Query, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from . import db, engine
from .models import ConfigUpdate, MatchRequest, ReportRequest, FeedbackRequest, Fact, RetrievalPolicy
from .integrations import IntegrationError, check_ai, authorize, Drive, redact_error, sheet_connected
from .ai_runtime import provider_catalog, available_models
from .providers import adapter
from .prompts import PROMPTS, VERSION

@asynccontextmanager
async def lifespan(app):
    db.init()
    if db.setting('model') is None:db.set_setting('model','deepseek-flash')
    if db.setting('provider') is None:db.set_setting('provider','deepseek')
    jobs=[asyncio.create_task(engine.worker()),asyncio.create_task(engine.worker(sync_only=True)),asyncio.create_task(engine.scheduler())]
    yield
    for t in jobs:t.cancel()
    await asyncio.gather(*jobs,return_exceptions=True)

app=FastAPI(title='Recruitment pilot',lifespan=lifespan)

@app.middleware('http')
async def local_only(request:Request,call_next):
    # Bind loopback AND reject DNS-rebinding hosts/cross-site mutation requests.
    host=request.headers.get('host','').split(':')[0]
    if host not in ('127.0.0.1','localhost','testserver'):return JSONResponse({'detail':'Localhost only'},status_code=403)
    if request.method not in ('GET','HEAD','OPTIONS'):
        origin=request.headers.get('origin')
        port=request.url.port or 80
        if origin and origin not in (f'http://127.0.0.1:{port}',f'http://localhost:{port}'):
            return JSONResponse({'detail':'Origin không hợp lệ'},status_code=403)
        if request.headers.get('x-pilot-request')!='1':return JSONResponse({'detail':'Thiếu header yêu cầu localhost'},status_code=403)
    return await call_next(request)

@app.exception_handler(IntegrationError)
async def integration_error(request,exc):return JSONResponse({'detail':str(exc)},status_code=409)

@app.get('/api/status')
def status():
    profiles=db.rows('SELECT kind,status,COUNT(*) n FROM profiles GROUP BY kind,status')
    calls=db.rows('SELECT usage,status,provider FROM calls')
    totals={'prompt_tokens':0,'completion_tokens':0,'total_tokens':0,'requests':len(calls)}
    by_provider={}
    for c in calls:
        u=json.loads(c['usage'] or '{}')
        for key in ('prompt_tokens','completion_tokens','total_tokens'):totals[key]+=u.get(key,0)
        p=by_provider.setdefault(c['provider'],{'requests':0,'total_tokens':0})
        p['requests']+=1;p['total_tokens']+=u.get('total_tokens',0)
    return {'profiles':profiles,'files':db.one('SELECT COUNT(*) n FROM files')['n'],
        'last_sync':db.setting('last_sync'),'sync_progress':db.setting('sync_progress'),
        'tasks':db.rows('SELECT * FROM tasks ORDER BY id DESC LIMIT 40'),
        'ai':db.setting('ai_check',{}),'google_connected':(db.RUNTIME/'google-token.json').exists(),'sheets_connected':sheet_connected(),
        'usage':totals,'usage_by_provider':by_provider,'provider':db.setting('provider','deepseek'),'model':db.setting('model','deepseek-flash'),
        'source_mode':'oauth' if (db.RUNTIME/'google-token.json').exists() else 'connector_snapshot',
        'prompt_version':VERSION,'architecture':'structured-2.0','storage':'postgresql' if db.database_url() else 'isolated-test',
        'backfill':engine.backfill_preview(),'backfill_last':db.setting('backfill_last'),
        'preparation_errors':{r['key'].split(':',1)[1]:json.loads(r['value']) for r in db.rows("SELECT key,value FROM settings WHERE key LIKE ?",('config_error:%',)) if json.loads(r['value'])}}

@app.get('/api/backfill')
def backfill_preview():return engine.backfill_preview()

@app.post('/api/backfill')
def backfill():return {'task_id':engine.enqueue('backfill',{}),'estimate':engine.backfill_preview()}

@app.post('/api/jobs/upload')
async def upload_job(file:UploadFile=File(...),job_id:str=Form(''),private_note:str=Form('')):
    from io import BytesIO
    from pypdf import PdfReader
    import hashlib,time
    content=await file.read(15*1024*1024+1)
    if len(content)>15*1024*1024:raise HTTPException(413,'PDF tối đa 15 MB.')
    if not content.startswith(b'%PDF-'):raise HTTPException(400,'Chỉ nhận JD PDF.')
    job_id=job_id.strip() or engine.identity(file.filename or '', 'job_public') or 'IT-'+str(int(time.time()*1000))
    if engine.identity(job_id,'job_public')!=job_id:raise HTTPException(400,'Mã job dạng IT-602 hoặc IT-595c.')
    if len(private_note)>30000:raise HTTPException(400,'Note tối đa 30.000 ký tự.')
    try:text=await asyncio.to_thread(lambda:'\n'.join(p.extract_text() or '' for p in PdfReader(BytesIO(content)).pages))
    except Exception:raise HTTPException(400,'Không đọc được PDF; kiểm tra file hoặc mật khẩu.')
    fid='local:'+job_id
    other=db.one("SELECT id FROM files WHERE entity_id=? AND group_name='job_public' AND available=1 AND id!=?",(job_id,fid))
    if other:
        # An upload is also an entry point to an existing Drive-backed job.
        # Never replace its authoritative source or discard a supplied note silently.
        return {'job_id':job_id,'status':'EXISTING','llm_calls':0,
                'message':'Job '+job_id+' đã có nguồn JD trong hệ thống. Đã mở job hiện có; file và note vừa chọn chưa được lưu. Nếu đây là bản cập nhật, sửa nguồn trên Drive rồi đồng bộ; hoặc nhập mã job mới để tạo job riêng.'}
    folder=db.RUNTIME/'uploads';folder.mkdir(exist_ok=True)
    path=folder/(hashlib.sha256(fid.encode()).hexdigest()+'.pdf');path.write_bytes(content)
    touched=False
    for sid,group,value,name in [(fid,'job_public',text,file.filename or job_id+'.pdf'),(fid+':note','job_private',private_note,job_id+' private note')]:
        old=db.one('SELECT * FROM files WHERE id=?',(sid,))
        if not value and group=='job_private':
            if old and old['available']:
                db.execute("UPDATE files SET available=0,status='UNAVAILABLE' WHERE id=?",(sid,));touched=True
            continue
        h=engine.digest(value);changed=not old or old['hash']!=h or not old['available']
        touched=touched or changed
        revision=(old['revision']+int(changed)) if old else 1
        status='READY' if len(value.strip())>=20 else 'NEEDS_OCR'
        db.execute('INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(sid,name,'application/pdf' if group=='job_public' else 'text/plain','/api/local-source/'+job_id if group=='job_public' else '',group,job_id,db.now(),h,value,status,None if status=='READY' else 'PDF cần OCR',1,revision,db.now()))
    if touched:engine.invalidate('job',job_id)
    else:return {'job_id':job_id,'status':'UNCHANGED','llm_calls':0}
    if len(text.strip())<20:
        await engine.normalize_profile('job',job_id)
        return {'job_id':job_id,'status':'NEEDS_OCR'}
    return {'job_id':job_id,'task_id':engine.enqueue('normalize',{'kind':'job','id':job_id}),'status':'PENDING'}

@app.get('/api/local-source/{job_id}')
def local_source(job_id:str):
    import hashlib
    path=db.RUNTIME/'uploads'/(hashlib.sha256(('local:'+job_id).encode()).hexdigest()+'.pdf')
    if not path.is_file():raise HTTPException(404)
    return FileResponse(path,media_type='application/pdf')

class FactsUpdate(BaseModel):
    revision:int
    facts:list[Fact]

@app.put('/api/profiles/{kind}/{entity_id}/facts')
def correct_facts(kind:str,entity_id:str,body:FactsUpdate):
    with db.LOCK:
        old=db.one('SELECT * FROM profiles WHERE id=? AND kind=?',(entity_id,kind))
        if not old or old['revision']!=body.revision:raise IntegrationError('Profile đã đổi; tải lại.')
        facts=[f.model_dump() for f in body.facts]
        if len({f['id'] for f in facts})!=len(facts):raise IntegrationError('Fact ID trùng.')
        for fact in facts:engine.validate_evidence(fact['evidence'],engine.sources(entity_id,kind))
        data=json.loads(old['data']);data['facts']=facts;data.setdefault('_meta',{})['human_corrected_at']=db.now()
        data['_meta']['correction_review_required']=False
        db.set_setting('facts_override:'+kind+':'+entity_id,{'facts':facts,'source_hash':old['source_hash']})
        db.execute("UPDATE profiles SET revision=revision+1,data=?,status='READY',updated=? WHERE id=? AND kind=?",(db.dumps(data),db.now(),entity_id,kind))
        engine.persist_facts(entity_id,kind,body.revision+1,data,old['source_hash']);engine.invalidate(kind,entity_id)
    return profile(kind,entity_id)

@app.post('/api/benchmark')
def benchmark():
    from .benchmark import evaluate
    return evaluate()

@app.get('/api/benchmark')
def benchmarks():return [db.unpack(r) for r in db.rows('SELECT * FROM benchmark_runs ORDER BY id DESC LIMIT 10')]

@app.post('/api/connections/check')
async def check():
    result=await check_ai(probe=True)
    try:
        inv=await Drive().inventory();google={'ok':True,'files':len(inv)}
    except Exception as e:google={'ok':False,'error':redact_error(e)}
    db.set_setting('google_check',{**google,'at':db.now()})
    return {'ai':result,'google':google}

def require_idle():
    if db.one("SELECT id FROM tasks WHERE status IN ('RUNNING','PENDING')"):
        raise IntegrationError('Đợi hàng đợi hoàn tất trước khi đổi provider/model/key file.')

class ProviderChoice(BaseModel):
    provider:str=Field(min_length=1,max_length=40)
    model:str=Field(min_length=1,max_length=120,pattern=r'^[\w.\-]+$')
    env_file:str|None=Field(default=None,max_length=1000)

class ProviderKeyFile(BaseModel):env_file:str|None=Field(default=None,max_length=1000)

@app.get('/api/providers')
def providers():return provider_catalog()

@app.post('/api/providers/{provider}/models')
async def provider_models(provider:str,body:ProviderKeyFile):
    adapter(provider)
    with db.LOCK:
        require_idle()
    try:return {'provider':provider,'models':await available_models(provider,body.env_file)}
    except Exception as e:raise IntegrationError(redact_error(e))

@app.put('/api/settings/ai')
async def provider_choice(body:ProviderChoice):
    adapter(body.provider)
    with db.LOCK:
        require_idle()
    checked=await check_ai(body.provider,body.model,persist=False,probe=True,env_path=body.env_file)
    if not checked['ok']:raise IntegrationError(checked['error'])
    with db.LOCK,db.conn() as c:
        require_idle()
        settings=[('provider',body.provider),('model',body.model),('ai_check',{**checked,'checked_at':db.now()})]
        if body.env_file is not None:settings.append(('env_file:'+body.provider,body.env_file))
        c.executemany('INSERT OR REPLACE INTO settings VALUES(?,?)',[(k,db.dumps(v)) for k,v in settings])
    return checked

class ModelChoice(BaseModel):model:str=Field(min_length=1,max_length=120)
@app.put('/api/settings/model')
async def model_choice(body:ModelChoice):
    return await provider_choice(ProviderChoice(provider=db.setting('provider','deepseek'),model=body.model))

class OAuthRequest(BaseModel):
    client_path:str=Field(min_length=1,max_length=1000)
    enable_sheets:bool=False
@app.post('/api/connections/google')
async def google_auth(body:OAuthRequest):
    try:await asyncio.to_thread(authorize,body.client_path,body.enable_sheets)
    except Exception as e:raise IntegrationError(redact_error(e))
    engine.enqueue('sync',{});return {'ok':True}

@app.post('/api/sync')
async def sync():
    # OAuth must be usable; never imply a snapshot sync fetched new remote files.
    from .integrations import credentials
    await asyncio.to_thread(credentials)
    return {'task_id':engine.enqueue('sync',{})}

@app.post('/api/bootstrap')
def bootstrap():return {'task_id':engine.enqueue('bootstrap',{})}

@app.get('/api/files')
def files():return [{**r,'extraction':db.setting('extraction:'+r['id'])} for r in db.rows('SELECT id,name,mime,url,group_name,entity_id,modified,status,error,available,revision FROM files ORDER BY group_name,name')]

class Mapping(BaseModel):entity_id:str=Field(min_length=1,max_length=40)
@app.put('/api/files/{file_id}/mapping')
def mapping(file_id:str,body:Mapping):
    f=db.one('SELECT * FROM files WHERE id=?',(file_id,))
    if not f or f['group_name']=='reference':raise HTTPException(404,'File không cần mapping.')
    valid=engine.identity(body.entity_id,f['group_name'])
    if valid!=body.entity_id:raise HTTPException(400,'Mã phải là CV123 hoặc IT-123/IT-595c đúng loại.')
    db.set_setting('mapping:'+file_id,body.entity_id)
    return {'ok':True,'message':'Đã lưu mapping. Đồng bộ/nạp lại snapshot để áp dụng.'}

@app.get('/api/profiles/{kind}')
def profiles(kind:str):
    if kind not in ('candidate','job'):raise HTTPException(404)
    if db.database_url():
        # List screens do not transfer thousands of evidence-rich profiles every poll.
        fields=['title','summary','skills','domains','signals','unknowns','contradictions','error','_meta']
        projection=','.join("'"+k+"',data::jsonb->'"+k+"'" for k in fields)
        return [db.unpack(r) for r in db.rows('SELECT id,kind,revision,status,updated,jsonb_build_object('+projection+')::text AS data FROM profiles WHERE kind=? ORDER BY id',(kind,))]
    return [db.unpack(r) for r in db.rows('SELECT * FROM profiles WHERE kind=? ORDER BY id',(kind,))]

@app.get('/api/profiles/{kind}/{entity_id}')
def profile(kind:str,entity_id:str):
    p=db.unpack(db.one('SELECT * FROM profiles WHERE kind=? AND id=?',(kind,entity_id)))
    if not p:raise HTTPException(404,'Chưa có profile.')
    return {**p,'sources':engine.sources(entity_id,kind)}

@app.get('/api/jobs/{job_id}/config')
def get_config(job_id:str):
    r=db.unpack(db.one('SELECT * FROM configs WHERE job_id=?',(job_id,)))
    if r:
        source=db.one("SELECT revision,status FROM profiles WHERE id=? AND kind='job'",(job_id,))
        r['input_current']=bool(source and source['status']=='READY' and source['revision']==r['source_revision'])
        r['meta']=db.setting('config_meta:'+job_id)
        r['strategies_current']=r['strategy_hash']==engine.criteria_hash(r['data'])
        r['strategy_meta']=db.setting('strategy_meta:'+job_id)
        r['error']=db.setting('config_error:'+job_id)
    return r

@app.post('/api/jobs/{job_id}/propose')
def propose(job_id:str):return {'task_id':engine.enqueue('propose',{'job_id':job_id})}

@app.post('/api/jobs/{job_id}/prepare')
def prepare_job(job_id:str):
    if not engine.sources(job_id,'job'):raise HTTPException(404,'Không tìm thấy nguồn JD.')
    p=db.one("SELECT status FROM profiles WHERE kind='job' AND id=?",(job_id,))
    kind='propose' if p and p['status']=='READY' else 'normalize'
    payload={'job_id':job_id} if kind=='propose' else {'kind':'job','id':job_id}
    return {'task_id':engine.enqueue(kind,payload)}

@app.put('/api/jobs/{job_id}/config')
def update_config(job_id:str,body:ConfigUpdate):
    config=body.config.model_dump();engine.validate_config(config,False)
    with db.LOCK,db.conn() as c:
        old=c.execute('SELECT * FROM configs WHERE job_id=?',(job_id,)).fetchone()
        if not old or old['version']!=body.version:raise HTTPException(409,'Phiên bản đã đổi, tải lại.')
        config['scouting_guidance']=json.loads(old['data']).get('scouting_guidance')
        same=engine.criteria_hash(json.loads(old['data']))==engine.criteria_hash(config)
        # Retain old strategy text for review, but edits to criteria require regeneration.
        valid_strategy=old['strategy_hash'] if same else None
        c.execute('UPDATE configs SET version=version+1,data=?,approved=0,criteria_approved=?,strategy_hash=?,updated=? WHERE job_id=?',
            (db.dumps(config),old['criteria_approved'] if same else 0,valid_strategy,db.now(),job_id))
        c.execute("UPDATE runs SET status='STALE',updated=? WHERE job_id=? AND status IN ('COMPLETED','PARTIAL')",(db.now(),job_id))
    return get_config(job_id)

class VersionBody(BaseModel):version:int
@app.post('/api/jobs/{job_id}/criteria/approve')
def approve_criteria(job_id:str,body:VersionBody):
    with db.LOCK:
        config,_=engine.checked_config(job_id,body.version)
        engine.validate_config(json.loads(config['data']),False)
        db.execute('UPDATE configs SET criteria_approved=1,updated=? WHERE job_id=?',(db.now(),job_id))
    return get_config(job_id)

class StrategyRequest(VersionBody):
    count:int=Field(default=5,ge=2,le=20)
    guidance_version:int|None=Field(default=None,ge=0)
@app.post('/api/jobs/{job_id}/strategies')
def strategies(job_id:str,body:StrategyRequest):
    with db.LOCK:
        config,_=engine.checked_config(job_id,body.version)
        if not config['criteria_approved']:raise IntegrationError('Duyệt criteria trước khi sinh strategy.')
        from .scouting_guidance import capture
        guidance=capture(job_id,body.guidance_version,'strategies')
        return {'task_id':engine.enqueue('strategies',{'job_id':job_id,'version':body.version,'count':body.count,'guidance':guidance})}

@app.post('/api/jobs/{job_id}/approve')
def approve(job_id:str,body:VersionBody):
    with db.LOCK,db.conn() as c:
        config=c.execute('SELECT * FROM configs WHERE job_id=?',(job_id,)).fetchone()
        p=c.execute("SELECT * FROM profiles WHERE kind='job' AND id=?",(job_id,)).fetchone()
        if not config:
            raise HTTPException(409,'Chưa có yêu cầu cho JD. Chuẩn bị yêu cầu trước khi duyệt.')
        if config['version']!=body.version:
            raise HTTPException(409,f'Yêu cầu đã đổi từ phiên bản {body.version} sang {config["version"]}. Tải bản mới, kiểm tra rồi xác nhận lại.')
        if not p or p['status']!='READY':
            raise HTTPException(409,'JD chưa sẵn sàng. Kiểm tra trạng thái đọc JD trước khi duyệt.')
        if config['source_revision']!=p['revision']:
            raise HTTPException(409,'JD gốc đã thay đổi. Chuẩn bị yêu cầu mới rồi kiểm tra và duyệt lại.')
        engine.validate_config(json.loads(config['data']))
        if not config['criteria_approved'] or config['strategy_hash']!=engine.criteria_hash(json.loads(config['data'])):
            raise IntegrationError('Duyệt criteria rồi sinh strategy từ bản đã duyệt trước khi duyệt matching.')
        c.execute('UPDATE configs SET approved=1,updated=? WHERE job_id=?',(db.now(),job_id))
    return get_config(job_id)

@app.post('/api/jobs/{job_id}/match')
def match(job_id:str,body:MatchRequest):return {'run_id':engine.create_run(job_id,body.config_version)}

@app.get('/api/runs')
def runs():return db.rows('SELECT id,job_id,status,config_version,provider,model,prompt_version,error,created,updated FROM runs ORDER BY created DESC')

@app.get('/api/runs/{run_id}')
def run_detail(run_id:str):
    run=db.one('SELECT * FROM runs WHERE id=?',(run_id,))
    if not run:raise HTTPException(404)
    snap=json.loads(run.pop('snapshot'))
    evaluations=[db.unpack(e) for e in db.rows('SELECT * FROM evaluations WHERE run_id=?',(run_id,))]
    # Historical result semantics remain untouched.
    evaluations.sort(key=lambda e:(-(e['data'] or {}).get('retrieval_score',(e['data'] or {}).get('score') or 0),e['candidate_id']))
    return {**run,'config':snap['config'],'total':len(evaluations) if snap.get('assessment_engine') else len(snap['candidates']),'evaluations':evaluations,
            'source_files':[{**{k:v for k,v in s.items() if k!='text'},'id':s['source_id']}
                            for p in [snap['job'],*snap['candidates'].values()] for s in p['sources']],
            'candidate_profiles':{eid:p['profile'] for eid,p in snap['candidates'].items()},
            'is_current':engine.current({**run,'snapshot':snap}), 'engine':snap.get('assessment_engine',snap.get('engine','legacy-llm')),
            'metrics':db.setting('retrieval_metrics:'+run_id),
            'reports':[db.unpack(r,('data','selected')) for r in db.rows('SELECT * FROM reports WHERE run_id=? ORDER BY created DESC',(run_id,))]}

@app.post('/api/runs/{run_id}/retry')
def retry_run(run_id:str):
    with db.LOCK:
        r=db.one('SELECT * FROM runs WHERE id=?',(run_id,))
        if not r or r['status'] not in ('PARTIAL','FAILED','INTERRUPTED') or not engine.current(r):
            raise IntegrationError('Chỉ retry run lỗi với input vẫn hiện hành.')
        if json.loads(r['snapshot']).get('engine')!='structured-2.0':raise IntegrationError('Legacy run chỉ dùng tra cứu.')
        if r['prompt_version']!=VERSION:raise IntegrationError('Prompt đã đổi; tạo run mới.')
        db.execute("UPDATE runs SET status='PENDING',error=NULL,updated=? WHERE id=?",(db.now(),run_id))
        engine.enqueue('match',{'run_id':run_id})
    return {'run_id':run_id}

@app.post('/api/runs/{run_id}/compare')
def compare(run_id:str,body:ReportRequest):return {'report_id':engine.create_report(run_id,body.selected_ids,body.limit,body.threshold)}

class ExportRequest(BaseModel):
    threshold:float=Field(default=85,ge=0,le=100)
    top_k:int=Field(default=10,ge=1,le=10000)

@app.get('/api/runs/{run_id}/shortlist.csv')
def shortlist_csv(run_id:str,threshold:float=Query(default=85,ge=0,le=100),top_k:int=Query(default=10,ge=1,le=10000)):
    from .sheets import export_payload,csv_bytes
    body=ExportRequest(threshold=threshold,top_k=top_k)
    payload=export_payload(run_id,body.threshold,body.top_k,live=False)
    return Response(csv_bytes(payload),media_type='text/csv; charset=utf-8',headers={
        'Content-Disposition':f'attachment; filename="shortlist-{run_id}.csv"'})

@app.post('/api/runs/{run_id}/sheets')
async def export_sheet(run_id:str,body:ExportRequest):
    from .integrations import credentials
    from .sheets import create_export
    await asyncio.to_thread(credentials,True)
    return create_export(run_id,body.threshold,body.top_k)

@app.get('/api/runs/{run_id}/sheets')
def sheet_exports(run_id:str):
    return db.rows('SELECT id,run_id,status,error,url,created FROM sheet_exports WHERE run_id=? ORDER BY created DESC',(run_id,))

@app.post('/api/sheets/{export_id}/review')
async def sheet_review(export_id:str):
    from .sheets import import_review
    return await import_review(export_id)

@app.post('/api/sheets/{export_id}/retry')
def retry_sheet(export_id:str):
    with db.LOCK:
        row=db.one('SELECT * FROM sheet_exports WHERE id=?',(export_id,))
        if not row or row['status'] not in ('FAILED','INTERRUPTED'):
            raise IntegrationError('Chỉ thử lại export lỗi đã biết trạng thái.')
        db.execute("UPDATE sheet_exports SET status='PENDING',error=NULL,updated=? WHERE id=?",(db.now(),export_id))
        return {'task_id':engine.enqueue('sheet_export',{'export_id':export_id})}

@app.get('/api/reports/{report_id}')
def report(report_id:str):
    r=db.unpack(db.one('SELECT * FROM reports WHERE id=?',(report_id,)),('data','selected'))
    if not r:raise HTTPException(404)
    run=db.one('SELECT * FROM runs WHERE id=?',(r['run_id'],))
    r['is_current']=engine.current(run);return r

@app.post('/api/feedback')
def feedback(body:FeedbackRequest):
    if not db.one('SELECT candidate_id FROM evaluations WHERE run_id=? AND candidate_id=?',(body.run_id,body.candidate_id)):
        raise HTTPException(400,'Candidate không thuộc run.')
    db.execute('INSERT INTO feedback(run_id,candidate_id,decision,note,created) VALUES(?,?,?,?,?)',
               (body.run_id,body.candidate_id,body.decision,body.note,db.now()))
    run=db.one('SELECT * FROM runs WHERE id=?',(body.run_id,));snap=json.loads(run['snapshot'])
    db.execute('INSERT INTO labels(job_id,candidate_id,decision,note,profile_revision,job_revision,created) VALUES(?,?,?,?,?,?,?)',
        (run['job_id'],body.candidate_id,body.decision,body.note,snap['candidates'][body.candidate_id]['revision'],snap['job']['revision'],db.now()))
    return {'ok':True}

@app.get('/api/feedback')
def read_feedback():return db.rows('SELECT * FROM feedback ORDER BY id DESC')

@app.get('/api/prompts')
def prompts():return {'version':VERSION,'prompts':PROMPTS}

@app.get('/api/calls')
def calls():return [db.unpack(c,('usage',)) for c in db.rows('SELECT * FROM calls ORDER BY id DESC LIMIT 500')]

from .search_api import router as search_router
app.include_router(search_router)
from .exa_api import router as exa_router
app.include_router(exa_router)
from .people_api import router as people_router
app.include_router(people_router)
from .public_assessment_api import router as public_assessment_router
app.include_router(public_assessment_router)

from .content_api import router as content_router
app.include_router(content_router)
from .assessment_group_api import router as assessment_group_router
app.include_router(assessment_group_router)
from .scouting_guidance import router as scouting_guidance_router
app.include_router(scouting_guidance_router)

FRONTEND=db.ROOT/'frontend'/'dist'
@app.get('/assets/{asset_path:path}')
def asset(asset_path:str):
    assets=(FRONTEND/'assets').resolve();p=(assets/asset_path).resolve()
    if not p.is_relative_to(assets) or not p.is_file():raise HTTPException(404)
    return FileResponse(p)
@app.get('/{path:path}')
def spa(path:str):
    if path.startswith('api/'):raise HTTPException(404)
    index=FRONTEND/'index.html'
    if not index.exists():return JSONResponse({'detail':'Frontend chưa build. Chạy start-pilot.ps1.'},status_code=503)
    return FileResponse(index)
