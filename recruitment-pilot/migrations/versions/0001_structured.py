from alembic import op
from pathlib import Path
revision='0001_structured'
down_revision=None
branch_labels=None
depends_on=None
def upgrade():op.get_bind().exec_driver_sql(Path(__file__).with_name('schema_0001.sql').read_text(encoding='utf-8'))
def downgrade():raise RuntimeError('Destructive downgrade disabled; restore a verified backup instead.')
