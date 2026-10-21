"""Private pilot sessions, audit and feedback."""
from alembic import op
revision='0013_online'
down_revision='0012_pipeline'
branch_labels=None
depends_on=None

def upgrade():
    from backend.online import SQL
    for statement in SQL.split(';'):
        if statement.strip():op.get_bind().exec_driver_sql(statement)

def downgrade():raise RuntimeError('Restore verified backup to preserve pilot audit history.')
