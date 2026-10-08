from alembic import op
revision='0007_public_content'
down_revision='0006_location_scope'
branch_labels=None
depends_on=None

def upgrade():
    from backend.content_fetch import SQL
    for statement in SQL.split(';'):
        if statement.strip():op.get_bind().exec_driver_sql(statement)

def downgrade():raise RuntimeError('Restore a verified backup instead of destructive downgrade.')
