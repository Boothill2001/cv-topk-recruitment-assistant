from alembic import op
revision='0006_location_scope'
down_revision='0005_public_assessment'
branch_labels=None
depends_on=None
def upgrade():
    op.get_bind().exec_driver_sql('ALTER TABLE web_searches ADD COLUMN location_scope TEXT')
    op.get_bind().exec_driver_sql('ALTER TABLE web_searches ADD COLUMN effective_query TEXT')
def downgrade():raise RuntimeError('Restore a verified backup instead of destructive downgrade.')
