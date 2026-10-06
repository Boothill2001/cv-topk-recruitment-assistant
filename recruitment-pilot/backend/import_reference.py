import json,sys
from . import db
if __name__=='__main__':
    ref=json.loads((db.RUNTIME/'reference.json').read_text(encoding='utf-8'))
    p=db.RUNTIME/'connector-snapshot.json';items=json.loads(p.read_text(encoding='utf-8'))
    items=[f for f in items if f['id']!=ref['id']];items.append(ref)
    for item in items:
        existing=db.one("SELECT text FROM files WHERE id=? AND status IN ('READY','REFERENCE')",(item['id'],))
        if existing and existing['text']:
            item['text']=existing['text'];item.pop('fetch_error',None)
    temporary=p.with_suffix('.tmp')
    temporary.write_text(json.dumps(items,ensure_ascii=False),encoding='utf-8')
    temporary.replace(p)
    print('Reference source added; snapshot files:',len(items))
