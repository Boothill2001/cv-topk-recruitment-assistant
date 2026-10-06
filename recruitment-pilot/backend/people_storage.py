SQL='''
CREATE TABLE IF NOT EXISTS people_searches(id TEXT PRIMARY KEY,job_id TEXT NOT NULL,
fingerprint TEXT NOT NULL,snapshot TEXT NOT NULL,created TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS people_search_fingerprint ON people_searches(fingerprint,created);
CREATE TABLE IF NOT EXISTS people_search_items(group_id TEXT NOT NULL,strategy_id TEXT NOT NULL,
search_id TEXT NOT NULL,PRIMARY KEY(group_id,strategy_id));
'''
