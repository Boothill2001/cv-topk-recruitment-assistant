"""Durable known-URL extraction. Text is available provider content, never a verified full profile."""
import asyncio,json,re,uuid
from datetime import datetime,timedelta,timezone
import httpx
from . import db,engine,exa_search
from .errors import IntegrationError

VERSION='public-content-3'
SQL='''CREATE TABLE IF NOT EXISTS content_fetches (
 id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,search_id TEXT NOT NULL,snapshot TEXT NOT NULL,
 status TEXT NOT NULL,data TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,error TEXT,created TEXT NOT NULL,updated TEXT NOT NULL);
 CREATE INDEX IF NOT EXISTS content_fetch_fingerprint ON content_fetches(fingerprint);
 CREATE TABLE IF NOT EXISTS public_source_labels (
 id TEXT PRIMARY KEY,assessment_id TEXT NOT NULL,source_id TEXT NOT NULL,decision TEXT NOT NULL,
 evidence_error INTEGER NOT NULL,note TEXT NOT NULL,created TEXT NOT NULL);
'''

def search_detail(sid):
    from . import people_search
    try:return people_search.detail(sid)
    except KeyError:
        value=exa_search.detail(sid)
        if value['mode']!='people':raise IntegrationError('Chỉ lấy nội dung nguồn People.')
        return value

def detail(fid):
    row=db.one('SELECT * FROM content_fetches WHERE id=?',(fid,))
    if not row:raise KeyError(fid)
    return {**row,'snapshot':json.loads(row['snapshot']),**json.loads(row.pop('data'))}

def blocks(text):
    # Stable paragraph IDs; preserve all input text and conservative subject flags.
    parts=re.split(r'\n\s*\n',text);result=[];section='professional'
    for part in parts:
        if not part.strip():continue
        if re.search(r'^#+\s*(Social|Research Topics|Publications)\b',part,re.I|re.M):section='context_only'
        if re.search(r'^#+\s*(Experience|Education|Skills|Languages|About|Licenses)\b',part,re.I|re.M):section='professional'
        kind='context_only' if section=='context_only' or re.search(r'\bis a[n]? .*?company\b|\bemployees\s*\(|reposted this|liked this',part,re.I) else 'professional'
        while part:
            end=min(3000,len(part))
            if end<len(part):
                space=part.rfind(' ',0,end)
                if space>1500:end=space+1
            piece=part[:end];part=part[end:]
            result.append({'id':'P'+engine.digest(piece)[:16],'text':piece,'kind':kind})
    # Duplicate paragraphs share one immutable evidence reference.
    unique=list({p['id']:p for p in result}.values())
    return [{**p,'content_hash':p['id'][1:],'id':f'P{i:03d}'} for i,p in enumerate(unique,1)]

@engine.serialized
def create(sid,urls,refresh=False,*,enqueue=True):
    search=search_detail(sid)
    if search['status'] not in ('COMPLETED','PARTIAL'):raise IntegrationError('Chờ lượt tìm People hoàn tất.')
    sources={s['url']:s for s in search.get('results',[])}
    if not urls or len(urls)!=len(set(urls)) or len(urls)>20 or not set(urls)<=set(sources):
        raise IntegrationError('Chọn 1–20 URL khác nhau thuộc lượt tìm này.')
    if not all(exa_search.safe_url(u) for u in urls):raise IntegrationError('URL nguồn không hợp lệ.')
    snapshot={'search_id':sid,'sources':[sources[u] for u in sorted(urls)],'adapter_version':VERSION,
              'search_snapshot':search.get('snapshot'),'location_scope':search.get('location_scope')}
    fp=engine.digest(db.dumps(snapshot));cutoff=(datetime.now(timezone.utc)-timedelta(hours=24)).isoformat()
    with db.LOCK,db.conn() as conn:
        old=conn.execute("SELECT id,status,data FROM content_fetches WHERE fingerprint=? AND (status IN ('PENDING','RUNNING') OR created>=?) ORDER BY created DESC",(fp,cutoff)).fetchone()
        fresh=old and all(i.get('status')!='AVAILABLE' or i.get('fetched_at','')>=cutoff for i in json.loads(old['data']).get('results',[]))
        if old and (old['status'] in ('PENDING','RUNNING') or (not refresh and fresh)):return detail(old['id'])
        cached={}
        if not refresh:
            for row in db.rows("SELECT data FROM content_fetches WHERE updated>=? ORDER BY updated DESC",(cutoff,)):
                for item in json.loads(row['data']).get('results',[]):
                    if item.get('status')=='AVAILABLE' and item.get('fetched_at','')>=cutoff and item.get('adapter_version')==VERSION and item.get('provider_source')!='user-provided-pdf':cached.setdefault(item['url'],item)
        items=[cached.get(u,{'url':u,'title':sources[u].get('title',''),'status':'PENDING'}) for u in sorted(urls)]
        fid=str(uuid.uuid4());now=db.now()
        conn.execute('INSERT INTO content_fetches VALUES(?,?,?,?,?,?,?,?,?,?)',(fid,fp,sid,db.dumps(snapshot),'PENDING',db.dumps({'results':items,'usage':[]}),0,None,now,now))
        if enqueue:conn.execute('INSERT INTO tasks(kind,payload,status,created,updated) VALUES(?,?,?,?,?)',('content_fetch',db.dumps({'fetch_id':fid}),'PENDING',now,now))
    return detail(fid)

