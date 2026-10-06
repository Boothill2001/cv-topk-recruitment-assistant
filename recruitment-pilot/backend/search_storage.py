"""Schema shared by the PostgreSQL migration and isolated SQLite test storage."""
SQL='''
CREATE TABLE IF NOT EXISTS searches (
 id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, job_id TEXT NOT NULL,
 retrieval_run_id TEXT NOT NULL, result_run_id TEXT, config_version INTEGER NOT NULL,
 candidate_limit INTEGER NOT NULL, snapshot TEXT NOT NULL, status TEXT NOT NULL,
 provider TEXT NOT NULL, model TEXT NOT NULL, prompt_version TEXT NOT NULL,
 scorer_version TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 error TEXT, usage TEXT, created TEXT NOT NULL, updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS search_assessments (
 search_id TEXT NOT NULL, candidate_id TEXT NOT NULL, data TEXT NOT NULL,
 PRIMARY KEY(search_id,candidate_id));
CREATE INDEX IF NOT EXISTS searches_job ON searches(job_id,created);
'''
