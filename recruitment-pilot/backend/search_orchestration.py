"""Persistent retrieval -> one batch assessment workflow; routes delegate here."""
import json,uuid
from . import db,engine,retrieval,ai_assessment,assessment_scoring
from .errors import IntegrationError
from .ai_runtime import redact_error
from .search_schema import BatchAssessment,JudgeBatch,JudgeCandidate
from .prompts import PROMPTS

PROMPTS['batch_assessment']=ai_assessment.PROMPT

def current(search):
    run=db.one('SELECT * FROM runs WHERE id=?',(search['retrieval_run_id'],))
    return bool(run and engine.current(run))

@engine.serialized
def create(job_id,version,limit=20,candidate_ids=None):
    run_id=engine.create_run(job_id,version,candidate_ids=candidate_ids)
    run=db.one('SELECT * FROM runs WHERE id=?',(run_id,));original=json.loads(run['snapshot'])
    rows=[db.unpack(r) for r in db.rows("SELECT * FROM evaluations WHERE run_id=? AND status='COMPLETED'",(run_id,))]
    rows.sort(key=lambda e:(-e['data'].get('retrieval_score',0),e['candidate_id']))
    chosen=rows[:limit] # Deliberately no MUST or threshold prefilter.
    group={}
    for rank,row in enumerate(chosen,1):
        eid=row['candidate_id'];p=original['candidates'][eid]
        revision=db.one("SELECT data FROM profile_revisions WHERE id=? AND kind='candidate' AND revision=?",(eid,p['revision']))
        if not revision:raise IntegrationError('Thiếu structured profile revision; không đọc lại PDF.')
        group[eid]={**p,'profile':json.loads(revision['data']),'be_rank':rank,'be_evaluation':row['data']}
    snapshot={'job':original['job'],'config':original['config'],'group':group,'pool_size':len(rows)}
    if candidate_ids is not None:snapshot['scope']='single-cv'
    provider=db.setting('provider','deepseek');model=db.setting('model','deepseek-flash')
    payload,_=ai_assessment.build(snapshot)
    snapshot['assessment_cache_key']=engine.digest(db.dumps({'payload':payload,'provider':provider,'model':model,'prompt_hash':engine.digest(ai_assessment.PROMPT)}))
    fingerprint=engine.digest(db.dumps({'snapshot':snapshot,'retrieval_run_id':run_id,'job_id':job_id,'config_version':version,'provider':provider,'model':model,'candidate_limit':limit,
        'prompt':ai_assessment.PROMPT_VERSION,'prompt_hash':engine.digest(ai_assessment.PROMPT),
        'scorer':assessment_scoring.VERSION,'retrieval':retrieval.VERSION}))
    old=db.one('SELECT * FROM searches WHERE fingerprint=?',(fingerprint,))
    if old:
        if old['status'] in ('FAILED','INTERRUPTED') and old['attempts']<3:
            db.execute("UPDATE searches SET status='PENDING',error=NULL,updated=? WHERE id=?",(db.now(),old['id']))
            engine.enqueue('batch_assessment',{'search_id':old['id']})
        return old['id']
    sid=str(uuid.uuid4());now=db.now()
    db.execute('INSERT INTO searches(id,fingerprint,job_id,retrieval_run_id,config_version,candidate_limit,snapshot,status,provider,model,prompt_version,scorer_version,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (sid,fingerprint,job_id,run_id,version,limit,db.dumps(snapshot),'PENDING',provider,model,
         ai_assessment.PROMPT_VERSION,assessment_scoring.VERSION,now,now))
    engine.enqueue('batch_assessment',{'search_id':sid})
    return sid

