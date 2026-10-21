"""Botkeep entrypoint. Migrations run separately with an owner credential."""
import os
import sys
from pathlib import Path
from dotenv import load_dotenv
import uvicorn

if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    load_dotenv(root / '.env', override=False, interpolate=False)
    if os.environ.get('PILOT_MODE') != 'ONLINE_PILOT':
        raise RuntimeError('Refusing public bind outside ONLINE_PILOT')
    port = int(os.environ['SERVER_PORT'])
    uvicorn.run('backend.api:app', host='0.0.0.0', port=port, workers=1,
                proxy_headers=False, access_log=False)
