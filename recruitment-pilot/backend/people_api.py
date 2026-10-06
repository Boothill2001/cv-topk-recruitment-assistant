from fastapi import APIRouter,HTTPException
from pydantic import Field
from . import people_search,engine,db
from .models import Strict
from .location_scope import Scope
router=APIRouter(prefix='/api/v1',tags=['People retrieval'])
class Selection(Strict):
    config_version:int
    strategy_ids:list[str]=Field(min_length=1,max_length=20)
    refresh:bool=False
    location_scope:Scope|None=None
class Version(Strict):config_version:int
@router.post('/jobs/{job_id}/people-searches',status_code=202)
def create(job_id:str,body:Selection):return people_search.create(job_id,body.config_version,body.strategy_ids,body.refresh,body.location_scope)
@router.get('/people-searches/{gid}')
def detail(gid:str):
    try:return people_search.detail(gid)
    except KeyError:raise HTTPException(404,'Không tìm thấy nhóm Exa People.')
@router.get('/jobs/{job_id}/people-searches')
def history(job_id:str):return db.rows('SELECT id,job_id,created FROM people_searches WHERE job_id=? ORDER BY created DESC LIMIT 30',(job_id,))
@router.post('/jobs/{job_id}/exa-queries',status_code=202)
def draft(job_id:str,body:Version):
    engine.checked_config(job_id,body.config_version)
    return {'task_id':engine.enqueue('exa_queries',{'job_id':job_id,'version':body.config_version})}
