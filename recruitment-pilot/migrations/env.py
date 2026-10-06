from alembic import context
from backend.postgres import engine
with engine(context.config.attributes['connection_url']).connect() as connection:
    context.configure(connection=connection)
    with context.begin_transaction():context.run_migrations()
