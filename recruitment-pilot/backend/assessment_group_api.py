import json
from typing import Literal
from fastapi import APIRouter, HTTPException, Query
from .models import Strict
from .errors import IntegrationError
from . import assessment_groups as service, db, content_fetch
router = APIRouter(prefix='/api/v1', tags=['All public sources'])

class ReviewLabel(Strict):
    label:Literal['CONTACT','VERIFY','UNSUITABLE']
    reason:str
    criterion_ids:list[str]=[]
    expected_version:int=0

class ReviewControl(Strict):
    url:str
    reason:str

@router.post('/assessment-groups/{gid}/review-controls')
def review_control(gid:str,body:ReviewControl):
    from . import shortlist_review
    if not body.reason.strip():raise HTTPException(422,'Cần lý do chọn hồ sơ đối chứng.')
    try:return shortlist_review.add_control(gid,body.url.strip(),body.reason.strip())
    except IntegrationError as error:raise HTTPException(422,str(error))

@router.get('/assessment-groups/{gid}/blind-review')
def blind_review(gid:str):
    from . import shortlist_review
    return shortlist_review.blind(gid)

@router.put('/assessment-groups/{gid}/blind-review/{cid}')
def review_label(gid:str,cid:str,body:ReviewLabel):
    from . import shortlist_review
    if not body.reason.strip():raise HTTPException(422,'Cần ghi lý do review.')
    report=service.detail(gid)
    if set(body.criterion_ids)-{c['id'] for c in report['criteria']}:raise HTTPException(422,'Sai tiêu chí của lượt.')
    try:return shortlist_review.label(gid,cid,body.label,body.reason.strip(),body.criterion_ids,body.expected_version)
    except IntegrationError as error:raise HTTPException(409,str(error))

@router.get('/assessment-groups/{gid}/review-metrics')
def review_metrics(gid:str):
    from . import shortlist_review
    return shortlist_review.metrics(gid)

class Request(Strict):
    config_version: int
    assessment_engine:Literal["CURRENT","JEV"]="CURRENT"

@router.get('/assessment-groups/{gid}/continuation-preview')
def continuation_preview(gid:str):
    try:return service.continuation_preview(gid)
    except KeyError:raise HTTPException(404,'Không tìm thấy nhóm đánh giá.')

@router.post('/assessment-groups/{gid}/continue',status_code=202)
def continue_group(gid:str):
    try:return service.continue_group(gid)
    except KeyError:raise HTTPException(404,'Không tìm thấy nhóm đánh giá.')

class BudgetRequest(Strict):
    limit_dollars:float
    expected_limit_dollars:float
    confirm:Literal[True]

@router.put('/jev-trial-budget')
def budget(body:BudgetRequest):
    from . import jev,engine
    import math
    with db.LOCK:
        current=jev.trial_budget()['limit_dollars']
        if current!=body.expected_limit_dollars:raise HTTPException(409,'Ngân sách đã thay đổi; tải lại trước khi xác nhận.')
        if not math.isfinite(body.limit_dollars) or body.limit_dollars<=current:
            raise HTTPException(422,'Ngân sách mới phải lớn hơn ngân sách hiện tại.')
        db.set_setting('jev_trial_limit_nano',round(body.limit_dollars*1e9))
    return jev.trial_budget()

@router.post('/people-searches/{sid}/assessment-groups', status_code=202)
def create(sid: str, body: Request):
    try: return service.create(sid, body.config_version, body.assessment_engine)
    except KeyError: raise HTTPException(404, 'Không tìm thấy nhóm People.')

@router.get('/people-searches/{sid}/assessment-groups')
def history(sid: str):
    return [{**{k:r[k] for k in ('id','status','created')},**{k:json.loads(r['snapshot']).get(k,'CURRENT' if k=='assessment_engine' else None) for k in ('assessment_engine','model','adapter_version')}} for r in db.rows('SELECT id,status,created,snapshot FROM assessment_groups WHERE search_id=? ORDER BY created DESC',(sid,))]

@router.get('/people-searches/{sid}/assessment-group-preview')
def preview(sid: str, config_version: int, assessment_engine:Literal["CURRENT","JEV"]="CURRENT"):
    try: return service.preview(sid, config_version, assessment_engine)
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
