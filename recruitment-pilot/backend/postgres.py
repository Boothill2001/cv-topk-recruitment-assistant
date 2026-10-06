"""PostgreSQL storage using SQLAlchemy pooling and psycopg; no production SQLite fallback."""
import json
import re
from contextlib import contextmanager
from sqlalchemy import create_engine
from psycopg.rows import dict_row

ENGINES={}
KEYS={'settings':['key'],'files':['id'],'profiles':['id','kind'],'configs':['job_id'],
      'runs':['id'],'evaluations':['run_id','candidate_id'],'reports':['id'],
      'source_revisions':['file_id','revision'],'config_revisions':['job_id','version'],
      'sheet_exports':['id'],'candidate_facts':['candidate_id','fact_id'],'profile_revisions':['id','kind','revision']}
AUTO={'tasks','calls','feedback','labels','benchmark_runs'}



def engine(url):
    if url not in ENGINES:ENGINES[url]=create_engine(url,pool_pre_ping=True,pool_size=5,max_overflow=5)
    return ENGINES[url]

class Result:
    def __init__(self,cursor,lastrowid=None):self.cursor=cursor;self.lastrowid=lastrowid
    def fetchone(self):return self.cursor.fetchone()
    def fetchall(self):return self.cursor.fetchall()
    def __iter__(self):return iter(self.cursor)

class Connection:
    def __init__(self,raw):self.raw=raw;self.columns={}
    def execute(self,sql,args=()):
        sql=sql.replace('?','%s')
        match=re.match(r'INSERT OR (REPLACE|IGNORE) INTO (\w+)(\s*\([^)]*\))?',sql,re.I)
        if match:
            mode,table,colspec=match.groups()
            sql=re.sub(r'INSERT OR (?:REPLACE|IGNORE)', 'INSERT',sql,count=1,flags=re.I)
            if mode.upper()=='IGNORE':sql+=' ON CONFLICT DO NOTHING'
            else:
                if colspec:columns=[s.strip() for s in colspec.strip()[1:-1].split(',')]
                else:
                    if table not in self.columns:self.columns[table]=[r['column_name'] for r in self.raw.cursor(row_factory=dict_row).execute('SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=%s ORDER BY ordinal_position',(table,)).fetchall()]
                    columns=self.columns[table]
                keys=KEYS[table];updates=[c for c in columns if c not in keys]
                sql+=' ON CONFLICT ('+','.join(keys)+') DO UPDATE SET '+','.join(c+'=EXCLUDED.'+c for c in updates)
        autoinsert=re.match(r'INSERT INTO (\w+)',sql,re.I)
        returns=autoinsert and autoinsert[1] in AUTO
        if returns:sql+=' RETURNING id'
        cur=self.raw.cursor(row_factory=dict_row);cur.execute(sql,args)
        last=cur.fetchone()['id'] if returns else None
        return Result(cur,last)
    def executemany(self,sql,args):
        if 'INSERT OR' not in sql.upper() and not re.match(r'INSERT INTO (tasks|calls|feedback|labels|benchmark_runs)\b',sql,re.I):
            return self.raw.cursor(row_factory=dict_row).executemany(sql.replace('?','%s'),args)
        for a in args:self.execute(sql,a)

@contextmanager
def connect(url):
    raw=engine(url).raw_connection()
    try:
        yield Connection(raw)
        raw.commit()
    except BaseException:
        raw.rollback();raise
    finally:raw.close()

def initialize(url,root):
    from alembic.config import Config
    from alembic import command
    cfg=Config();cfg.set_main_option('script_location',str(root/'migrations'))
    cfg.attributes['connection_url']=url
    command.upgrade(cfg,'head')
