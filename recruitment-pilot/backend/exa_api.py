from fastapi import APIRouter,HTTPException
from typing import Literal
from pydantic import Field
from .models import Strict
from . import db,exa_search
from .location_scope import Scope

router=APIRouter(prefix='/api/v1/web-searches',tags=['Exa web search'])
class SearchBody(Strict):
    query:str=Field(min_length=3,max_length=1500)
    refresh:bool=False
    mode:Literal['web','people']='web'
    location_scope:Scope|None=None
    search_type:Literal['auto','deep']='auto'

@router.get('/connection')
def connection():return {'provider':'exa','configured':exa_search.configured(),'endpoint':'search','default_results':10}

@router.get('')
def history():return db.rows('SELECT id,query,mode,search_type,location_scope,effective_query,status,attempts,error,created,updated FROM web_searches ORDER BY created DESC LIMIT 30')

@router.post('',status_code=202)
def create(body:SearchBody):return exa_search.create(body.query,body.refresh,body.mode,body.location_scope,body.search_type)

@router.get('/{sid}')
def detail(sid:str):
    try:return exa_search.detail(sid)
    except KeyError:raise HTTPException(404,'Không tìm thấy lượt tìm web.')

@router.post('/{sid}/retry',status_code=202)
def retry(sid:str):
    try:return exa_search.retry(sid)
    except KeyError:raise HTTPException(404,'Không tìm thấy lượt tìm web.')
