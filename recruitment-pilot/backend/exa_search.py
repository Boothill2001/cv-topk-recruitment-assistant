"""Exa web-search adapter and durable orchestration; no CV ingestion or LLM calls."""
import asyncio,ipaddress,json,math,os,re,uuid
from datetime import datetime,timedelta,timezone
from urllib.parse import urlsplit
import httpx
from pydantic import BaseModel,Field
from . import db,engine
from .errors import IntegrationError

API='https://api.exa.ai/search'
VERSION='exa-highlights-v4-search-type'
SEMAPHORE=asyncio.Semaphore(1)

def api_key():
    value=os.environ.get('EXA_API_KEY','').strip()
    if not value:
        path=db.RUNTIME/'secrets'/'exa.env'
        if path.is_file():
            match=re.search(r'(?m)^EXA_API_KEY\s*=\s*(.+)$',path.read_text(encoding='utf-8-sig'))
            if match:value=match[1].strip().strip('\"\x27')
    if not value:raise IntegrationError('Chưa cấu hình EXA_API_KEY ở backend.')
    return value

def configured():
    try:api_key();return True
    except IntegrationError:return False

class Source(BaseModel):
    title:str=''
    url:str
    highlights:list[str]=Field(default_factory=list)
    author:str|None=None
    publishedDate:str|None=None

class Response(BaseModel):
    requestId:str|None=None
    results:list[Source]
    costDollars:dict|float|None=None

def safe_url(url):
    try:
        parts=urlsplit(url);host=(parts.hostname or '').lower()
        if parts.scheme not in ('https','http') or not host or parts.username or parts.password:return False
        if host=='localhost' or host.endswith(('.local','.localhost','.internal')):return False
        try:return ipaddress.ip_address(host).is_global
        except ValueError:return '.' in host
    except ValueError:return False

def reported_cost(raw):
    value=raw.get('total') if isinstance(raw,dict) else raw
    if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and value>=0:return float(value)
    return None

def ensure_search(conn,query,refresh=False,mode='web',location_scope=None,search_type='auto'):
    api_key();query=query.strip()
    if search_type not in ('auto','deep'):raise IntegrationError('Chọn chế độ tìm kiếm Auto hoặc Deep.')
    if mode not in ('web','people'):raise IntegrationError('Chế độ Exa không hợp lệ.')
    if not 3<=len(query)<=1500:raise IntegrationError('Nhập nội dung tìm kiếm từ 3 đến 1.500 ký tự.')
    from .location_scope import effective_query
    effective=effective_query(query,location_scope,mode)
    if location_scope is not None:
        from .people_search import validate_query
        validate_query(effective)
    fingerprint=engine.digest(db.dumps({'query':query,'effective_query':effective,'location_scope':location_scope,'mode':mode,'search_type':search_type,'adapter':VERSION}))
    cutoff=(datetime.now(timezone.utc)-timedelta(hours=24)).isoformat()
    old=conn.execute("SELECT * FROM web_searches WHERE fingerprint=? AND (status IN ('PENDING','RUNNING') OR (status='COMPLETED' AND created>=?)) ORDER BY created DESC LIMIT 1",(fingerprint,cutoff)).fetchone()
    if old and (not refresh or old['status'] in ('PENDING','RUNNING')):return old['id']
    sid=str(uuid.uuid4());now=db.now()
    conn.execute('INSERT INTO web_searches(id,fingerprint,query,status,created,updated,mode,location_scope,effective_query,search_type) VALUES(?,?,?,?,?,?,?,?,?,?)',(sid,fingerprint,query,'PENDING',now,now,mode,location_scope,effective,search_type))
    conn.execute('INSERT INTO tasks(kind,payload,status,created,updated) VALUES(?,?,?,?,?)',('web_search',db.dumps({'search_id':sid}),'PENDING',now,now))
    return sid

@engine.serialized
def create(query,refresh=False,mode='web',location_scope=None,search_type='auto'):
    with db.LOCK,db.conn() as conn:sid=ensure_search(conn,query,refresh,mode,location_scope,search_type)
    return detail(sid)

