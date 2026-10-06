"""One-time, transactional SQLite import into a dedicated local PostgreSQL database."""
import json
import sqlite3
from pathlib import Path
from urllib.parse import quote
import psycopg
from psycopg import sql
from . import db

def provision():
    if not db.database_url():
        password=(db.RUNTIME/'postgres-password.txt').read_text().strip()
        with psycopg.connect(host='127.0.0.1',port=55432,user='pilot_admin',password=password,dbname='postgres',autocommit=True) as c:
            if not c.execute("SELECT 1 FROM pg_roles WHERE rolname='recruitment_pilot'").fetchone():
                c.execute(sql.SQL('CREATE ROLE recruitment_pilot LOGIN PASSWORD {}').format(sql.Literal(password)))
            if not c.execute("SELECT 1 FROM pg_database WHERE datname='recruitment_pilot'").fetchone():
                c.execute('CREATE DATABASE recruitment_pilot OWNER recruitment_pilot')
        url='postgresql+psycopg://recruitment_pilot:'+quote(password,safe='')+'@127.0.0.1:55432/recruitment_pilot'
        (db.RUNTIME/'database.json').write_text(json.dumps({'url':url}),encoding='utf-8')
    db.init()
    if db.setting('sqlite_import_complete'):return
    source=db.RUNTIME/'pilot.sqlite3'
    if source.exists():
        backup=db.RUNTIME/'before-structured-migration.sqlite3'
        with sqlite3.connect(source) as old:
            with sqlite3.connect(backup) as out:old.backup(out)
            old.row_factory=sqlite3.Row
            names=[r[0] for r in old.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            allowed={'settings','files','profiles','configs','runs','evaluations','reports','tasks','calls','feedback','source_revisions','config_revisions','sheet_exports'}
            with db.LOCK,db.conn() as c:
                for table in names:
                    if table not in allowed:continue
                    rows=old.execute('SELECT * FROM '+table).fetchall()
                    for row in rows:
                        columns=list(row.keys());values=[row[k] for k in columns]
                        c.execute('INSERT OR IGNORE INTO '+table+'('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',values)
                for table in ('tasks','calls','feedback'):
                    c.execute("SELECT setval(pg_get_serial_sequence('"+table+"','id'),COALESCE((SELECT MAX(id) FROM "+table+"),1),(SELECT COUNT(*)>0 FROM "+table+"))")
                c.execute("UPDATE configs SET approved=0,criteria_approved=0,strategy_hash=NULL")
                c.execute("UPDATE profiles SET status='NEEDS_BACKFILL'")
                c.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('sqlite_import_complete',db.dumps({'at':db.now(),'backup':backup.name})))
        db.cancel_legacy()
    else:db.set_setting('sqlite_import_complete',{'at':db.now(),'empty':True})
    print('PostgreSQL ready; source SQLite and backup preserved. Credentials stay in ignored runtime.')

if __name__=='__main__':provision()
