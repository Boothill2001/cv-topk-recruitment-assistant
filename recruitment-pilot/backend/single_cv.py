"""Direct uploaded CV -> approved JD assessment, reusing ingestion and judge."""
import hashlib,json
from . import db,engine,search_orchestration
from .errors import IntegrationError

def check_job(job_id,version):
    config,_=engine.checked_config(job_id,version)
    if not config['criteria_approved']:raise IntegrationError('Xác nhận yêu cầu tuyển dụng trước khi đánh giá CV.')
    engine.validate_config(json.loads(config['data']),False)

@engine.serialized
def register(job_id,version,content,text,filename,note=''):
    check_job(job_id,version)
    # Use content-derived IDs; never overwrite an existing Drive CV based on filename.
    content_hash=hashlib.sha256(content).hexdigest()
    identity=engine.digest(content_hash+'\n'+note)
    eid='CV'+str(int(identity[:24],16))
    fingerprint=engine.digest(db.dumps({'job':job_id,'version':version,'identity':identity,
        'provider':db.setting('provider','deepseek'),'model':db.setting('model','deepseek-flash'),
        'prompt':search_orchestration.ai_assessment.PROMPT_VERSION,'profile_prompt':engine.digest(engine.PROMPTS['profile'])}))
    key='single_cv:'+fingerprint
    old=db.setting(key)
    if old:return detail(fingerprint)
    folder=db.RUNTIME/'uploads';folder.mkdir(exist_ok=True)
    (folder/(eid+'.pdf')).write_bytes(content)
    for fid,group,value,name,mime,url in [
        ('local:'+eid,'candidate_public',text,filename,'application/pdf','/api/v1/uploaded-cvs/'+eid+'/source'),
        ('local:'+eid+':note','candidate_private',note,eid+' private note','text/plain','')]:
        if not value:continue
        if not db.one('SELECT id FROM files WHERE id=?',(fid,)):
            db.execute('INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (fid,name,mime,url,group,eid,db.now(),engine.digest(value),value,'READY',None,1,1,db.now()))
    ticket={'ticket_id':fingerprint,'job_id':job_id,'config_version':version,'candidate_id':eid,
        'filename':filename,'content_hash':content_hash,'status':'PENDING','created':db.now(),'search_id':None,'error':None}
    db.set_setting(key,ticket)
    ticket['task_id']=engine.enqueue('single_cv',{'ticket_id':fingerprint})
    db.set_setting(key,ticket)
    return ticket

async def process(ticket_id):
    key='single_cv:'+ticket_id;t=db.setting(key)
    if not t:raise IntegrationError('Không tìm thấy CV đã tải lên.')
    check_job(t['job_id'],t['config_version'])
    await engine.normalize_profile('candidate',t['candidate_id'])
    p=db.one("SELECT status FROM profiles WHERE kind='candidate' AND id=?",(t['candidate_id'],))
    if not p or p['status']!='READY':raise IntegrationError('CV chưa chuẩn hóa hợp lệ; kiểm tra nguồn và bằng chứng.')
    check_job(t['job_id'],t['config_version'])
    t['search_id']=search_orchestration.create(t['job_id'],t['config_version'],1,candidate_ids=[t['candidate_id']])
    t['status']='COMPLETED';db.set_setting(key,t)

def detail(ticket_id):
    t=db.setting('single_cv:'+ticket_id)
    if not t:raise KeyError(ticket_id)
    task=db.one('SELECT status,error FROM tasks WHERE id=?',(t.get('task_id'),))
    if task and not t.get('search_id'):t={**t,'status':task['status'],'error':task['error']}
    return t

@engine.serialized
def retry(ticket_id):
    t=detail(ticket_id)
    check_job(t['job_id'],t['config_version'])
    if t.get('search_id') or t['status'] in ('PENDING','RUNNING'):return t
    t.update(task_id=engine.enqueue('single_cv',{'ticket_id':ticket_id}),status='PENDING',error=None)
    db.set_setting('single_cv:'+ticket_id,t)
    return t
