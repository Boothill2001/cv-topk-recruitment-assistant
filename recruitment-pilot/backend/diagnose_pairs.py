"""Validate Gemini's reference pairs without treating them as ground truth."""
import asyncio,json
from . import db,engine
from .models import Evaluation
from .integrations import ai,check_ai

async def main():
    db.RUNTIME=db.RUNTIME/'validation';db.init()
    if not (await check_ai())['ok']:raise RuntimeError('DeepSeek not ready')
    run=db.one("SELECT * FROM runs WHERE job_id='IT-599' ORDER BY created DESC LIMIT 1")
    prior=db.unpack(db.one('SELECT * FROM evaluations WHERE run_id=? AND candidate_id=?',(run['id'],'CV429')))
    output=[{'candidate_id':'CV429','job_id':'IT-599','status':prior['status'],'data':prior['data']}]
    config=json.loads(db.one("SELECT data FROM configs WHERE job_id='IT-595c'")['data'])
    candidate={'profile':json.loads(db.one("SELECT data FROM profiles WHERE id='CV413' AND kind='candidate'")['data']),
               'sources':engine.sources('CV413','candidate')}
    job={'profile':json.loads(db.one("SELECT data FROM profiles WHERE id='IT-595c' AND kind='job'")['data']),
         'sources':engine.sources('IT-595c','job')}
    criteria=[c for c in config['criteria'] if c['enabled']]
    def validate(data):
        if sorted(a['criterion_id'] for a in data['assessments'])!=sorted(c['id'] for c in criteria):raise engine.IntegrationError('Sai criteria.')
        for a in data['assessments']:
            if a['status']!='UNKNOWN' and not a['evidence']:raise engine.IntegrationError('Thiếu evidence.')
            engine.validate_evidence(a['evidence'],candidate['sources'])
    try:
        result=await ai('evaluation',{'criteria':criteria,'strategies':[s for s in config['strategies'] if s['enabled']],
                                     'candidate':candidate,'job':job},Evaluation,validate)
        result.update(engine.calculate(config,result))
        output.append({'candidate_id':'CV413','job_id':'IT-595c','status':'COMPLETED','data':result})
    except Exception as e:output.append({'candidate_id':'CV413','job_id':'IT-595c','status':'FAILED','error':str(e)})
    (db.RUNTIME/'pairs.json').write_text(db.dumps(output),encoding='utf-8')
    print(db.dumps([{'candidate_id':r['candidate_id'],'job_id':r['job_id'],'status':r['status'],
                     'score':(r.get('data') or {}).get('score'),'must_complete':(r.get('data') or {}).get('must_complete')} for r in output]),flush=True)

if __name__=='__main__':asyncio.run(main())
