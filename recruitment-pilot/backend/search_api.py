from fastapi import APIRouter,HTTPException,Query,Request,UploadFile,File,Form
from fastapi.responses import StreamingResponse
import asyncio
from . import db,search_orchestration as service
from .search_schema import SearchRequest
from . import rule_review
import json
from pydantic import Field
from .models import Strict
from . import ai_assessment
from .errors import IntegrationError

router=APIRouter(prefix='/api/v1')

@router.post('/jobs/{job_id}/cv-assessments',status_code=202)
async def upload_single(job_id:str,file:UploadFile=File(...),config_version:int=Form(...),private_note:str=Form('')):
    from . import single_cv
    from .integrations import extract
    single_cv.check_job(job_id,config_version)
    if len(private_note)>30000:raise HTTPException(400,'Private note tối đa 30.000 ký tự.')
    content=await file.read(15*1024*1024+1)
    if len(content)>15*1024*1024:raise HTTPException(413,'CV PDF tối đa 15 MB.')
    if not content.startswith(b'%PDF-'):raise HTTPException(400,'Chọn CV dạng PDF có chữ.')
    try:text=await asyncio.to_thread(extract,content,'application/pdf')
    except IntegrationError as e:
        if 'NEEDS_OCR' in str(e):raise HTTPException(422,'CV scan chưa có chữ: cần OCR trước khi đánh giá.')
        raise HTTPException(400,'Không đọc được CV PDF. Kiểm tra file hoặc mật khẩu.')
    except Exception:raise HTTPException(400,'Không đọc được CV PDF. Kiểm tra file hoặc mật khẩu.')
    if len(text.strip())<20:raise HTTPException(422,'CV scan chưa có chữ: cần OCR trước khi đánh giá. Không tạo điểm 0.')
    return single_cv.register(job_id,config_version,content,text,file.filename or 'CV.pdf',private_note)

@router.get('/cv-assessments/{ticket_id}')
def single_detail(ticket_id:str):
    from . import single_cv
    try:return single_cv.detail(ticket_id)
    except KeyError:raise HTTPException(404,'Không tìm thấy lượt đánh giá CV.')

@router.post('/cv-assessments/{ticket_id}/retry',status_code=202)
def retry_single(ticket_id:str):
    from . import single_cv
    try:return single_cv.retry(ticket_id)
    except KeyError:raise HTTPException(404,'Không tìm thấy lượt đánh giá CV.')

@router.post('/searches/{search_id}/retry',status_code=202)
def retry_search(search_id:str):
    d=detail(search_id)
    if not d['is_current']:raise IntegrationError('Yêu cầu hoặc hồ sơ đã đổi; mở JD để đánh giá lại.')
    if d['attempts']>=3:raise IntegrationError('Đã hết 3 lượt thử. Kiểm tra lỗi hoặc đổi model trước khi tạo lượt mới.')
    ids=list(d['group']) if d.get('scope')=='single-cv' else None
    return {'search_id':service.create(d['job_id'],d['config_version'],d['input_count'],candidate_ids=ids)}

@router.get('/uploaded-cvs/{candidate_id}/source')
def uploaded_source(candidate_id:str):
    from fastapi.responses import FileResponse
    if not candidate_id.startswith('CV') or not candidate_id[2:].isdigit():raise HTTPException(404)
    path=db.RUNTIME/'uploads'/(candidate_id+'.pdf')
    if not path.is_file():raise HTTPException(404)
    return FileResponse(path,media_type='application/pdf')

class BudgetRequest(Strict):
    model_context_tokens:int=Field(ge=1024,le=2000000)
    model_max_output_tokens:int=Field(ge=512,le=1000000)
    output_tokens:int=Field(default=32000,ge=512,le=64000)
    context_chars:int=Field(default=250000,ge=1000,le=500000)
    max_cells:int=Field(default=300,ge=1,le=500)
    public_context_chars:int=Field(default=100000,ge=1000,le=500000)
    public_output_tokens:int=Field(default=24000,ge=512,le=64000)
    verified:bool

@router.get('/settings/assessment-budget')
def read_budget():return ai_assessment.model_budget()

@router.put('/settings/assessment-budget')
def set_budget(body:BudgetRequest):
    if not body.verified or body.output_tokens>body.model_max_output_tokens:
        raise IntegrationError('Xác nhận giới hạn model; output pilot không vượt max output của model.')
    current=ai_assessment.model_budget();caps=db.setting('assessment_budgets',{})
    caps[current['key']]=body.model_dump()|{'confirmed_at':db.now(),'source':'operator-confirmed model limits'}
    db.set_setting('assessment_budgets',caps)
    return ai_assessment.model_budget()

@router.get('/jobs/{job_id}/rule-review')
def review_rules(job_id:str):
    c=db.one('SELECT data FROM configs WHERE job_id=?',(job_id,))
    if not c:raise HTTPException(404)
    return rule_review.propose(json.loads(c['data']))

@router.post('/jobs/{job_id}/searches',status_code=202)
def create(job_id:str,body:SearchRequest):
    return {'search_id':service.create(job_id,body.config_version,body.candidate_limit)}

@router.get('/searches')
def history():
    return db.rows('SELECT id,job_id,status,config_version,provider,model,created,updated FROM searches ORDER BY created DESC')

@router.get('/searches/{search_id}')
def detail(search_id:str):
    try:return service.detail(search_id)
    except KeyError:raise HTTPException(404,'Không tìm thấy lượt tìm kiếm.')

@router.get('/searches/{search_id}/results')
def results(search_id:str,limit:int=Query(default=10,ge=1,le=20)):
    d=detail(search_id)
    return {'search_id':search_id,'status':d['status'],'total':len(d['results']),
            'results':d['results'][:limit],'is_current':d['is_current'],'ai_assessed':bool(d['results'])}

@router.get('/searches/{search_id}/events')
async def events(search_id:str,request:Request):
    detail(search_id) # Return 404 before opening stream.
    async def changes():
        previous=None;heartbeat=0
        while not await request.is_disconnected():
            state=await asyncio.to_thread(service.detail,search_id)
            body=db.dumps(state)
            if body!=previous:
                yield 'data: '+body+'\n\n';previous=body
            elif heartbeat%10==0:yield ': keepalive\n\n'
            if state['status'] not in ('PENDING','RUNNING'):break
            heartbeat+=1;await asyncio.sleep(1)
    return StreamingResponse(changes(),media_type='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

@router.get('/searches/{search_id}/benchmark')
def benchmark(search_id:str):
    from .search_benchmark import evaluate
    s=db.one('SELECT * FROM searches WHERE id=?',(search_id,))
    if not s:raise HTTPException(404)
    return evaluate(s)
