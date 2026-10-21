"""Private, single-workspace online pilot. Local mode remains unchanged."""
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from contextvars import ContextVar
from urllib.parse import urlsplit
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from . import db

actor = ContextVar('pilot_actor', default=None)
COOKIE = '__Host-pilot_session'
SQL = '''
CREATE TABLE IF NOT EXISTS pilot_sessions (token_hash TEXT PRIMARY KEY, username TEXT NOT NULL, credential_hash TEXT NOT NULL, csrf TEXT NOT NULL, expires DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS pilot_login_attempts (id TEXT PRIMARY KEY, bucket TEXT NOT NULL, at DOUBLE PRECISION NOT NULL);
CREATE INDEX IF NOT EXISTS pilot_login_bucket ON pilot_login_attempts(bucket,at);
CREATE TABLE IF NOT EXISTS pilot_audit (id TEXT PRIMARY KEY, request_id TEXT NOT NULL, username TEXT NOT NULL, action TEXT NOT NULL, resource TEXT, task_id INTEGER, status INTEGER, release TEXT NOT NULL, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pilot_feedback (id TEXT PRIMARY KEY, username TEXT NOT NULL, job_id TEXT NOT NULL, run_id TEXT, description TEXT NOT NULL, expected TEXT NOT NULL, quality TEXT NOT NULL, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pilot_task_actors (task_id INTEGER PRIMARY KEY, username TEXT NOT NULL, request_id TEXT NOT NULL, release TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pilot_uncertain (task_id INTEGER PRIMARY KEY, resolved INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL);
'''

def enabled():
    mode = os.environ.get('PILOT_MODE', 'LOCAL')
    if mode not in ('LOCAL', 'ONLINE_PILOT'): raise RuntimeError('PILOT_MODE must be LOCAL or ONLINE_PILOT')
    return mode == 'ONLINE_PILOT'

def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    value = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return 'scrypt$'+salt+'$'+value

def verify(password, encoded):
    try:
        _, salt, _ = encoded.split('$')
        return hmac.compare_digest(password_hash(password, salt), encoded)
    except (ValueError, TypeError): return False

def accounts():
    return json.loads(os.environ.get('PILOT_USERS_JSON', '{}'))

def configure():
    if not enabled(): return
    origin = os.environ.get('PILOT_ORIGIN', '')
    parts = urlsplit(origin)
    if parts.scheme != 'https' or not parts.netloc or parts.path or parts.query or parts.fragment or parts.username:
        raise RuntimeError('PILOT_ORIGIN must be an exact HTTPS origin')
    if not os.environ.get('DATABASE_URL') or not os.environ.get('PILOT_RUNTIME_DIR'):
        raise RuntimeError('Online requires a separate DATABASE_URL and persistent PILOT_RUNTIME_DIR')
    users = accounts()
    if set(users) != {'tri', 'dat'} or users['tri'].get('role') != 'admin' or users['dat'].get('role') != 'recruiter':
        raise RuntimeError('Configure the two pilot accounts tri/admin and dat/recruiter')
    for user in users.values():
        if not re.fullmatch(r'scrypt\$[0-9a-f]{32}\$[0-9a-f]{128}', user.get('password_hash', '')):
            raise RuntimeError('Pilot passwords must be pre-hashed with scrypt')
    if not os.environ.get('PILOT_RELEASE'): raise RuntimeError('PILOT_RELEASE is required')

def check_clean_runtime():
    if not enabled(): return
    if any((db.RUNTIME/name).exists() for name in ('google-token.json', 'connector-snapshot.json', 'database.json')):
        raise RuntimeError('Online runtime must not contain local connector credentials or snapshots')
    if db.one("SELECT id FROM files WHERE group_name NOT IN ('job_public') LIMIT 1") or db.one("SELECT id FROM profiles WHERE kind='candidate' LIMIT 1"):
        raise RuntimeError('Online DB contains private/pool data; use a clean pilot database')

def interrupt_online():
    # No recovery queue may replay a provider request whose response was lost.
    with db.LOCK, db.conn() as c:
        for row in c.execute("SELECT id FROM tasks WHERE status='RUNNING'").fetchall():
            c.execute('INSERT OR IGNORE INTO pilot_uncertain VALUES(?,0,?)',(row['id'],db.now()))
        c.execute("UPDATE tasks SET status='INTERRUPTED',error='Restart: kiểm tra trạng thái và phí trước khi tiếp tục.' WHERE status IN ('RUNNING','PENDING')")

