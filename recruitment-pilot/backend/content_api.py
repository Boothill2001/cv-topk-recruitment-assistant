import json,uuid
from typing import Literal
from fastapi import APIRouter,HTTPException,UploadFile,File,Form
from fastapi.responses import Response
from pydantic import Field
from . import content_fetch,db,public_assessment
from .models import Strict
router=APIRouter(prefix='/api/v1',tags=['Public content'])

class FetchRequest(Strict):
    source_urls:list[str]=Field(min_length=1,max_length=20)
    refresh:bool=False

@router.post('/people-searches/{sid}/content-fetches',status_code=202)
def create(sid:str,body:FetchRequest):
    try:return content_fetch.create(sid,body.source_urls,body.refresh)
    except KeyError:raise HTTPException(404,'Không tìm thấy lượt People.')

@router.get('/people-searches/{sid}/content-fetches')
def history(sid:str):
    return db.rows('SELECT id,status,created FROM content_fetches WHERE search_id=? ORDER BY created DESC',(sid,))

@router.get('/content-fetches/{fid}')
def detail(fid:str):
    try:return content_fetch.detail(fid)
    except KeyError:raise HTTPException(404,'Không tìm thấy lượt lấy nội dung.')

@router.post('/content-fetches/{fid}/retry',status_code=202)
def retry(fid:str):return content_fetch.retry(fid)

@router.post('/content-fetches/{fid}/profile-document',status_code=201)
async def document(fid:str,source_url:str=Form(...),file:UploadFile=File(...)):
    import asyncio
    from .integrations import extract
    from . import engine
    row=content_fetch.detail(fid)
    if row['status'] in ('PENDING','RUNNING'):raise HTTPException(409,'Chờ lấy nội dung hoàn tất.')
    if source_url not in {s['url'] for s in row['results']}:raise HTTPException(409,'Nguồn không thuộc lượt này.')
    raw=await file.read(15*1024*1024+1)
    if len(raw)>15*1024*1024:raise HTTPException(413,'PDF tối đa 15 MB.')
    if not raw.startswith(b'%PDF-'):raise HTTPException(400,'Chỉ nhận PDF hồ sơ.')
    text=await asyncio.to_thread(extract,raw,'application/pdf')
    if not text.strip():raise HTTPException(400,'PDF chưa có chữ; cần OCR trước khi bổ sung.')
    items=[dict(s) for s in row['results']]
    item=next(s for s in items if s['url']==source_url)
    item.update(status='AVAILABLE',text=text,paragraphs=content_fetch.blocks(text),revision=engine.digest(text),
                content_hash=engine.digest(text),fetched_at=db.now(),provider_source='user-provided-pdf',
                warnings=['PDF do người dùng cung cấp; cần xác minh đúng người và độ cập nhật.'],adapter_version=content_fetch.VERSION)
    snapshot={**row['snapshot'],'document_revision':engine.digest(text),'document_url':source_url}
    fp=engine.digest(db.dumps(snapshot));old=db.one('SELECT id FROM content_fetches WHERE fingerprint=?',(fp,))
    if old:return content_fetch.detail(old['id'])
    nid=str(uuid.uuid4());now=db.now();state='COMPLETED' if all(s['status']=='AVAILABLE' for s in items) else 'PARTIAL'
    db.execute('INSERT INTO content_fetches VALUES(?,?,?,?,?,?,?,?,?,?)',(nid,fp,row['search_id'],db.dumps(snapshot),state,
        db.dumps({'results':items,'usage':[],'known_cost_dollars':0,'has_unreported_cost':False}),0,None,now,now))
    return content_fetch.detail(nid)

@router.get('/public-assessments/{aid}/export')
def export(aid:str):
    value=public_assessment.detail(aid);snap=value['input_snapshot']
    # Allowlisted export, excluding secrets, private job notes and runtime settings.
    data={k:value.get(k) for k in ('id','status','provider','model','prompt_version','source_kind','results','usage','error')}
    data['input']={k:snap.get(k) for k in ('public_jd','criteria','sources','job_revision','content_fetch_id','scorer_version')}
    return Response(json.dumps(data,ensure_ascii=False,indent=2),media_type='application/json',headers={'Content-Disposition':f'attachment; filename="assessment-{aid}.json"'})

class Label(Strict):
    source_id:str
    decision:Literal['GOOD','NOT_GOOD','UNCERTAIN']
    evidence_error:bool=False
    note:str=Field(default='',max_length=5000)

@router.post('/public-assessments/{aid}/labels',status_code=201)
def label(aid:str,body:Label):
    report=public_assessment.detail(aid)
    if report['status']!='COMPLETED' or body.source_id not in {s['candidate_id'] for s in report['results']}:
        raise HTTPException(409,'Chỉ gán nhãn nguồn đã đánh giá trong lượt này.')
    lid=str(uuid.uuid4())
    db.execute('INSERT INTO public_source_labels VALUES(?,?,?,?,?,?,?)',(lid,aid,body.source_id,body.decision,int(body.evidence_error),body.note,db.now()))
    return {'id':lid}

@router.get('/public-assessments/{aid}/benchmark')
def benchmark(aid:str):
    report=public_assessment.detail(aid)
    rows=db.rows('SELECT * FROM public_source_labels WHERE assessment_id=? ORDER BY created',(aid,))
    latest={r['source_id']:r for r in rows};known={k:v for k,v in latest.items() if v['decision']!='UNCERTAIN'}
    top=[r['candidate_id'] for r in report.get('results',[])[:10]]
    labeled_top=[known[k] for k in top if k in known]
    return {'status':'PILOT_REVIEW' if known else 'INSUFFICIENT_LABELS','policy_benchmarked':False,
            'labeled_sources':len(latest),'confirmed_sources':len(known),
            'precision_on_labeled_top10':sum(r['decision']=='GOOD' for r in labeled_top)/len(labeled_top) if labeled_top else None,
            'evidence_error_count':sum(r['evidence_error'] for r in latest.values()),
            'retrieval_recall':None,'note':'Không đo recall toàn web từ các nguồn đã tìm. Cần tập người tham khảo do khách xác nhận ngoài nhóm.'}
