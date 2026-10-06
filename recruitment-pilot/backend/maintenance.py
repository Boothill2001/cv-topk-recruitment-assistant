"""Read-only local readiness check. Never starts workers, migrations or AI calls."""
import argparse
import json
import sys
from urllib.parse import urlsplit
from urllib.request import urlopen
from alembic.script import ScriptDirectory
from . import db


def local_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost')
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ('', '/')):
        raise ValueError('Chỉ kiểm tra địa chỉ localhost của pilot.')
    return value.rstrip('/')


def inspect(url='http://127.0.0.1:8000', require_idle=False):
    url = local_url(url)
    checks = []
    warnings = []
    try:
        with urlopen(url + '/api/status', timeout=10) as response:
            status = json.load(response)
        checks.append({'name': 'Dịch vụ đúng kiến trúc', 'ok': status.get('architecture') == 'structured-2.0'})
        checks.append({'name': 'Dịch vụ dùng PostgreSQL', 'ok': status.get('storage') == 'postgresql'})
        if not status.get('google_connected'):
            warnings.append('Chưa kết nối Google: đồng bộ Drive live chưa hoạt động.')
        if not status.get('sheets_connected'):
            warnings.append('Chưa kết nối Google Sheets: dùng CSV trước.')
        if not status.get('ai', {}).get('ok'):
            warnings.append('Chưa xác nhận AI sẵn sàng. Công cụ này không gọi AI để kiểm tra key.')
    except Exception:
        checks.append({'name': 'Dịch vụ đang phản hồi', 'ok': False})
    try:
        revisions = {r['version_num'] for r in db.rows('SELECT version_num FROM alembic_version')}
        expected = set(ScriptDirectory(str(db.ROOT/'migrations')).get_heads())
        checks.append({'name': 'Schema database khớp phiên bản code', 'ok': revisions == expected})
        pending = db.one("SELECT COUNT(*) n FROM tasks WHERE status IN ('PENDING','RUNNING')")['n']
        checks.append({'name': 'Không có tác vụ đang chạy', 'ok': pending == 0,
                       'required': require_idle})
        if pending:
            warnings.append('Có tác vụ đang chạy: chờ hoàn tất trước khi sao lưu để cập nhật.')
    except Exception:
        # SQL/connection exceptions can contain credentials; do not serialize them.
        checks.append({'name': 'Đọc database cấu hình cục bộ', 'ok': False})
    checks.append({'name': 'Có frontend đã build', 'ok': (db.ROOT/'frontend/dist/index.html').is_file()})
    return {'checked_at': db.now(), 'ok': all(c['ok'] for c in checks if c.get('required', True)),
            'checks': checks, 'warnings': warnings, 'llm_calls': 0, 'database_writes': 0}


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8000', type=local_url)
    parser.add_argument('--require-idle', action='store_true')
    args = parser.parse_args()
    result = inspect(args.url, args.require_idle)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
