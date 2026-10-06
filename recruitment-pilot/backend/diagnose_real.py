"""Isolated end-to-end validation. Never marks customer criteria approved in pilot DB."""
import asyncio,json,shutil,sqlite3
from pathlib import Path
from . import db,engine
from .integrations import check_ai

async def main():
    live=db.RUNTIME/'pilot.sqlite3'
    dest=db.RUNTIME/'validation';dest.mkdir(exist_ok=True)
    # SQLite backup captures WAL consistently while live dashboard remains open.
    with sqlite3.connect(live) as source,sqlite3.connect(dest/'pilot.sqlite3') as target:source.backup(target)
    db.RUNTIME=dest
    db.init()
    if not (await check_ai())['ok']:raise RuntimeError('DeepSeek not ready')
    config=db.one("SELECT * FROM configs WHERE job_id='IT-599'")
    if not config:raise RuntimeError('IT-599 chưa có criteria.')
    db.execute("UPDATE configs SET approved=1 WHERE job_id='IT-599'")
    rid=engine.create_run('IT-599',config['version'])
    await engine.match(rid)
    results=[db.unpack(r) for r in db.rows('SELECT * FROM evaluations WHERE run_id=?',(rid,))]
    good=[r for r in results if engine.eligible(r.get('data'),85)]
    good.sort(key=lambda r:r['data']['score'],reverse=True)
    print('VALIDATION ONLY: evaluated',len(results),'eligible',len(good),flush=True)
    picked=[r['candidate_id'] for r in good[:5]]
    report=None
    if picked:
        report_id=engine.create_report(rid,picked,5,85)
        await engine.compare(report_id)
        report=db.unpack(db.one('SELECT * FROM reports WHERE id=?',(report_id,)),('data','selected'))
        print('Round2 validated',report['status'],'selected',picked,flush=True)
    summary={'validation_only':True,'job_id':'IT-599','run_id':rid,'candidate_count':len(results),
        'completed':sum(r['status']=='COMPLETED' for r in results),'failed':[r['candidate_id'] for r in results if r['status']=='FAILED'],
        'shortlist_count':len(good),'selected':picked,'round2_status':report['status'] if report else 'EMPTY_SHORTLIST',
        'reference_pair_CV429':next((r.get('data') for r in results if r['candidate_id']=='CV429'),None)}
    (dest/'summary.json').write_text(db.dumps(summary),encoding='utf-8')
    print(db.dumps({k:v for k,v in summary.items() if k!='reference_pair_CV429'}),flush=True)

if __name__=='__main__':asyncio.run(main())
