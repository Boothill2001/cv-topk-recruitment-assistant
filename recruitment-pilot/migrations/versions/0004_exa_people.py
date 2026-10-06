from alembic import op
revision='0004_exa_people'
down_revision='0003_exa_search'
branch_labels=None
depends_on=None
def upgrade():
    op.get_bind().exec_driver_sql("ALTER TABLE web_searches ADD COLUMN mode TEXT NOT NULL DEFAULT 'web'")
    op.get_bind().exec_driver_sql('''
CREATE TABLE people_searches(id TEXT PRIMARY KEY,job_id TEXT NOT NULL,fingerprint TEXT NOT NULL,snapshot TEXT NOT NULL,created TEXT NOT NULL);
CREATE INDEX people_search_fingerprint ON people_searches(fingerprint,created);
CREATE TABLE people_search_items(group_id TEXT NOT NULL,strategy_id TEXT NOT NULL,search_id TEXT NOT NULL,PRIMARY KEY(group_id,strategy_id));
''')
def downgrade():
    raise RuntimeError('Restore a verified backup instead of destructive downgrade.')
