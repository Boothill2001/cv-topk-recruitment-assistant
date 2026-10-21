"""Lazy projections of saved business records, never new provider work."""
import json
from . import db,pipeline
from .errors import IntegrationError

def content(fid,legacy=True):
    row=db.one('SELECT * FROM content_fetches WHERE id=?',(fid,))
    if not row:raise KeyError(fid)
    data=json.loads(row['data']);snap=json.loads(row['snapshot'])
    rid=pipeline.run('content-fetch',fid,{'source_created':row['created'],'source_updated':row['updated'],'search_id':row['search_id'],'status':row['status']},legacy)
    for item in data['results']:
        def validate(value):
            if value.get('status')=='AVAILABLE':
                if not value.get('text') or not value.get('revision'):raise IntegrationError('Thiếu text/revision nguồn.')
                from .engine import digest
                import re
                if (not legacy or re.fullmatch('[0-9a-f]{64}',str(value['revision']))) and digest(value['text'])!=value['revision']:raise IntegrationError('Revision nội dung không khớp.')
        pipeline.capture(rid,'content:'+item['url'],{'url':item['url'],'search_id':row['search_id']},
          {'adapter':snap.get('adapter_version'),'source_revision':item.get('revision'),'source_updated':row['updated'],'legacy':legacy},'source-content',item,validate,
          usage={'ledger_ref':{'kind':'content_fetch','id':fid},'cached_live':item.get('provider_source','unknown'),
            'shared_fetch_requests':data.get('usage',[]),'cost_allocation':'Shared fetch cost, not a per-source invoice.'},
          error={'code':'SOURCE_UNAVAILABLE','message':item.get('error','Chưa có text')} if item['status'] not in ('AVAILABLE','PENDING') else None)
    return rid

def content_results(fid):
    rid=content(fid)
    result=[]
    # Only the latest observation per source is eligible for downstream consumption.
    rows=db.rows("SELECT * FROM node_attempts WHERE run_id=? ORDER BY attempt_no DESC",(rid,))
    seen=set()
    for row in rows:
        if row['node_id'] in seen:continue
        seen.add(row['node_id'])
        value=pipeline.output(row['id'],'source-content')
        if value is not None:result.append(pipeline.thaw(value))
    return result

def assessment(aid):
    row=db.one('SELECT * FROM public_assessments WHERE id=?',(aid,))
    internal=False
    if not row:
        row=db.one('SELECT * FROM searches WHERE id=?',(aid,));internal=True
    if not row:raise KeyError(aid)
    snap=json.loads(row['snapshot']);jev_snap=snap.get('jev',snap)
    rid=pipeline.run('assessment',aid,{'status':row['status'],'source_created':row['created'],'source_updated':row['updated'],
      'engine':snap.get('assessment_engine','CURRENT'),'model':row.get('model') or snap.get('model'),'missing_history':'Raw provider attempts may not have been retained by the legacy engine.'},True)
    safe={k:jev_snap[k] for k in ('public_jd','criteria','sources','job_revision','config','model','adapter_version','prompt_hash','policy','rubric') if k in jev_snap}
    if internal:safe['job']={'id':row['job_id'],'revision':snap['job']['revision'],'config_version':row['config_version']}
    pipeline.capture(rid,'snapshot',{}, {'origin':'saved-snapshot'},'assessment-snapshot',safe)
    if jev_snap.get('content_fetch_id') and jev_snap['content_fetch_id']!='stored-cv':
        try:
            cid=content(jev_snap['content_fetch_id'])
            pipeline.capture(rid,'content-link',{}, {},'upstream-run',{'pipeline_run_id':cid})
        except KeyError:pass
    from . import jev
    for step in db.rows('SELECT * FROM jev_steps WHERE assessment_id=? ORDER BY created,id',(aid,)):
        payload=json.loads(step['request']);response=json.loads(step['response']) if step['response'] else None
        # Legacy step projections have distinct IDs; never overwrite native executions.
        pipeline.capture(rid,'history:'+step['id'],{'request':payload},
          {'adapter':jev.VERSION,'source_created':step['created'],'source_updated':step['updated'],'legacy':True},
          'legacy-provider-step',{'response':response,'status':step['status'],'attempts':step['attempts'],
             'source_id':step['source_id'],'phase':step['phase'],'ledger_ref':step['id'],'missing_response':response is None},
          error={'code':step['status'],'message':step['error']} if step['error'] else None)
    if internal:
        results=[json.loads(r['data']) for r in db.rows('SELECT data FROM search_assessments WHERE search_id=?',(aid,))]
    else:results=json.loads(row['data']).get('results',[])
    pipeline.capture(rid,'stored-results',{}, {'scorer':row.get('scorer_version') or snap.get('scorer_version'),'source_status':row['status']},'saved-results',{'results':results,'status':row['status'],'legacy':True})
    return rid

