"""Local, frozen inbound review sets and append-only human labels."""
from alembic import op
revision='0014_inbound_review'
down_revision='0013_online'
branch_labels=None
depends_on=None

def upgrade():
    from backend.inbound_review import SQL
    for statement in SQL.split(';'):
        if statement.strip():op.get_bind().exec_driver_sql(statement)

def downgrade():
    raise RuntimeError('Restore verified backup to preserve review labels.')
