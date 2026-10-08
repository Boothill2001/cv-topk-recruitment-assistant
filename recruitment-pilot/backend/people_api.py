from typing import Literal
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
    search_type:Literal['auto','deep']='auto'
class Version(Strict):
    config_version:int
    guidance_version:int|None=Field(default=None,ge=0)
@router.post('/jobs/{job_id}/people-searches',status_code=202)
def create(job_id:str,body:Selection):return people_search.create(job_id,body.config_version,body.strategy_ids,body.refresh,body.location_scope,body.search_type)
@router.get('/people-searches/{gid}')
def detail(gid:str):
    try:return people_search.detail(gid)
    except KeyError:raise HTTPException(404,'Không tìm thấy nhóm Exa People.')
@router.get('/jobs/{job_id}/people-searches')
def history(job_id:str):return db.rows('SELECT id,job_id,created FROM people_searches WHERE job_id=? ORDER BY created DESC LIMIT 30',(job_id,))
@router.post('/jobs/{job_id}/exa-queries',status_code=202)
def draft(job_id:str,body:Version):
    engine.checked_config(job_id,body.config_version)
    from .scouting_guidance import capture
    with db.LOCK:
        engine.checked_config(job_id,body.config_version)
        guidance=capture(job_id,body.guidance_version,'exa_queries')
        return {'task_id':engine.enqueue('exa_queries',{'job_id':job_id,'version':body.config_version,'guidance':guidance})}