def audit(action, resource=None, task_id=None, status=None):
    current = actor.get()
    if not enabled() or not current: return
    db.execute('INSERT INTO pilot_audit VALUES(?,?,?,?,?,?,?,?,?)',
      (secrets.token_hex(16), current['request_id'], current['username'], action, resource, task_id, status,
       os.environ.get('PILOT_RELEASE', 'unknown'), db.now()))

# Fail closed: new endpoints are not automatically exposed in the online pilot.
READ = (
 r'/api/status', r'/api/files', r'/api/profiles/job(?:/[^/]+)?', r'/api/local-source/[^/]+',
 r'/api/jobs/[^/]+/config', r'/api/v1/jobs/[^/]+/(?:scouting-guidance|people-searches)',
 r'/api/v1/web-searches(?:/[^/]+)?', r'/api/v1/people-searches/[^/]+(?:/(?:assessment-groups|assessment-group-preview|content-fetches))?',
 r'/api/v1/content-fetches/[^/]+', r'/api/v1/assessment-groups/[^/]+(?:/(?:results|sources/[^/]+))?',
 r'/api/v1/public-assessments(?:/[^/]+(?:/(?:export|benchmark))?)?', r'/api/v1/public-assessment-preview',
 r'/api/v1/pipeline-runs/.*', r'/api/pilot/feedback',
)
WRITE = (
 r'/api/jobs/upload', r'/api/jobs/[^/]+/(?:prepare|propose|config|criteria/approve|strategies|approve)',
 r'/api/v1/jobs/[^/]+/(?:scouting-guidance|exa-queries|people-searches)',
 r'/api/v1/web-searches(?:/[^/]+/retry)?', r'/api/v1/people-searches/[^/]+/(?:content-fetches|assessment-groups)',
 r'/api/v1/(?:content-fetches|assessment-groups|public-assessments)/[^/]+/retry',
 r'/api/v1/public-assessments(?:/[^/]+/labels)?', r'/api/v1/public-assessment-preview', r'/api/pilot/feedback',
)
ADMIN = (r'/api/providers(?:/[^/]+/models)?', r'/api/settings/(?:ai|model)', r'/api/connections/check',
         r'/api/prompts', r'/api/calls', r'/api/pilot/settings',r'/api/pilot/audit', r'/api/pilot/uncertain(?:/\d+/resolve)?')

def allowed(path, method, role):
    paths = READ if method in ('GET','HEAD') else WRITE
    return any(re.fullmatch(p,path) for p in paths) or (role=='admin' and any(re.fullmatch(p,path) for p in ADMIN))

def identity(request):
    token = request.cookies.get(COOKIE, '')
    if not token: return None
    row = db.one('SELECT * FROM pilot_sessions WHERE token_hash=?', (hashlib.sha256(token.encode()).hexdigest(),))
    if not row or row['expires'] <= time.time(): return None
    user = accounts().get(row['username'])
    if not user or not hmac.compare_digest(row['credential_hash'], hashlib.sha256(user['password_hash'].encode()).hexdigest()): return None
    return {'username':row['username'], 'role':user['role'], 'csrf':row['csrf']}

