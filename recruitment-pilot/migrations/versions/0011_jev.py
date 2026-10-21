"""Durable experimental Jev steps and local trial budget ledger."""
from alembic import op
revision='0011_jev'
down_revision='0010_scouting_guidance'
branch_labels=None
depends_on=None

def upgrade():
    from backend.jev import SQL
    for statement in SQL.split(';'):
        if statement.strip():op.get_bind().exec_driver_sql(statement)

def downgrade():
    raise RuntimeError('Restore a verified backup; do not delete Jev usage history.')
