"""Live criteria → approval → strategies check in a separate SQLite copy."""
import asyncio
import json
import sqlite3
from . import db,engine
from .api import approve_criteria,approve,VersionBody

async def main():
    original=db.RUNTIME
    target=original/'flow-validation';target.mkdir(exist_ok=True)
    with sqlite3.connect(original/'pilot.sqlite3') as src,sqlite3.connect(target/'pilot.sqlite3') as dest:
        src.backup(dest)
    db.RUNTIME=target;db.init()
    await engine.propose('IT-602')
    config=db.unpack(db.one("SELECT * FROM configs WHERE job_id='IT-602'"))
    assert not config['data']['strategies'] and not config['criteria_approved']
    approve_criteria('IT-602',VersionBody(version=config['version']))
    await engine.generate_strategies('IT-602',config['version'],5)
    config=db.unpack(db.one("SELECT * FROM configs WHERE job_id='IT-602'"))
    approve('IT-602',VersionBody(version=config['version']))
    report={'job_id':'IT-602','provider':db.setting('provider'),'model':db.setting('model'),
        'criteria':len(config['data']['criteria']),'strategies':len(config['data']['strategies']),
        'stages_validated':True,'main_database_modified':False,'prompt_version':engine.VERSION}
    (target/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))

if __name__=='__main__':asyncio.run(main())