async def assess(sid):
    s=db.one('SELECT * FROM searches WHERE id=?',(sid,))
    if not s or s['status']=='COMPLETED':return
    try:
        if not current(s):raise IntegrationError('Input đã đổi. Mở JD để cập nhật tìm kiếm.')
        if s['prompt_version']!=ai_assessment.PROMPT_VERSION:raise IntegrationError('Prompt đã đổi. Tạo lượt tìm mới; giữ lịch sử cũ.')
        if s['provider']!=db.setting('provider','deepseek') or s['model']!=db.setting('model','deepseek-flash'):
            raise IntegrationError('Provider/model đã đổi. Tạo lượt tìm mới.')
        snapshot=json.loads(s['snapshot']);payload,sources=ai_assessment.build(snapshot)
        budget=ai_assessment.preflight(payload)
        db.execute("UPDATE searches SET status='RUNNING',error=NULL,updated=? WHERE id=?",(db.now(),sid))
        preview=[];seen=set()
        def attempt():
            preview.clear();seen.clear()
            db.execute('UPDATE searches SET attempts=attempts+1,updated=? WHERE id=?',(db.now(),sid))
            db.set_setting('search_preview:'+sid,{'items':[],'started_at':db.now()})
        def candidate(raw):
            try:
                item=JudgeCandidate.model_validate(raw).model_dump();eid=item['candidate_id']
                if eid not in snapshot['group'] or eid in seen:return False
                subset={**payload,'candidates':[p for p in payload['candidates'] if p['candidate_id']==eid]}
                expanded=ai_assessment.expand_judge({'items':[item]},subset)
                ai_assessment.validate(expanded,subset,{eid:sources[eid]})
                scored=assessment_scoring.score(snapshot['config'],expanded['items'][0],snapshot['group'][eid]['be_rank'])
                be=snapshot['group'][eid]['be_evaluation']
                scored.update(be_score=be['score'],be_retrieval_score=be.get('retrieval_score',be['score']))
                seen.add(eid);preview.append(scored)
                state=db.setting('search_preview:'+sid,{})
                db.set_setting('search_preview:'+sid,{**state,'items':preview.copy()})
                return True
            except (ValueError,IntegrationError,KeyError):return False
        def usage(value,state):
            entries=db.setting('search_usage:'+sid,[])
            db.set_setting('search_usage:'+sid,entries+[{'status':state,'provider':s['provider'],'model':s['model'],'tokens':value}])
            if state=='FAILED':db.set_setting('search_preview:'+sid,{'items':[],'retrying':True})
        from .ai_runtime import ai
        def checked(result):
            if result['items'] and 'strengths' not in result['items'][0]:
                result=ai_assessment.expand_judge(result,payload)
            ai_assessment.validate(result,payload,sources)
            # Persist validated response before finalization, so a restart can resume without a new call.
            db.set_setting('search_response:'+sid,result)
            db.set_setting('assessment_response:'+snapshot.get('assessment_cache_key',sid),result)
            db.set_setting('assessment_origin:'+snapshot.get('assessment_cache_key',sid),sid)
        result=db.setting('search_response:'+sid) or db.setting('assessment_response:'+snapshot.get('assessment_cache_key',sid))
        if result:
            ai_assessment.validate(result,payload,sources)
            budget['assessment_reused_from']=db.setting('assessment_origin:'+snapshot.get('assessment_cache_key',sid),sid)
        else:
            result=await ai('batch_assessment',payload,JudgeBatch,checked,
                            attempts_remaining=3-s['attempts'],on_attempt=attempt,on_usage=usage,max_tokens=budget['output_tokens'],on_candidate=candidate)
            if result['items'] and 'strengths' not in result['items'][0]:
                result=ai_assessment.expand_judge(result,payload)
        # Expansion creates a fresh dict; resolve exact quotes on the object that is scored/stored.
        ai_assessment.validate(result,payload,sources)
        scored=assessment_scoring.ordered([assessment_scoring.score(snapshot['config'],item,snapshot['group'][item['candidate_id']]['be_rank']) for item in result['items']])
        final_id=str(uuid.uuid4());raw=db.one('SELECT * FROM runs WHERE id=?',(s['retrieval_run_id'],))
        full=json.loads(raw['snapshot']);full['assessment_engine']=ai_assessment.PROMPT_VERSION;full['search_id']=sid
        state='COMPLETED' if current(s) else 'STALE'
        # Separate result run permits existing selected comparison/export, without overwriting BE history.
        with db.LOCK,db.conn() as c:
            c.execute('INSERT INTO runs(id,job_id,status,config_version,snapshot,model,prompt_version,created,updated,provider) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (final_id,s['job_id'],state,s['config_version'],db.dumps(full),s['model'],s['prompt_version'],db.now(),db.now(),s['provider']))
            for item in scored:
                be=snapshot['group'][item['candidate_id']]['be_evaluation']
                item['be_score']=be['score'];item['be_retrieval_score']=be.get('retrieval_score',be['score'])
                item['retrieval_score']=item['score'] # Legacy consumers order the final scores, not BE scores.
                c.execute('INSERT INTO search_assessments VALUES(?,?,?)',(sid,item['candidate_id'],db.dumps(item)))
                c.execute('INSERT INTO evaluations VALUES(?,?,?,?,?)',(final_id,item['candidate_id'],db.dumps(item),'COMPLETED',None))
            c.execute('UPDATE searches SET status=?,result_run_id=?,usage=?,updated=? WHERE id=?',
                      (state,final_id,db.dumps(budget),db.now(),sid))
    except Exception as e:
        db.set_setting('search_preview:'+sid,{'items':[]})
        db.execute('UPDATE searches SET status=?,error=?,updated=? WHERE id=?',
                   ('FAILED' if current(s) else 'STALE',redact_error(e),db.now(),sid))
        raise

def detail(sid):
    s=db.one('SELECT * FROM searches WHERE id=?',(sid,))
    if not s:raise KeyError(sid)
    snap=json.loads(s.pop('snapshot'));fresh=current(s) and s['prompt_version']==ai_assessment.PROMPT_VERSION and s['scorer_version']==assessment_scoring.VERSION
    if not fresh and s['status'] not in ('STALE',):s['status']='STALE'
    results=assessment_scoring.ordered([json.loads(r['data']) for r in db.rows('SELECT data FROM search_assessments WHERE search_id=?',(sid,))])
    be=[{'candidate_id':eid,**p['be_evaluation'],'be_rank':p['be_rank']} for eid,p in snap['group'].items()]
    return {**s,'is_current':fresh,'engine':s['prompt_version'],'input_count':len(snap['group']),
            'scope':snap.get('scope','pool'),
            'pool_size':snap['pool_size'],'config':snap['config'],'group':{eid:{'revision':p['revision'],'title':p['profile'].get('title'),'sources':p['sources']} for eid,p in snap['group'].items()},
            'steps':{'retrieval':'COMPLETED','assessment':s['status']},'results':results,
            'be_results':sorted(be,key=lambda r:r['be_rank']),
            'budget':json.loads(s['usage']) if s['usage'] else None,
            'preview':db.setting('search_preview:'+sid,{}).get('items',[]) if s['status']=='RUNNING' and fresh else [],
            'stream_supported':hasattr(__import__('backend.providers',fromlist=['adapter']).adapter(s['provider']),'generate_stream'),
            'usage':db.setting('search_usage:'+sid,[])}
