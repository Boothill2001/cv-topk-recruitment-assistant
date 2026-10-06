"""One-time local OCR recovery of connector PDFs. Does not synthesize CV/JD text."""
import json,subprocess,sys,tempfile
from pathlib import Path
import pypdfium2 as pdfium
from . import db

def ocr(path):
    document=pdfium.PdfDocument(str(path));texts=[]
    with tempfile.TemporaryDirectory(dir=db.RUNTIME) as temp:
        for index in range(len(document)):
            page=document[index];bitmap=page.render(scale=3)
            png=Path(temp)/f'page-{index}.png'
            bitmap.to_pil().save(png)
            result=subprocess.run([r'C:\Program Files\Tesseract-OCR\tesseract.exe',str(png),'stdout','-l','eng','--psm','3'],capture_output=True,check=True)
            texts.append(result.stdout.decode('utf-8'))
            bitmap.close();page.close()
    document.close();return '\n'.join(texts)

if __name__=='__main__':
    file_id,path=sys.argv[1:]
    text=ocr(Path(path))
    if len(text.strip())<40:raise RuntimeError('OCR không có đủ văn bản')
    p=db.RUNTIME/'connector-snapshot.json';data=json.loads(p.read_text(encoding='utf-8'))
    f=next(f for f in data if f['id']==file_id);f['text']=text;f.pop('fetch_error',None)
    p.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
    db.set_setting('extraction:'+file_id,{'method':'Tesseract OCR (eng)','review_required':True})
    print('OCR recovered',file_id, len(text),'characters; human source review required')
