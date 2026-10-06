
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files (id TEXT PRIMARY KEY,name TEXT,mime TEXT,url TEXT,group_name TEXT,entity_id TEXT,modified TEXT,hash TEXT,text TEXT,status TEXT,error TEXT,available INTEGER DEFAULT 1,revision INTEGER DEFAULT 1,updated TEXT);
CREATE TABLE IF NOT EXISTS profiles (id TEXT,kind TEXT,revision INTEGER,data TEXT,source_hash TEXT,status TEXT,updated TEXT,PRIMARY KEY(id,kind));
CREATE TABLE IF NOT EXISTS configs (job_id TEXT PRIMARY KEY,version INTEGER,data TEXT,approved INTEGER DEFAULT 0,source_revision INTEGER,updated TEXT,criteria_approved INTEGER DEFAULT 0,strategy_hash TEXT);
CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY,job_id TEXT,status TEXT,config_version INTEGER,snapshot TEXT,model TEXT,prompt_version TEXT,error TEXT,created TEXT,updated TEXT,provider TEXT DEFAULT 'deepseek');
CREATE TABLE IF NOT EXISTS evaluations (run_id TEXT,candidate_id TEXT,data TEXT,status TEXT,error TEXT,PRIMARY KEY(run_id,candidate_id));
CREATE TABLE IF NOT EXISTS reports (id TEXT PRIMARY KEY,run_id TEXT,selected TEXT,data TEXT,status TEXT,error TEXT,model TEXT,prompt_version TEXT,created TEXT,provider TEXT DEFAULT 'deepseek');
CREATE TABLE IF NOT EXISTS tasks (id BIGSERIAL PRIMARY KEY,kind TEXT,payload TEXT,status TEXT,error TEXT,created TEXT,updated TEXT);
CREATE TABLE IF NOT EXISTS calls (id BIGSERIAL PRIMARY KEY,task TEXT,model TEXT,prompt_version TEXT,usage TEXT,status TEXT,error TEXT,created TEXT,provider TEXT DEFAULT 'deepseek');
CREATE TABLE IF NOT EXISTS feedback (id BIGSERIAL PRIMARY KEY,run_id TEXT,candidate_id TEXT,decision TEXT,note TEXT,created TEXT);
CREATE TABLE IF NOT EXISTS source_revisions (file_id TEXT,revision INTEGER,data TEXT NOT NULL,captured TEXT,PRIMARY KEY(file_id,revision));
CREATE TABLE IF NOT EXISTS config_revisions (job_id TEXT,version INTEGER,data TEXT NOT NULL,source_revision INTEGER,captured TEXT,PRIMARY KEY(job_id,version));
CREATE TABLE IF NOT EXISTS sheet_exports (id TEXT PRIMARY KEY,run_id TEXT,fingerprint TEXT UNIQUE,payload TEXT NOT NULL,spreadsheet_id TEXT,url TEXT,status TEXT,error TEXT,created TEXT,updated TEXT);
CREATE TABLE IF NOT EXISTS profile_revisions (id TEXT,kind TEXT,revision INTEGER,data TEXT,source_hash TEXT,captured TEXT,PRIMARY KEY(id,kind,revision));
CREATE TABLE IF NOT EXISTS candidate_facts (candidate_id TEXT,fact_id TEXT,field TEXT,value TEXT,numeric_value DOUBLE PRECISION,polarity TEXT,evidence JSONB,profile_revision INTEGER,PRIMARY KEY(candidate_id,fact_id));
CREATE INDEX IF NOT EXISTS facts_lookup ON candidate_facts(field,value);
CREATE INDEX IF NOT EXISTS facts_numeric ON candidate_facts(field,numeric_value);
CREATE INDEX IF NOT EXISTS profiles_pool ON profiles(kind,status,id);
CREATE INDEX IF NOT EXISTS tasks_pending ON tasks(status,kind,id);
CREATE TABLE IF NOT EXISTS labels (id BIGSERIAL PRIMARY KEY,job_id TEXT,candidate_id TEXT,decision TEXT,note TEXT,profile_revision INTEGER,job_revision INTEGER,created TEXT);
CREATE TABLE IF NOT EXISTS benchmark_runs (id BIGSERIAL PRIMARY KEY,data TEXT,created TEXT);
CREATE OR REPLACE FUNCTION pilot_archive_source() RETURNS trigger AS $$ BEGIN
 INSERT INTO source_revisions VALUES(NEW.id,NEW.revision,row_to_json(NEW)::text,NEW.updated) ON CONFLICT DO NOTHING; RETURN NEW; END; $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS archive_source ON files;
CREATE TRIGGER archive_source AFTER INSERT OR UPDATE ON files FOR EACH ROW EXECUTE FUNCTION pilot_archive_source();
CREATE OR REPLACE FUNCTION pilot_archive_config() RETURNS trigger AS $$ BEGIN
 INSERT INTO config_revisions VALUES(NEW.job_id,NEW.version,NEW.data,NEW.source_revision,NEW.updated) ON CONFLICT DO NOTHING; RETURN NEW; END; $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS archive_config ON configs;
CREATE TRIGGER archive_config AFTER INSERT OR UPDATE ON configs FOR EACH ROW EXECUTE FUNCTION pilot_archive_config();
