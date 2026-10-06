from fastapi import APIRouter,HTTPException
from .models import Strict
from . import public_assessment as service
router=APIRouter(prefix='/api/v1',tags=['Public source assessment'])
class Request(Strict):
    job_id:str
    config_version:int
    search_id:str
@router.get('/public-assessments')
def history(job_id:str,search_id:str):
    from . import db
    return [service.detail(r['id']) for r in db.rows('SELECT id FROM public_assessments WHERE job_id=? AND search_id=? ORDER BY created DESC',(job_id,search_id))]
@router.post('/public-assessments',status_code=202)
def create(body:Request):
    try:return service.create(body.job_id,body.config_version,body.search_id)
    except KeyError:raise HTTPException(404,'Không tìm thấy lượt tìm nguồn.')
@router.get('/public-assessments/{aid}')
def detail(aid:str):
    try:return service.detail(aid)
    except KeyError:raise HTTPException(404,'Không tìm thấy lượt đánh giá.')
@router.post('/public-assessments/{aid}/retry',status_code=202)
def retry(aid:str):
    try:return service.retry(aid)
    except KeyError:raise HTTPException(404,'Không tìm thấy lượt đánh giá.')
