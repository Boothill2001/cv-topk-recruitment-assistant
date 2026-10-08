from alembic import op
revision='0008_exa_search_type'
down_revision='0007_public_content'
branch_labels=None
depends_on=None

def upgrade():
    op.get_bind().exec_driver_sql("ALTER TABLE web_searches ADD COLUMN search_type TEXT NOT NULL DEFAULT 'auto' CHECK (search_type IN ('auto','deep'))")

def downgrade():raise RuntimeError('Restore a verified backup instead of destructive downgrade.')
