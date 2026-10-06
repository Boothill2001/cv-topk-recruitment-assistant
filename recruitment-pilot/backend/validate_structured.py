"""Real PostgreSQL smoke test in a separate database, never approves customer configs.
Run: .venv/Scripts/python -m backend.validate_structured
"""
import asyncio
import copy
import json
import os
import time
from datetime import datetime,timezone
import psycopg
from psycopg import sql
from . import db,engine,retrieval
from .api import approve_criteria,approve,VersionBody

async def main():
    tables={t:db.rows('SELECT * FROM '+t) for t in ('settings','files','profiles','configs','profile_revisions')}
    password=(db.RUNTIME/'postgres-password.txt').read_text().strip()
    name='pilot_validation_'+datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
    with psycopg.connect(host='127.0.0.1',port=55432,user='pilot_admin',password=password,dbname='postgres',autocommit=True) as c:
        c.execute(sql.SQL('CREATE DATABASE {} OWNER recruitment_pilot').format(sql.Identifier(name)))
    from sqlalchemy.engine import make_url
    url=make_url(db.database_url()).set(database=name)
    os.environ['DATABASE_URL']=url.render_as_string(hide_password=False)
    db.init()
    with db.conn() as c:
        for table,rows in tables.items():
            for row in rows:
                columns=list(row)
                c.execute('INSERT OR REPLACE INTO '+table+'('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',tuple(row.values()))
    validation={'database':name,'production_configs_approved':False,'real_runs':[],'load_test':[]}
    for job_id in ('IT-599','IT-595c'):
        config=db.one('SELECT * FROM configs WHERE job_id=?',(job_id,))
        # Simulated human approvals belong exclusively to this validation database.
        approve_criteria(job_id,VersionBody(version=config['version']))
        await engine.generate_strategies(job_id,config['version'],3)
        config=db.one('SELECT * FROM configs WHERE job_id=?',(job_id,))
        approve(job_id,VersionBody(version=config['version']))
        before=db.one('SELECT COUNT(*) n FROM calls')['n'];started=time.perf_counter()
        rid=engine.create_run(job_id,config['version']);elapsed=time.perf_counter()-started
        after=db.one('SELECT COUNT(*) n FROM calls')['n']
        assert after==before,'Round 1 called LLM'
        rows=[db.unpack(e) for e in db.rows('SELECT * FROM evaluations WHERE run_id=?',(rid,))]
        rows.sort(key=lambda e:(-e['data']['retrieval_score'],e['candidate_id']))
        validation['real_runs'].append({'job_id':job_id,'run_id':rid,'pool':len(rows),'end_to_end_seconds':elapsed,
            'llm_calls_round1':after-before,'pdf_reads':0,'top5':[{'id':r['candidate_id'],'T':r['data']['score'],'R':r['data']['retrieval_score'],'lane':r['data']['lane']} for r in rows[:5]]})
        if job_id=='IT-599':
            selected=[r['candidate_id'] for r in rows[:2]]
            report=engine.create_report(rid,selected,5,85);await engine.compare(report)
            completed=db.unpack(db.one('SELECT * FROM reports WHERE id=?',(report,)))
            assert sorted(i['candidate_id'] for i in completed['data']['items'])==sorted(selected)
            validation['comparison']={'id':report,'selected':selected,'status':completed['status']}
    # Synthetic load clones are explicitly labelled and exist ONLY in this validation DB.
    prototype=db.one("SELECT * FROM profiles WHERE kind='candidate' AND status='READY' ORDER BY id LIMIT 1")
    config=db.one("SELECT * FROM configs WHERE job_id='IT-599'")
    for count in (30,1000,2000):
        if count>30:
            with db.conn() as c:
                for i in range(30,count):
                    eid='LOAD_'+str(i)
                    c.execute('INSERT OR REPLACE INTO profiles VALUES(?,?,?,?,?,?,?)',(eid,'candidate',prototype['revision'],prototype['data'],'synthetic-load','READY',db.now()))
        times=[]
        for repeat in range(3):
            # Explicitly make a fresh policy version to benchmark uncached runs.
            db.execute('UPDATE configs SET version=version+1 WHERE job_id=?',('IT-599',))
            version=db.one('SELECT version FROM configs WHERE job_id=?',('IT-599',))['version']
            before=db.one('SELECT COUNT(*) n FROM calls')['n'];start=time.perf_counter()
            rid=engine.create_run('IT-599',version);times.append(time.perf_counter()-start)
            assert db.one('SELECT COUNT(*) n FROM calls')['n']==before
        validation['load_test'].append({'profiles':count,'samples_seconds':times,'max_seconds':max(times),'type':'synthetic size using real structured profile','llm_calls':0})
    output=db.RUNTIME/'structured-validation.json';output.write_text(db.dumps(validation),encoding='utf-8')
    print(json.dumps(validation,ensure_ascii=False,indent=2))

if __name__=='__main__':asyncio.run(main())
