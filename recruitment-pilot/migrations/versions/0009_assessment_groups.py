"""Preserve legacy runs; add durable all-source orchestration."""
from alembic import op
revision = '0009_assessment_groups'
down_revision = '0008_exa_search_type'
branch_labels = None
depends_on = None

def upgrade():
    from backend.assessment_groups import SQL
    for statement in SQL.split(';'):
        if statement.strip(): op.get_bind().exec_driver_sql(statement)

def downgrade():
    raise RuntimeError('Restore a verified backup instead of destructive downgrade.')