async def middleware(request, call_next):
    if not enabled(): return await call_next(request)
    path = request.url.path
    origin = os.environ['PILOT_ORIGIN']
    if request.headers.get('host') != urlsplit(origin).netloc:
        return JSONResponse({'detail':'Host không hợp lệ'},status_code=403)
    # Static app shell contains no workspace data. API/files are authenticated below.
    public = path in ('/', '/api/health', '/api/ready', '/api/auth/session', '/api/auth/login') or path.startswith('/assets/')
    write = request.method not in ('GET','HEAD','OPTIONS')
    if write and request.headers.get('origin') != origin:
        return JSONResponse({'detail':'Origin không hợp lệ'},status_code=403)
    try:length=int(request.headers.get('content-length', '0') or 0)
    except ValueError:return JSONResponse({'detail':'Content-Length không hợp lệ'},status_code=400)
    if length>16*1024*1024:
        return JSONResponse({'detail':'Tệp vượt giới hạn 12 MB của pilot online'},status_code=413)
    user = identity(request) if not public or path=='/api/auth/session' else None
    if not public and not user: return JSONResponse({'detail':'Vui lòng đăng nhập'},status_code=401)
    if user and write and (not request.headers.get('x-csrf-token') or not hmac.compare_digest(request.headers['x-csrf-token'], user['csrf'])):
        return JSONResponse({'detail':'CSRF không hợp lệ'},status_code=403)
    if not public and path!='/api/auth/logout' and not allowed(path,request.method,user['role']):
        return JSONResponse({'detail':'Tính năng không mở trong pilot online'},status_code=403)
    if write and request.headers.get('content-type','').startswith('application/json'):
        # Bodyless actions may carry the shared client's JSON content type.
        # Leave required-body validation to the endpoint, but reject malformed JSON.
        try:
            raw=await request.body()
            body=json.loads(raw) if raw else None
        except ValueError:return JSONResponse({'detail':'JSON không hợp lệ'},status_code=400)
        if isinstance(body,dict) and body.get('assessment_engine','CURRENT')!='CURRENT':
            return JSONResponse({'detail':'Jev không mở trong pilot online'},status_code=403)
    if request.query_params.get('assessment_engine','CURRENT')!='CURRENT':
        return JSONResponse({'detail':'Jev không mở trong pilot online'},status_code=403)
    paid=write and (path.endswith(('/prepare','/propose','/strategies','/exa-queries','/people-searches','/content-fetches','/assessment-groups','/retry')) or path in ('/api/jobs/upload','/api/v1/web-searches','/api/v1/public-assessments'))
    if paid and db.one('SELECT task_id FROM pilot_uncertain WHERE resolved=0 LIMIT 1'):
        return JSONResponse({'detail':'Có request bị gián đoạn chưa rõ chi phí. Tri cần kiểm tra và xử lý trong Quản trị trước khi chạy tiếp.'},status_code=409)
    token = actor.set({'username':user['username'], 'request_id':secrets.token_hex(16)}) if user else None
    try:
        response = await call_next(request)
        if user and write: audit(request.method, path, status=response.status_code)
        response.headers['Cache-Control']='no-store' if not path.startswith('/assets/') else 'public, max-age=3600'
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='same-origin'
        response.headers['X-Frame-Options']='DENY'
        if token: response.headers['X-Pilot-Request-ID']=actor.get()['request_id']
        return response
    finally:
        if token: actor.reset(token)

router=APIRouter()
class Login(BaseModel):
    username:str=Field(min_length=1,max_length=80)
    password:str=Field(min_length=1,max_length=256)

@router.get('/api/auth/session')
def session(request:Request):
    if not enabled():return {'mode':'LOCAL','user':None}
    return {'mode':'ONLINE_PILOT','user':identity(request)}