def group(gid):
    row=db.one('SELECT * FROM assessment_groups WHERE id=?',(gid,))
    if not row:raise KeyError(gid)
    snap=json.loads(row['snapshot']);data=json.loads(row['data'])
    rid=pipeline.run('assessment-group',gid,{'job_id':row['job_id'],'config_version':row['config_version'],
      'model':snap.get('model'),'engine':snap.get('assessment_engine','CURRENT'),'search_id':row['search_id']},True)
    for chunk in data.get('chunks',[]):
        if chunk.get('fetch_id'):
            child=content(chunk['fetch_id'])
            pipeline.capture(rid,'fetch:'+chunk['fetch_id'],{}, {},'upstream-run',{'pipeline_run_id':child})
    for batch in data.get('batches',[]):
        if batch.get('assessment_id'):
            child=assessment(batch['assessment_id'])
            pipeline.capture(rid,'assessment:'+batch['assessment_id'],{}, {},'upstream-run',{'pipeline_run_id':child})
    return rid

def people(sid):
    row=db.one('SELECT * FROM people_searches WHERE id=?',(sid,))
    if not row:raise KeyError(sid)
    snap=json.loads(row['snapshot'])
    rid=pipeline.run('people-search',sid,{'job_id':row['job_id'],'source_created':row['created'],
      'snapshot':snap},True)
    pipeline.capture(rid,'search-snapshot',{}, {'origin':'saved-snapshot'},
      'people-search-snapshot',snap)
    # Project persisted Exa responses, never reconstruct provider calls or timings.
    from . import people_search
    saved=people_search.detail(sid)
    for item in saved['items']:
        search=item['search']
        pipeline.capture(rid,'exa-search:'+search['id'],
          {'query':search.get('effective_query') or item['strategy'].get('effective_query'),
           'strategy':item['strategy']},
          {'adapter':snap.get('adapter_version'),'legacy':True,'saved_hash':pipeline.digest(pipeline.canonical(search))},
          'saved-exa-search',search,
          error={'code':search['status'],'message':search['error']} if search.get('error') else None)
    pipeline.capture(rid,'discovered-sources',{}, {'legacy':True,'saved_hash':pipeline.digest(pipeline.canonical(saved['results'])),'source_status':saved['status']},'saved-discovered-sources',
      {'status':saved['status'],'results':saved['results'],
       'known_cost_dollars':saved['known_cost_dollars'],
       'has_unreported_cost':saved['has_unreported_cost']})
    for child in db.rows('SELECT id FROM assessment_groups WHERE search_id=?',(sid,)):
        cid=group(child['id'])
        pipeline.capture(rid,'group:'+child['id'],{}, {},'upstream-run',{'pipeline_run_id':cid})
    for child in db.rows('SELECT id FROM public_assessments WHERE search_id=?',(sid,)):
        cid=assessment(child['id'])
        pipeline.capture(rid,'assessment:'+child['id'],{}, {},'upstream-run',{'pipeline_run_id':cid})
    return rid
