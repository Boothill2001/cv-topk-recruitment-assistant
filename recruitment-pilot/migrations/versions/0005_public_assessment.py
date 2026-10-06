from alembic import op
revision='0005_public_assessment'
down_revision='0004_exa_people'
branch_labels=None
depends_on=None
def upgrade():
    op.get_bind().exec_driver_sql('''CREATE TABLE public_assessments (
 id TEXT PRIMARY KEY,fingerprint TEXT UNIQUE,job_id TEXT NOT NULL,config_version INTEGER NOT NULL,
 search_id TEXT NOT NULL,snapshot TEXT NOT NULL,status TEXT NOT NULL,data TEXT,
 attempts INTEGER NOT NULL DEFAULT 0,error TEXT,created TEXT NOT NULL,updated TEXT NOT NULL)''')
def downgrade():raise RuntimeError('Restore a verified backup instead of destructive downgrade.')