@router.post('/api/auth/login')
def login(body:Login, request:Request, response:Response):
    if not enabled():raise HTTPException(404)
    name=body.username.strip().lower();now=time.time()
    peer=request.client.host if request.client else 'unknown'
    buckets=['user:'+name,'peer:'+peer]
    with db.LOCK, db.conn() as c:
        for bucket, limit in zip(buckets,(5,50)):
            count=c.execute('SELECT COUNT(*) AS n FROM pilot_login_attempts WHERE bucket=? AND at>?',(bucket,now-900)).fetchone()['n']
            if count>=limit:raise HTTPException(429,'Quá nhiều lần đăng nhập. Thử lại sau 15 phút.')
        for bucket in buckets:c.execute('INSERT INTO pilot_login_attempts VALUES(?,?,?)',(secrets.token_hex(16),bucket,now))
        c.execute('DELETE FROM pilot_login_attempts WHERE at<?',(now-86400,))
    user=accounts().get(name)
    dummy='scrypt$'+'00'*16+'$'+'00'*64
    if not verify(body.password,user['password_hash'] if user else dummy):raise HTTPException(401,'Tài khoản hoặc mật khẩu không đúng')
    token=secrets.token_urlsafe(48);csrf=secrets.token_urlsafe(32)
    db.execute('INSERT INTO pilot_sessions VALUES(?,?,?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),name,hashlib.sha256(user['password_hash'].encode()).hexdigest(),csrf,now+43200))
    response.set_cookie(COOKIE,token,max_age=43200,secure=True,httponly=True,samesite='lax',path='/')
    return {'username':name,'role':user['role'],'csrf':csrf}

@router.post('/api/auth/logout')
def logout(request:Request,response:Response):
    if not enabled():raise HTTPException(404)
    token=request.cookies.get(COOKIE,'')
    db.execute('DELETE FROM pilot_sessions WHERE token_hash=?',(hashlib.sha256(token.encode()).hexdigest(),))
    response.delete_cookie(COOKIE,path='/',secure=True,httponly=True,samesite='lax')
    return {'ok':True}

@router.get('/api/health')
def health():return {'ok':True}

@router.get('/api/ready')
def ready():
    try:db.one('SELECT 1 AS n')
    except Exception:raise HTTPException(503,'Not ready')
    return {'ok':True}

class Feedback(BaseModel):
    job_id:str=Field(min_length=1,max_length=80)
    run_id:str|None=Field(default=None,max_length=80)
    description:str=Field(min_length=1,max_length=4000)
    expected:str=Field(default='',max_length=4000)
    quality:str=Field(default='',max_length=4000)

@router.post('/api/pilot/feedback',status_code=201)
def feedback(body:Feedback):
    if not db.one("SELECT id FROM profiles WHERE id=? AND kind='job'",(body.job_id,)):raise HTTPException(404,'Không tìm thấy JD')
    if body.run_id and not any(db.one('SELECT id FROM '+t+' WHERE id=? AND job_id=?',(body.run_id,body.job_id)) for t in ('people_searches','assessment_groups','public_assessments')):
        raise HTTPException(400,'Lượt chạy không thuộc JD này')
    id=secrets.token_hex(16)
    db.execute('INSERT INTO pilot_feedback VALUES(?,?,?,?,?,?,?,?)',(id,actor.get()['username'],body.job_id,body.run_id,body.description,body.expected,body.quality,db.now()))
    return {'id':id}

@router.get('/api/pilot/feedback')
def feedback_history():return db.rows('SELECT * FROM pilot_feedback ORDER BY created DESC LIMIT 100')

@router.get('/api/pilot/audit')
def audit_history():return db.rows('SELECT * FROM pilot_audit ORDER BY created DESC LIMIT 200')

class Settings(BaseModel):
    provider:str=Field(min_length=1,max_length=80)
    model:str=Field(min_length=1,max_length=120)

@router.put('/api/pilot/settings')
def settings(body:Settings):
    from .providers import adapter
    try:adapter(body.provider)
    except Exception:raise HTTPException(400,'Provider không được hỗ trợ')
    with db.LOCK,db.conn() as c:
        if c.execute("SELECT id FROM tasks WHERE status IN ('PENDING','RUNNING') LIMIT 1").fetchone():raise HTTPException(409,'Chờ tác vụ hoàn tất trước khi đổi cấu hình.')
        for key,value in [('provider',body.provider),('model',body.model)]:
            c.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(key,db.dumps(value)))
    return {'provider':body.provider,'model':body.model,'verified':False}

@router.get('/api/pilot/uncertain')
def uncertain():
    return db.rows("SELECT t.id,t.kind,t.status,t.error,t.created,u.resolved FROM tasks t LEFT JOIN pilot_uncertain u ON u.task_id=t.id WHERE t.status='INTERRUPTED' ORDER BY t.id")

class Resolve(BaseModel):
    action:str=Field(pattern='^(RETRY|CANCEL)$')
    checked_provider:bool

@router.post('/api/pilot/uncertain/{task_id}/resolve')
def resolve(task_id:int,body:Resolve):
    if not body.checked_provider:raise HTTPException(400,'Xác nhận đã kiểm tra trạng thái và phí provider.')
    with db.LOCK,db.conn() as c:
        row=c.execute("SELECT * FROM tasks WHERE id=? AND status='INTERRUPTED'",(task_id,)).fetchone()
        if not row:raise HTTPException(409,'Tác vụ không còn ở trạng thái gián đoạn.')
        c.execute('UPDATE pilot_uncertain SET resolved=1 WHERE task_id=?',(task_id,))
        c.execute('UPDATE tasks SET status=?,updated=? WHERE id=?',('PENDING' if body.action=='RETRY' else 'CANCELLED',db.now(),task_id))
    return {'ok':True}
