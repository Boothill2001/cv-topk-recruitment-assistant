"""Private published review packets and per-account independent labels."""
from alembic import op
revision = '0015_pilot_review'
down_revision = '0014_inbound_review'
branch_labels = None
depends_on = None


def upgrade():
    from backend.pilot_review import SQL
    for statement in SQL.split(';'):
        if statement.strip(): op.get_bind().exec_driver_sql(statement)


def downgrade():
    raise RuntimeError('Restore verified backup to preserve review packets and human labels.')
