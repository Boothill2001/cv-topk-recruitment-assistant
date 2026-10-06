"""Local backup and non-destructive restore verification; credentials never printed."""
import argparse
import json
import os
import subprocess
from datetime import datetime,timezone
from pathlib import Path
import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url
from . import db

BIN=Path(r'C:\Program Files\PostgreSQL\17\bin')

def backup():
    url=make_url(db.database_url());folder=db.RUNTIME/'backups';folder.mkdir(exist_ok=True)
    path=folder/('pilot-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.dump')
    environment={**os.environ,'PGPASSWORD':url.password}
    subprocess.run([str(BIN/'pg_dump.exe'),'-Fc','-h',url.host,'-p',str(url.port or 5432),'-U',url.username,'-d',url.database,'-f',str(path)],env=environment,check=True,capture_output=True,timeout=120)
    print('Backup saved locally:',path.name)
    return path

def verify_restore(path):
    path=Path(path).resolve();folder=(db.RUNTIME/'backups').resolve()
    if not path.is_relative_to(folder) or not path.is_file():raise ValueError('Chỉ restore bản dump trong runtime/backups.')
    url=make_url(db.database_url())
    if url.host!='127.0.0.1' or url.port!=55432:raise ValueError('Restore test chỉ chạy trên PostgreSQL riêng của pilot, cổng 55432.')
    password=(db.RUNTIME/'postgres-password.txt').read_text().strip()
    target='pilot_restore_validation_'+datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
    with psycopg.connect(host='127.0.0.1',port=55432,user='pilot_admin',password=password,dbname='postgres',autocommit=True) as c:
        c.execute(sql.SQL('CREATE DATABASE {} OWNER recruitment_pilot').format(sql.Identifier(target)))
    environment={**os.environ,'PGPASSWORD':url.password}
    subprocess.run([str(BIN/'pg_restore.exe'),'--no-owner','--no-privileges','-h',url.host,'-p','55432','-U',url.username,'-d',target,str(path)],env=environment,check=True,capture_output=True,timeout=120)
    counts={}
    with psycopg.connect(host=url.host,port=55432,user=url.username,password=url.password,dbname=target) as c:
        for table in ('files','profiles','candidate_facts','runs','calls'):
            n=c.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
            assert n==db.one('SELECT COUNT(*) n FROM '+table)['n'],table+' restore count mismatch'
            counts[table]=n
    report={'backup':path.name,'restored_database':target,'counts':counts,'verified_at':db.now(),'production_modified':False}
    (db.RUNTIME/'restore-validation.json').write_text(json.dumps(report),encoding='utf-8')
    print('Restore verified in separate database:',counts)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--verify',action='store_true');args=parser.parse_args()
    path=backup()
    if args.verify:verify_restore(path)
