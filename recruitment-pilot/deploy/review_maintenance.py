"""Explicit stopped-service maintenance, never an application startup migration."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('backup', 'apply'))
    parser.add_argument('--packet')
    parser.add_argument('--sha256')
    args = parser.parse_args()
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env', override=False, interpolate=False)
    if os.environ.get('PILOT_MODE') != 'ONLINE_PILOT': raise RuntimeError('Online maintenance only')
    from backend import db
    if db.one("SELECT id FROM tasks WHERE status IN ('PENDING','RUNNING') LIMIT 1"):
        raise RuntimeError('Queue is not idle; do not interrupt provider calls')
    if args.action == 'backup':
        target = db.RUNTIME / 'operator-backups'
        target.mkdir(exist_ok=True)
        manifest = {}
        files = sorted({p for folder in (ROOT / 'uploads', db.RUNTIME / 'uploads') if folder.is_dir() for p in folder.rglob('*') if p.is_file()})
        with zipfile.ZipFile(target / 'uploads-before-review-20261021.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
            for p in files:
                name = p.relative_to(ROOT).as_posix()
                archive.write(p, name); manifest[name] = hashlib.sha256(p.read_bytes()).hexdigest()
            archive.writestr('uploads-manifest.json', json.dumps(manifest, sort_keys=True))
        if not files: raise RuntimeError('Expected existing JD uploads; inspect persistent paths')
        print(json.dumps({'backup': str(target / 'uploads-before-review-20261021.zip'), 'files': len(files), 'hashes': manifest}))
        return
    if not args.packet or not args.sha256: raise RuntimeError('Packet and checksum are required')
    packet = Path(args.packet).resolve()
    if not packet.is_relative_to(db.RUNTIME.resolve()): raise RuntimeError('Packet must be inside private runtime')
    manifest = json.loads((ROOT / 'release-manifest.json').read_text())
    for name, expected in manifest['files_sha256'].items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError('Release checksum mismatch: ' + name)
    if hashlib.sha256(packet.read_bytes()).hexdigest() != args.sha256: raise RuntimeError('Packet checksum mismatch')
    from backend import pilot_review
    value = json.loads(packet.read_text(encoding='utf-8')); pilot_review.validate_bundle(value)
    before = {t: db.one('SELECT COUNT(*) AS n FROM ' + t)['n'] for t in ('calls', 'jev_ledger', 'tasks')}
    from alembic.config import Config
    from alembic import command
    cfg = Config(); cfg.set_main_option('script_location', str(ROOT / 'migrations'))
    cfg.attributes['connection_url'] = os.environ['DATABASE_URL']
    command.upgrade(cfg, 'head')
    rid = pilot_review.import_bundle(value, 'tri', manifest['release'])
    after = {t: db.one('SELECT COUNT(*) AS n FROM ' + t)['n'] for t in before}
    if before != after: raise RuntimeError('Unexpected queue/provider ledger mutation')
    report = {'schema': db.one('SELECT version_num FROM alembic_version')['version_num'], 'review_set_id': rid,
              'release': manifest['release'], 'provider_calls_added': 0, 'candidates': len(value['candidates'])}
    (db.RUNTIME / 'review-deployment.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__': main()
