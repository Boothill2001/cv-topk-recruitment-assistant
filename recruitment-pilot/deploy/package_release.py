"""Whitelist-only release bundle, including hashes of the exact dirty-tree build."""
import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def build(destination):
    files = []
    for folder in ('backend', 'migrations', 'frontend/dist', 'deploy'):
        files.extend(p for p in (ROOT / folder).rglob('*') if p.is_file()
                     and '__pycache__' not in p.parts and p.suffix != '.pyc')
    files.extend(ROOT / n for n in ('requirements.txt', 'requirements-lock.txt'))
    hashes = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(files)}
    digest = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    manifest = {'release': 'pilot-' + digest[:16], 'base_commit': commit,
                'working_tree_modified': bool(subprocess.check_output(
                    ['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip()),
                'files_sha256': hashes, 'contains_local_data': False}
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED) as archive:
        for p in files:
            archive.write(p, p.relative_to(ROOT).as_posix())
        archive.writestr('release-manifest.json', json.dumps(manifest, indent=2))
    print(json.dumps({'release': manifest['release'], 'zip': str(destination),
                      'sha256': hashlib.sha256(destination.read_bytes()).hexdigest(),
                      'file_count': len(files)}))

if __name__ == '__main__':
    build(ROOT / 'runtime' / 'releases' / 'techdy-online.zip')
