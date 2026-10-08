from fastapi import APIRouter, HTTPException, Query
from .models import Strict
from . import assessment_groups as service, db, content_fetch
router = APIRouter(prefix='/api/v1', tags=['All public sources'])

class Request(Strict):
    config_version: int

@router.post('/people-searches/{sid}/assessment-groups', status_code=202)
def create(sid: str, body: Request):
    try: return service.create(sid, body.config_version)
    except KeyError: raise HTTPException(404, 'Không tìm thấy nhóm People.')

@router.get('/people-searches/{sid}/assessment-groups')
def history(sid: str):
    return db.rows('SELECT id,status,created FROM assessment_groups WHERE search_id=? ORDER BY created DESC', (sid,))

@router.get('/people-searches/{sid}/assessment-group-preview')
def preview(sid: str, config_version: int):
    try: return service.preview(sid, config_version)
    except KeyError: raise HTTPException(404, 'Không tìm thấy nhóm People.')

@router.get('/assessment-groups/{gid}')
def detail(gid: str):
    try: return service.detail(gid)
    except KeyError: raise HTTPException(404, 'Không tìm thấy nhóm đánh giá.')

@router.get('/assessment-groups/{gid}/results')
def results(gid: str, limit: int = Query(10, ge=1)):
    try: return service.detail(gid, limit)
    except KeyError: raise HTTPException(404, 'Không tìm thấy nhóm đánh giá.')

@router.post('/assessment-groups/{gid}/retry', status_code=202)
def retry(gid: str):
    try: return service.retry(gid)
    except KeyError: raise HTTPException(404, 'Không tìm thấy nhóm đánh giá.')

@router.get('/assessment-groups/{gid}/sources/{source_id}')
def source(gid: str, source_id: str):
    try: value = service.row(gid)
    except KeyError: raise HTTPException(404, 'Không tìm thấy nhóm đánh giá.')
    from .engine import digest
    url = next((s['url'] for s in value['snapshot']['sources'] if 'SRC'+digest(s['url'])[:24] == source_id), None)
    if not url: raise HTTPException(404, 'Nguồn không thuộc nhóm.')
    for chunk in value['data']['chunks']:
        if url in chunk['urls'] and chunk['fetch_id']:
            return next(s for s in content_fetch.detail(chunk['fetch_id'])['results'] if s['url'] == url)
    raise HTTPException(409, 'Chưa lấy nội dung nguồn này.')
