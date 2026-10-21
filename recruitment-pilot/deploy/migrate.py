"""Run release migrations explicitly; never print database credentials."""
import os
import sys
from pathlib import Path
from alembic import command
from alembic.config import Config

if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    cfg = Config()
    cfg.set_main_option('script_location', str(Path(__file__).resolve().parents[1] / 'migrations'))
    cfg.attributes['connection_url'] = os.environ['MIGRATION_DATABASE_URL']
    command.upgrade(cfg, 'head')