def detail(sid):
    row=db.one('SELECT * FROM web_searches WHERE id=?',(sid,))
    if not row:raise KeyError(sid)
    data=json.loads(row.pop('response') or '{}')
    attempts=db.rows('SELECT status,request_id,cost_dollars,error,created FROM web_search_attempts WHERE search_id=? ORDER BY created',(sid,))
    costs=[a['cost_dollars'] for a in attempts if a['cost_dollars'] is not None]
    return {**row,**data,'usage':attempts,'known_cost_dollars':sum(costs),
            'has_unreported_cost':row['attempts']>len(attempts) or any(a['cost_dollars'] is None for a in attempts),'adapter_version':VERSION}

@engine.serialized
def retry(sid):
    api_key()
    s=detail(sid)
    if s['status'] in ('PENDING','RUNNING','COMPLETED'):return s
    if s['attempts']>=3:raise IntegrationError('Đã hết 3 lượt thử. Kiểm tra kết nối/quota trước khi tạo lượt tìm mới.')
    with db.LOCK,db.conn() as conn:
        conn.execute("UPDATE web_searches SET status='PENDING',error=NULL,updated=? WHERE id=?",(db.now(),sid))
        conn.execute('INSERT INTO tasks(kind,payload,status,created,updated) VALUES(?,?,?,?,?)',('web_search',db.dumps({'search_id':sid}),'PENDING',db.now(),db.now()))
    return detail(sid)

async def run(sid):
    async with SEMAPHORE:
        s=detail(sid)
        if s['status']=='COMPLETED':return
        try:
            if s['attempts']>=3:raise IntegrationError('Đã hết 3 lượt thử.')
            key=api_key()
        except IntegrationError as e:
            db.execute("UPDATE web_searches SET status='FAILED',error=?,updated=? WHERE id=?",(str(e),db.now(),sid));return
        db.execute("UPDATE web_searches SET status='RUNNING',error=NULL,updated=? WHERE id=?",(db.now(),sid))
        while s['attempts']<3:
            s['attempts']+=1;request_id=None;cost=None
            db.execute('UPDATE web_searches SET attempts=?,updated=? WHERE id=?',(s['attempts'],db.now(),sid))
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(60,connect=15)) as c:
                    # Canonical skill defaults: no inferred category, domains, dates or extra synthesis.
                    payload={'query':s.get('effective_query') or s['query'],'type':s['search_type'],'contents':{'highlights':True}}
                    if s['mode']=='people':payload['category']='people'
                    r=await c.post(API,headers={'x-api-key':key},json=payload)
                    r.raise_for_status();raw=r.json()
                if isinstance(raw,dict):request_id=raw.get('requestId');cost=reported_cost(raw.get('costDollars'))
                data=Response.model_validate(raw);items=[];seen=set()
                for item in data.results:
                    if not safe_url(item.url):continue
                    url=item.url.split('#',1)[0]
                    if url in seen:continue
                    seen.add(url);items.append(item.model_dump())
                result={'results':items,'request_id':data.requestId,'returned_count':len(data.results),
                        'omitted_count':len(data.results)-len(items),'retrieved_at':db.now()}
                with db.LOCK,db.conn() as conn:
                    conn.execute('INSERT INTO web_search_attempts VALUES(?,?,?,?,?,?,?)',(str(uuid.uuid4()),sid,'COMPLETED',request_id,cost,None,db.now()))
                    conn.execute("UPDATE web_searches SET response=?,status='COMPLETED',error=NULL,updated=? WHERE id=?",(db.dumps(result),db.now(),sid))
                return
            except Exception as e:
                # Never echo provider bodies, query content, headers or exceptions containing secrets.
                retryable=isinstance(e,(httpx.TimeoutException,httpx.NetworkError))
                if isinstance(e,httpx.HTTPStatusError):
                    code=e.response.status_code;retryable=code==429 or code>=500
                    error=f'Exa HTTP {code}: kiểm tra API key, quota hoặc kết nối.'
                elif isinstance(e,(httpx.TimeoutException,httpx.NetworkError)):error='Exa phản hồi quá lâu hoặc mất kết nối; phí lượt này chưa xác định.'
                else:error='Exa trả dữ liệu chưa hợp lệ; giữ lịch sử để kiểm tra, không tạo kết quả giả.'
                db.execute('INSERT INTO web_search_attempts VALUES(?,?,?,?,?,?,?)',(str(uuid.uuid4()),sid,'FAILED',request_id,cost,error,db.now()))
                if not retryable or s['attempts']>=3:
                    db.execute("UPDATE web_searches SET status='FAILED',error=?,updated=? WHERE id=?",(error,db.now(),sid));return
                await asyncio.sleep(2**(s['attempts']-1))
