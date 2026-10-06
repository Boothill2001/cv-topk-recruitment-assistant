import json
import sqlite3
import threading
import os
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / 'runtime'
RUNTIME.mkdir(exist_ok=True)
DEFAULT_RUNTIME = RUNTIME
LOCK = threading.RLock()

def now():
    return datetime.now(timezone.utc).isoformat()

def conn():
    if database_url():
        from .postgres import connect
        return connect(database_url())
    if RUNTIME == DEFAULT_RUNTIME:
        raise RuntimeError('PostgreSQL chưa cấu hình. Chạy setup-postgres.ps1 trước khi mở pilot.')
    c = sqlite3.connect(RUNTIME / 'pilot.sqlite3', timeout=30)
    c.row_factory = sqlite3.Row
    return c

def init():
    if database_url():
        from .postgres import initialize
        initialize(database_url(),ROOT)
        cancel_legacy()
        return
    with LOCK, conn() as c:
        c.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS files (id TEXT PRIMARY KEY, name TEXT, mime TEXT, url TEXT,
          group_name TEXT, entity_id TEXT, modified TEXT, hash TEXT, text TEXT, status TEXT,
          error TEXT, available INTEGER DEFAULT 1, revision INTEGER DEFAULT 1, updated TEXT);
        CREATE TABLE IF NOT EXISTS profiles (id TEXT, kind TEXT, revision INTEGER, data TEXT,
          source_hash TEXT, status TEXT, updated TEXT, PRIMARY KEY(id,kind));
        CREATE TABLE IF NOT EXISTS configs (job_id TEXT PRIMARY KEY, version INTEGER, data TEXT,
          approved INTEGER DEFAULT 0, source_revision INTEGER, updated TEXT);
        CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, job_id TEXT, status TEXT, config_version INTEGER,
          snapshot TEXT, model TEXT, prompt_version TEXT, error TEXT, created TEXT, updated TEXT);
        CREATE TABLE IF NOT EXISTS evaluations (run_id TEXT, candidate_id TEXT, data TEXT, status TEXT,
          error TEXT, PRIMARY KEY(run_id,candidate_id));
        CREATE TABLE IF NOT EXISTS reports (id TEXT PRIMARY KEY, run_id TEXT, selected TEXT, data TEXT,
          status TEXT, error TEXT, model TEXT, prompt_version TEXT, created TEXT);
        CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, payload TEXT,
          status TEXT, error TEXT, created TEXT, updated TEXT);
        CREATE TABLE IF NOT EXISTS calls (id INTEGER PRIMARY KEY AUTOINCREMENT, task TEXT, model TEXT,
          prompt_version TEXT, usage TEXT, status TEXT, error TEXT, created TEXT);
        CREATE TABLE IF NOT EXISTS feedback (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT,
          candidate_id TEXT, decision TEXT, note TEXT, created TEXT);
        CREATE TABLE IF NOT EXISTS source_revisions (file_id TEXT, revision INTEGER,
          data TEXT NOT NULL, captured TEXT, PRIMARY KEY(file_id,revision));
        CREATE TABLE IF NOT EXISTS config_revisions (job_id TEXT, version INTEGER,
          data TEXT NOT NULL, source_revision INTEGER, captured TEXT, PRIMARY KEY(job_id,version));
        CREATE TRIGGER IF NOT EXISTS archive_source AFTER INSERT ON files BEGIN
          INSERT OR IGNORE INTO source_revisions VALUES(NEW.id,NEW.revision,
            json_object('id',NEW.id,'name',NEW.name,'mime',NEW.mime,'url',NEW.url,
              'group_name',NEW.group_name,'entity_id',NEW.entity_id,'modified',NEW.modified,
              'hash',NEW.hash,'text',NEW.text,'status',NEW.status,'error',NEW.error),NEW.updated);
        END;
        CREATE TRIGGER IF NOT EXISTS archive_config_insert AFTER INSERT ON configs BEGIN
          INSERT OR IGNORE INTO config_revisions VALUES(NEW.job_id,NEW.version,
            NEW.data,NEW.source_revision,NEW.updated);
        END;
        CREATE TRIGGER IF NOT EXISTS archive_config_update AFTER UPDATE OF version,data ON configs BEGIN
          INSERT OR IGNORE INTO config_revisions VALUES(NEW.job_id,NEW.version,
            NEW.data,NEW.source_revision,NEW.updated);
        END;
        INSERT OR IGNORE INTO source_revisions SELECT id,revision,
          json_object('id',id,'name',name,'mime',mime,'url',url,'group_name',group_name,
            'entity_id',entity_id,'modified',modified,'hash',hash,'text',text,'status',status,'error',error),updated FROM files;
        INSERT OR IGNORE INTO config_revisions SELECT job_id,version,data,source_revision,updated FROM configs;
        ''')
        # Historical pilot calls/runs used DeepSeek; preserve their identity on migration.
        for table in ('calls','runs','reports'):
            if 'provider' not in {r['name'] for r in c.execute('PRAGMA table_info('+table+')')}:
                c.execute("ALTER TABLE "+table+" ADD COLUMN provider TEXT NOT NULL DEFAULT 'deepseek'")
        if 'criteria_approved' not in {r['name'] for r in c.execute('PRAGMA table_info(configs)')}:
            c.execute('ALTER TABLE configs ADD COLUMN criteria_approved INTEGER NOT NULL DEFAULT 0')
            c.execute('ALTER TABLE configs ADD COLUMN strategy_hash TEXT')
            # Keep legacy reviewed results readable. New drafts follow the separated flow.
            import hashlib
            for r in c.execute('SELECT job_id,data FROM configs WHERE approved=1').fetchall():
                h=hashlib.sha256(dumps(json.loads(r['data'])['criteria']).encode()).hexdigest()
                c.execute('UPDATE configs SET criteria_approved=1,strategy_hash=? WHERE job_id=?',(h,r['job_id']))
        c.execute('''CREATE TABLE IF NOT EXISTS sheet_exports (id TEXT PRIMARY KEY, run_id TEXT,
            fingerprint TEXT UNIQUE, payload TEXT NOT NULL, spreadsheet_id TEXT, url TEXT,
            status TEXT, error TEXT, created TEXT, updated TEXT)''')
        c.execute("UPDATE tasks SET status='INTERRUPTED',error='Dịch vụ dừng giữa tác vụ; bấm chạy lại.',updated=? WHERE status='RUNNING'", (now(),))
        c.execute("UPDATE runs SET status='INTERRUPTED',error='Dịch vụ dừng giữa tác vụ' WHERE status='RUNNING'")
        c.execute("UPDATE reports SET status='INTERRUPTED',error='Dịch vụ dừng giữa tác vụ' WHERE status='RUNNING'")
        c.execute("UPDATE sheet_exports SET status='INTERRUPTED',error='Dịch vụ dừng giữa export; thử lại trên cùng Sheet.' WHERE status='RUNNING'")
        c.execute("UPDATE sheet_exports SET status='CREATE_UNCERTAIN',error='Dừng trong lúc tạo Sheet; kiểm tra Drive theo mã export trước khi xử lý tiếp.' WHERE status='CREATING'")
        c.executescript('''CREATE TABLE IF NOT EXISTS profile_revisions (id TEXT,kind TEXT,revision INTEGER,data TEXT,source_hash TEXT,captured TEXT,PRIMARY KEY(id,kind,revision));
        CREATE TABLE IF NOT EXISTS candidate_facts (candidate_id TEXT,fact_id TEXT,field TEXT,value TEXT,numeric_value REAL,polarity TEXT,evidence TEXT,profile_revision INTEGER,PRIMARY KEY(candidate_id,fact_id));
        CREATE TABLE IF NOT EXISTS labels (id INTEGER PRIMARY KEY AUTOINCREMENT,job_id TEXT,candidate_id TEXT,decision TEXT,note TEXT,profile_revision INTEGER,job_revision INTEGER,created TEXT);
        CREATE TABLE IF NOT EXISTS benchmark_runs (id INTEGER PRIMARY KEY AUTOINCREMENT,data TEXT,created TEXT);''')
        from .search_storage import SQL
        c.executescript(SQL)
        from .exa_storage import SQL as EXA_SQL
        c.executescript(EXA_SQL)
        if 'mode' not in [r['name'] for r in c.execute('PRAGMA table_info(web_searches)').fetchall()]:
            c.execute("ALTER TABLE web_searches ADD COLUMN mode TEXT NOT NULL DEFAULT 'web'")
        from .people_storage import SQL as PEOPLE_SQL
        c.executescript(PEOPLE_SQL)
        from .public_assessment import SQL as PUBLIC_SQL
        c.executescript(PUBLIC_SQL)
        c.execute("UPDATE public_assessments SET status='INTERRUPTED',error='Dịch vụ dừng giữa đánh giá; thử lại.' WHERE status='RUNNING'")
        c.execute("UPDATE web_searches SET status='INTERRUPTED',error='Dịch vụ dừng giữa lượt tìm web; kiểm tra lịch sử rồi thử lại.' WHERE status='RUNNING'")
        c.execute("UPDATE searches SET status='INTERRUPTED',error='Dịch vụ dừng giữa đánh giá; mở lượt tìm để tiếp tục.' WHERE status='RUNNING'")

def database_url():
    # Tests use an explicitly isolated runtime and never connect to customer storage.
    if RUNTIME != DEFAULT_RUNTIME:return None
    configured=os.environ.get('DATABASE_URL')
    path=RUNTIME/'database.json'
    if not configured and path.exists():configured=json.loads(path.read_text(encoding='utf-8-sig')).get('url')
    if configured and configured.startswith('postgresql://'):configured=configured.replace('postgresql://','postgresql+psycopg://',1)
    return configured

def cancel_legacy():
    execute("UPDATE public_assessments SET status='INTERRUPTED',error='Dịch vụ dừng giữa đánh giá; thử lại.' WHERE status='RUNNING'")
    execute("UPDATE web_searches SET status='INTERRUPTED',error='Dịch vụ dừng giữa lượt tìm web; kiểm tra lịch sử rồi thử lại.' WHERE status='RUNNING'")
    execute("UPDATE searches SET status='INTERRUPTED',error='Dịch vụ dừng giữa đánh giá; mở lượt tìm để tiếp tục.' WHERE status='RUNNING'")
    for t in rows("SELECT * FROM tasks WHERE status IN ('PENDING','RUNNING')"):
        payload=json.loads(t['payload'])
        run=one('SELECT snapshot FROM runs WHERE id=?',(payload.get('run_id'),)) if payload.get('run_id') else None
        if t['kind']=='match' and (not run or json.loads(run['snapshot']).get('engine')!='structured-2.0'):
            execute("UPDATE tasks SET status='CANCELLED_ARCHITECTURE',error=? WHERE id=?",('Old full-CV matching disabled.',t['id']))
    execute("UPDATE tasks SET status='INTERRUPTED',error='Dịch vụ dừng giữa tác vụ' WHERE status='RUNNING'")
    execute("UPDATE runs SET status='INTERRUPTED' WHERE status='RUNNING'")
    execute("UPDATE reports SET status='INTERRUPTED' WHERE status='RUNNING'")
    execute("UPDATE sheet_exports SET status='INTERRUPTED' WHERE status='RUNNING'")
    execute("UPDATE sheet_exports SET status='CREATE_UNCERTAIN' WHERE status='CREATING'")

def execute(sql, args=()):
    with LOCK, conn() as c:
        cur = c.execute(sql, args)
        return cur.lastrowid

def rows(sql, args=()):
    with LOCK, conn() as c:
        return [dict(x) for x in c.execute(sql, args).fetchall()]

def one(sql, args=()):
    r = rows(sql, args)
    return r[0] if r else None

def dumps(x):
    from .fastjson import dumps as encode
    return encode(x)

def setting(key, default=None):
    r = one('SELECT value FROM settings WHERE key=?', (key,))
    return json.loads(r['value']) if r else default

def set_setting(key, value):
    execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, dumps(value)))

def unpack(row, fields=('data',)):
    if row:
        for key in fields:
            if row.get(key): row[key] = json.loads(row[key])
    return row