@engine.serialized
def retry(fid):
    row=detail(fid)
    if row['status'] in ('PENDING','RUNNING','COMPLETED'):return row
    if row['attempts']>=3:raise IntegrationError('Đã hết 3 lượt thử lấy nội dung; kiểm tra lỗi trước khi làm mới.')
    with db.LOCK,db.conn() as conn:
        conn.execute("UPDATE content_fetches SET status='PENDING',error=NULL WHERE id=?",(fid,))
        conn.execute('INSERT INTO tasks(kind,payload,status,created,updated) VALUES(?,?,?,?,?)',('content_fetch',db.dumps({'fetch_id':fid}),'PENDING',db.now(),db.now()))
    return detail(fid)

async def run(fid):
    async with exa_search.SEMAPHORE:
        row=detail(fid)
        if row['status']=='COMPLETED':return
        items=row['results'];usage=row['usage'];attempts=row['attempts']
        with db.LOCK,db.conn() as connection:
            claimed=connection.execute("UPDATE content_fetches SET status='RUNNING',updated=? WHERE id=? AND status IN ('PENDING','FAILED','PARTIAL','INTERRUPTED') AND attempts<3 RETURNING id",(db.now(),fid)).fetchone()
        if not claimed:return
        pending=[i['url'] for i in items if i['status']!='AVAILABLE']
        while pending and attempts<3:
            attempts+=1;db.execute('UPDATE content_fetches SET attempts=? WHERE id=?',(attempts,fid))
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(60,connect=15)) as client:
                    response=await client.post('https://api.exa.ai/contents',headers={'x-api-key':exa_search.api_key()},json={'urls':pending,'text':True})
                    response.raise_for_status();raw=response.json()
                cost=exa_search.reported_cost(raw.get('costDollars'));usage.append({'request_id':raw.get('requestId'),'cost_dollars':cost,'created':db.now()})
                texts={v.get('url',v.get('id')):v for v in raw.get('results',[])}
                statuses={v.get('id',v.get('url')):v for v in raw.get('statuses',[])}
                again=[]
                for item in items:
                    if item['url'] not in pending:continue
                    url=item['url'];status=statuses.get(url,{});result=texts.get(url,{})
                    text=result.get('text','');tag=status.get('error',{})
                    if status.get('status')=='success' and isinstance(text,str) and text.strip():
                        revision=engine.digest(text);paragraphs=blocks(text)
                        warnings=['Nội dung provider lấy được; chưa xác minh đầy đủ so với trang gốc.']
                        if '…' in text or '...' in text:warnings.append('Có dấu rút gọn; cần kiểm tra độ đầy đủ.')
                        if any(p['kind']=='context_only' for p in paragraphs):warnings.append('Có phần ngữ cảnh công ty/bài chia sẻ; không dùng làm năng lực cá nhân.')
                        item.update(status='AVAILABLE',text=text,paragraphs=paragraphs,revision=revision,content_hash=revision,
                                    fetched_at=db.now(),provider_source=status.get('source','unknown'),warnings=warnings,adapter_version=VERSION,provider_status=status,title=result.get('title',item.get('title','')))
                    else:
                        item.update(status='UNAVAILABLE',error='Nguồn không có text khả dụng; không coi là full profile.',provider_status=status)
                        if re.search(r'timeout|rate|500|temporar',str(tag),re.I):again.append(url)
                pending=again
            except Exception as exc:
                retryable=isinstance(exc,(httpx.TimeoutException,httpx.NetworkError)) or isinstance(exc,httpx.HTTPStatusError) and (exc.response.status_code==429 or exc.response.status_code>=500)
                usage.append({'request_id':None,'cost_dollars':None,'created':db.now(),'error':'Lỗi kết nối/provider; phí chưa xác định.'})
                for item in items:
                    if item['url'] in pending:item.update(status='UNAVAILABLE',error='Lấy nội dung thất bại; kiểm tra kết nối, quyền hoặc quota.')
                if not retryable:pending=[]
            db.execute('UPDATE content_fetches SET data=?,updated=? WHERE id=?',(db.dumps({'results':items,'usage':usage}),db.now(),fid))
            if pending and attempts<3:await asyncio.sleep(2**(attempts-1))
        good=sum(i['status']=='AVAILABLE' for i in items)
        state='COMPLETED' if good==len(items) else 'PARTIAL' if good else 'FAILED'
        data={'results':items,'usage':usage,'known_cost_dollars':sum(u['cost_dollars'] or 0 for u in usage),'has_unreported_cost':any(u['cost_dollars'] is None for u in usage)}
        db.execute('UPDATE content_fetches SET status=?,data=?,updated=? WHERE id=?',(state,db.dumps(data),db.now(),fid))
