SQL='''
CREATE TABLE IF NOT EXISTS web_searches(
id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,query TEXT NOT NULL,status TEXT NOT NULL,
response TEXT,attempts INTEGER NOT NULL DEFAULT 0,error TEXT,created TEXT NOT NULL,updated TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS web_search_fingerprint ON web_searches(fingerprint,created);
CREATE TABLE IF NOT EXISTS web_search_attempts(
id TEXT PRIMARY KEY,search_id TEXT NOT NULL,status TEXT NOT NULL,request_id TEXT,cost_dollars REAL,
error TEXT,created TEXT NOT NULL);
'''
