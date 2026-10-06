import asyncio
import io
import json
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
import httpx
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from pypdf import PdfReader
from . import db

from .ai_runtime import ai, check_ai, available_models, key, redact_error, provider_catalog, env_file
from .errors import IntegrationError

SCOPES = ['https://www.googleapis.com/auth/drive.readonly']
SHEETS_SCOPE = 'https://www.googleapis.com/auth/spreadsheets'
FOLDER_ID = '1kFMAzNNHclHeFJdiTkQSwz560gJOx9kC'

def sheet_connected():
    p=db.RUNTIME/'google-token.json'
    if not p.exists():return False
    try:return SHEETS_SCOPE in json.loads(p.read_text(encoding='utf-8')).get('scopes',[])
    except (ValueError,OSError):return False

def credentials(sheets=False):
    p = db.RUNTIME / 'google-token.json'
    if not p.exists(): raise IntegrationError('Chưa kết nối Google OAuth. Mở Thiết lập để kết nối Drive.')
    c = Credentials.from_authorized_user_file(str(p))
    if not c.has_scopes(SCOPES+([SHEETS_SCOPE] if sheets else [])):
        raise IntegrationError('Google chưa cấp đủ quyền. Kết nối lại và bật Google Sheets trong Thiết lập.')
    if c.expired and c.refresh_token:
        c.refresh(Request()); p.write_text(c.to_json(),encoding='utf-8')
    if not c.valid: raise IntegrationError('Google token hết hạn hoặc bị thu hồi. Kết nối lại.')
    return c

def authorize(client_path,enable_sheets=False):
    p = Path(client_path)
    if not p.is_file(): raise IntegrationError('Không tìm thấy OAuth Desktop client JSON tại đường dẫn đã nhập.')
    j = json.loads(p.read_text(encoding='utf-8-sig'))
    if 'installed' not in j: raise IntegrationError('Cần OAuth client loại Desktop app, không phải Web/service account.')
    requested=SCOPES+([SHEETS_SCOPE] if enable_sheets else [])
    flow = InstalledAppFlow.from_client_secrets_file(str(p), requested)
    creds = flow.run_local_server(host='127.0.0.1',port=0,open_browser=True,timeout_seconds=180,
                                 authorization_prompt_message='Hoàn tất đăng nhập Google trong browser.',
                                 success_message='Đã kết nối Google. Có thể đóng tab này.',prompt='consent',access_type='offline')
    if not creds.has_scopes(requested):raise IntegrationError('Chưa cấp đủ quyền đã chọn; token cũ được giữ nguyên.')
    (db.RUNTIME/'google-token.json').write_text(creds.to_json(),encoding='utf-8')

class Drive:
    async def request(self, path, params=None):
        cred = await asyncio.to_thread(credentials)
        async with httpx.AsyncClient(timeout=90) as c:
            r = await c.get('https://www.googleapis.com/drive/v3/'+path,
                            headers={'Authorization':'Bearer '+cred.token},params=params)
            r.raise_for_status(); return r

    async def children(self, folder):
        files=[]; token=None
        while True:
            r = await self.request('files',{'q':f"'{folder}' in parents and trashed=false",'pageSize':100,
                'fields':'nextPageToken,files(id,name,mimeType,modifiedTime,webViewLink,parents)',
                'supportsAllDrives':'true','includeItemsFromAllDrives':'true', **({'pageToken':token} if token else {})})
            data = r.json(); files.extend(data.get('files',[])); token=data.get('nextPageToken')
            if not token: return files

    async def inventory(self):
        output=[]
        groups={'cv public':'candidate_public','cv private notes':'candidate_private',
                'jd public':'job_public','jd private notes':'job_private'}
        folder_groups={'15pdh17cz8MxI5_LR1u4r8Lz4uPXzqLxM':'candidate_public',
                       '13hQxacLUjmTaKySnnyYptJ6P8WvvfTaN':'candidate_private',
                       '1hEuV-KqIDaNFTpZ6U7OWK2oRrpwlnscZ':'job_public',
                       '1uzImJbufbsGoEye9fLZ2DZz4EEJeQASQ':'job_private'}
        for f in await self.children(FOLDER_ID):
            if f['mimeType']=='application/vnd.google-apps.folder':
                group=folder_groups.get(f['id']) or groups.get(f['name'].strip().lower())
                if not group: continue
                pending=[f['id']]
                while pending:
                    for child in await self.children(pending.pop()):
                        if child['mimeType']=='application/vnd.google-apps.folder': pending.append(child['id'])
                        else: output.append({**child,'group':group})
            else: output.append({**f,'group':'reference'})
        if not output: raise IntegrationError('Drive inventory rỗng: kiểm tra quyền truy cập folder.')
        return output

    async def text(self,f):
        if f['mimeType']=='application/vnd.google-apps.document':
            r=await self.request(f"files/{f['id']}/export",{'mimeType':'text/plain'})
            return r.content.decode('utf-8-sig')
        r=await self.request(f"files/{f['id']}",{'alt':'media','supportsAllDrives':'true'})
        return extract(r.content,f['mimeType'])

def extract(raw,mime):
    if mime=='application/pdf':
        try: text='\n'.join(p.extract_text() or '' for p in PdfReader(io.BytesIO(raw)).pages)
        except Exception: raise IntegrationError('PDF không đọc được hoặc bị mã hóa.')
        if len(text.strip())<40: raise IntegrationError('NEEDS_OCR: PDF scan/không có đủ văn bản.')
        return text
    if mime=='application/vnd.openxmlformats-officedocument.wordprocessingml.document':
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            root=ET.fromstring(z.read('word/document.xml'))
            return '\n'.join(''.join(n.itertext()) for n in root.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t'))
    if mime.startswith('text/'): return raw.decode('utf-8-sig')
    raise IntegrationError('UNSUPPORTED: định dạng chưa hỗ trợ trong pilot.')
