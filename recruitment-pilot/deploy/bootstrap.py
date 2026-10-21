"""Install pinned dependencies once per lockfile, then start one app process."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
env = ROOT / '.online-venv'
python = env / 'bin' / 'python'
lock = ROOT / 'requirements-lock.txt'
stamp = env / '.requirements-sha256'
digest = hashlib.sha256(lock.read_bytes()).hexdigest()
if not python.exists():
    venv.EnvBuilder(with_pip=True).create(env)
if not stamp.exists() or stamp.read_text() != digest:
    subprocess.run([str(python), '-m', 'pip', 'install', '--no-cache-dir',
                    '-r', str(lock)], check=True)
    stamp.write_text(digest)
os.execv(str(python), [str(python), str(ROOT / 'deploy' / 'start_online.py')])
