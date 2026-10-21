from fastapi import APIRouter,HTTPException
from fastapi.responses import Response
from . import pipeline,db
router=APIRouter(prefix='/api/v1/pipeline-runs',tags=['Pipeline trace'])
def check_blind(origin):
    import json
    s=db.one('SELECT snapshot FROM searches WHERE id=?',(origin,))
    if s:
        rid=json.loads(s['snapshot']).get('inbound_review_set')
        if rid:
            from .inbound_review import row
            if not row(rid)['revealed']:raise HTTPException(409,'Hoàn tất review mù trước khi mở trace kết quả AI.')

def check_run(rid):
    r=db.one('SELECT origin_id FROM pipeline_runs WHERE id=?',(rid,))
    if r:check_blind(r['origin_id'])
@router.get('/by-origin/{kind}/{origin}')
def by_origin(kind:str,origin:str):
    check_blind(origin)
    from . import pipeline_history
    try:
        loader={'content-fetch':pipeline_history.content,'assessment':pipeline_history.assessment,'assessment-group':pipeline_history.group,'people-search':pipeline_history.people}.get(kind)
        rid=loader(origin) if loader else None
        if rid is None:raise KeyError(kind)
        return pipeline.manifest(rid)
    except KeyError:raise HTTPException(404,'Không tìm thấy origin của pipeline.')
@router.get('/{rid}')
def detail(rid:str):
    check_run(rid)
    try:return pipeline.manifest(rid)
    except KeyError:raise HTTPException(404,'Không tìm thấy pipeline run.')
@router.get('/{rid}/artifacts/{aid}')
def artifact(rid:str,aid:str):
    check_run(rid)
    try:return pipeline.read_artifact(rid,aid)
    except KeyError:raise HTTPException(404,'Artifact không thuộc lượt này.')
@router.get('/{rid}/export')
def export(rid:str):
    check_run(rid)
    try:data=pipeline.export(rid)
    except KeyError:raise HTTPException(404,'Không tìm thấy pipeline run.')
    return Response(db.dumps(data),media_type='application/json',headers={'Content-Disposition':f'attachment; filename="pipeline-{data["manifest"]["id"]}.json"','Cache-Control':'no-store'})
