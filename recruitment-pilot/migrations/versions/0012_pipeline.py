"""Immutable pipeline artifacts and lineage."""
from alembic import op
revision='0012_pipeline'
down_revision='0011_jev'
branch_labels=None
depends_on=None
def upgrade():
    from backend.pipeline import SQL
    for statement in SQL.split(';'):
        if statement.strip():op.get_bind().exec_driver_sql(statement)
    op.get_bind().exec_driver_sql("""CREATE FUNCTION pipeline_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'Immutable pipeline record'; END $$""")
    for table in ('artifacts','artifact_links','pipeline_runs'):
        op.get_bind().exec_driver_sql(f'CREATE TRIGGER immutable_{table} BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION pipeline_immutable()')
    op.get_bind().exec_driver_sql("CREATE TRIGGER immutable_terminal_attempt BEFORE UPDATE OR DELETE ON node_attempts FOR EACH ROW WHEN (OLD.status IN ('COMPLETED','FAILED')) EXECUTE FUNCTION pipeline_immutable()")
def downgrade():raise RuntimeError('Restore verified backup; preserve artifact history.')
