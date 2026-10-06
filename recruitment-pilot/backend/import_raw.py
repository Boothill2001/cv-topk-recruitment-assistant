import json,sys
import httpx
from pathlib import Path
from .integrations import extract

if __name__=='__main__':
    url=json.loads(sys.stdin.read())['url']
    r=httpx.get(url,timeout=90);r.raise_for_status()
    text=extract(r.content,'application/pdf')
    p=Path(__file__).resolve().parents[1]/'runtime'/'connector-snapshot.json'
    data=json.loads(p.read_text(encoding='utf-8'))
    for f in data:
        if f['id']=='1Bykund2QxiPCcmXWKH1OUp9eHVF5KeTg':f['text']=text;f.pop('fetch_error',None)
    p.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
    print('IT-600 raw PDF extracted:',len(text),'characters')
