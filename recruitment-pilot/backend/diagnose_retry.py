"""Retry failures only in the isolated validation database."""
import asyncio,json
from . import db,engine
from .integrations import check_ai

async def main():
    db.RUNTIME=db.RUNTIME/'validation'
    db.init()
    if not (await check_ai())['ok']:raise RuntimeError('DeepSeek not ready')
    summary=json.loads((db.RUNTIME/'summary.json').read_text(encoding='utf-8'))
    await engine.match(summary['run_id'])
    results=[db.unpack(r) for r in db.rows('SELECT * FROM evaluations WHERE run_id=?',(summary['run_id'],))]
    good=sorted([r for r in results if engine.eligible(r.get('data'),85)],key=lambda r:r['data']['score'],reverse=True)
    picked=[r['candidate_id'] for r in good[:5]]
    report_status='EMPTY_SHORTLIST'
    if picked:
        rid=engine.create_report(summary['run_id'],picked,5,85)
        await engine.compare(rid)
        report_status=db.one('SELECT status FROM reports WHERE id=?',(rid,))['status']
    summary.update(completed=sum(r['status']=='COMPLETED' for r in results),
        failed=[r['candidate_id'] for r in results if r['status']=='FAILED'],
        shortlist_count=len(good),selected=picked,round2_status=report_status)
    (db.RUNTIME/'summary.json').write_text(db.dumps(summary),encoding='utf-8')
    print(db.dumps({k:v for k,v in summary.items() if k!='reference_pair_CV429'}),flush=True)

if __name__=='__main__':asyncio.run(main())
