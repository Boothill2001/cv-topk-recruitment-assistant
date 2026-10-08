"""Append-only business guidance per JD; historical configurations unchanged."""
from alembic import op
revision = '0010_scouting_guidance'
down_revision = '0009_assessment_groups'
branch_labels = None
depends_on = None

def upgrade():
    from backend.scouting_guidance import SQL
    op.get_bind().exec_driver_sql(SQL)

def downgrade():
    raise RuntimeError('Restore a verified backup rather than deleting recruiter guidance history.')
