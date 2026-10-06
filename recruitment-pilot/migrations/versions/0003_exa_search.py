from alembic import op
revision='0003_exa_search'
down_revision='0002_batch_search'
branch_labels=None
depends_on=None

def upgrade():
    # Frozen schema; never import mutable runtime SQL into historical migrations.
    op.get_bind().exec_driver_sql('''
CREATE TABLE web_searches(id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,query TEXT NOT NULL,
status TEXT NOT NULL,response TEXT,attempts INTEGER NOT NULL DEFAULT 0,error TEXT,created TEXT NOT NULL,updated TEXT NOT NULL);
CREATE INDEX web_search_fingerprint ON web_searches(fingerprint,created);
CREATE TABLE web_search_attempts(id TEXT PRIMARY KEY,search_id TEXT NOT NULL,status TEXT NOT NULL,
request_id TEXT,cost_dollars REAL,error TEXT,created TEXT NOT NULL);
''')

def downgrade():
    raise RuntimeError('Restore a verified backup instead of destructive downgrade.')
