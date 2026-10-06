"""Review exports: frozen matching snapshot, RAW cells, explicit human feedback import."""
import asyncio
import csv
import hashlib
import io
import json
import uuid
import httpx
from . import db,engine
from .errors import IntegrationError
from .integrations import credentials,redact_error

HEADERS=['Candidate ID','Job ID','Score','Coverage %','MUST','Title','Strengths','Gaps',
    'Salary / location / preferences (not scored)','Source links','Choose Round 2','Feedback','Recruiter note',
    'Run ID','Criteria version','Provider','Model','Prompt version','Candidate revision','Evidence (criterion / quote / source)','Retrieval priority R','Strategy fit S','Lane','Policy JSON']

class SheetHTTPError(IntegrationError):
    def __init__(self,status):
        self.status=status
        super().__init__('Google Sheets HTTP '+str(status)+'. Kiểm tra quyền/API/quota.')

def export_payload(run_id,threshold,top_k,live=True):
    with db.LOCK:
        run=db.one('SELECT * FROM runs WHERE id=?',(run_id,))
        if not run or run['status'] not in ('COMPLETED','PARTIAL','STALE'):
            raise IntegrationError('Matching cần hoàn tất trước khi xuất shortlist.')
        current=engine.current(run)
        if live and not current:raise IntegrationError('Run đã cũ. Tạo run mới trước khi xuất Sheet review.')
        snap=json.loads(run['snapshot'])
        results=[db.unpack(r) for r in db.rows("SELECT * FROM evaluations WHERE run_id=? AND status='COMPLETED'",(run_id,))]
        chosen=results if snap.get('assessment_engine') else [r for r in results if engine.eligible(r['data'],threshold,snap['config'])]
        chosen.sort(key=lambda r:(-r['data'].get('retrieval_score',r['data']['score']),-r['data'].get('coverage',0) if snap.get('assessment_engine') else 0,r['data'].get('be_rank',0),r['candidate_id']))
        chosen=chosen[:top_k]
        headings=HEADERS.copy()
        if snap.get('assessment_engine'):headings[2]='Score after AI assessment';headings[20]='BE retrieval priority (not AI score)'
        shortlist=[headings];evidence=[['Candidate ID','Criterion ID','Criterion','Priority','Assessment','Evidence quote','Source ID','Source revision','Source link','Explanation']]
        criterion={c['id']:c for c in snap['config']['criteria']}
        for r in chosen:
            eid=r['candidate_id'];p=snap['candidates'][eid];d=r['data']
            shortlist.append([eid,run['job_id'],d['score'],d['coverage'],('MET' if d['must_passed'] else 'UNKNOWN' if not d['must_complete'] else 'REVIEW'),p['profile']['title'],
                '\n'.join(d['strengths']),'\n'.join(d['gaps']),
                '\n'.join(s['summary'] for s in p['profile'].get('signals',[])),
                '\n'.join(s['url'] for s in p['sources']),False,'','',run_id,run['config_version'],
                run['provider'],run['model'],run['prompt_version'],p['revision'],
                '\n'.join(a['criterion_id']+': '+e['quote']+' ['+e['source_id']+']' for a in d['assessments'] for e in a['evidence']),d.get('be_retrieval_score',d.get('retrieval_score',d['score'])),d.get('strategy_score',0),d.get('lane','LEGACY'),db.dumps(snap['config'].get('policy',{}))])
            sources={s['source_id']:s for s in p['sources']}
            for a in d['assessments']:
                c=criterion[a['criterion_id']]
                for e in a['evidence'] or [{'quote':'','source_id':''}]:
                    s=sources.get(e['source_id'],{})
                    evidence.append([eid,c['id'],c['name'],c['type'],a['status'],e['quote'],e['source_id'],s.get('revision',''),s.get('url',''),a['explanation']])
        criteria=[['ID','Criterion','Priority','Enabled','Description']]+[[c['id'],c['name'],c['type'],c['enabled'],c['description']] for c in snap['config']['criteria']]
        info=[['Field','Value'],['Job',run['job_id']],['Run',run_id],['Created',run['created']],
            ['Run status at export',run['status']],['Current at export',current],['Threshold preview',threshold],
            ['TopK',top_k],['Exported candidates',len(chosen)],['Pool evaluated',len(snap['candidates'])],
            ['Provider',run['provider']],['Model',run['model']],['Prompt',run['prompt_version']],
            ['Criteria version',run['config_version']],['JD revision',snap['job']['revision']],
            ['Matching engine',snap.get('engine','legacy-llm')],['Policy',db.dumps(snap['config'].get('policy',{}))],
            ['Review','Edit only Choose Round 2, Feedback, Recruiter note. Keep candidate IDs/rows.'],
            ['Actions','Import review manually in localhost. No AI call or automatic Round 2.'],
            ['Coverage','UNKNOWN is missing evidence; other candidates remain in localhost full results.'],
            ['Sharing','Private spreadsheet in the signed-in Google account; share manually if desired.']]
        return {'run_id':run_id,'threshold':threshold,'top_k':top_k,'candidate_ids':[r['candidate_id'] for r in chosen],
            'tabs':{'Shortlist':shortlist,'Evidence':evidence,'Criteria':criteria,'Run_Info':info}}

def csv_bytes(payload):
    def safe(v):
        # CSV import may interpret formulas; Sheets API below uses RAW instead.
        if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@')):return "'"+v
        return v
    output=io.StringIO(newline='')
    csv.writer(output).writerows([[safe(v) for v in row] for row in payload['tabs']['Shortlist']])
    return ('\ufeff'+output.getvalue()).encode('utf-8')

def create_export(run_id,threshold,top_k):
    with db.LOCK:
        payload=export_payload(run_id,threshold,top_k)
        if not payload['candidate_ids']:raise IntegrationError('Không có hồ sơ đủ MUST và vượt ngưỡng; không tạo Sheet rỗng.')
        fingerprint=hashlib.sha256(db.dumps(payload).encode()).hexdigest()
        old=db.one('SELECT id,status,url,error FROM sheet_exports WHERE fingerprint=?',(fingerprint,))
        if old:return old
        eid=str(uuid.uuid4())
        db.execute('INSERT INTO sheet_exports(id,run_id,fingerprint,payload,status,created,updated) VALUES(?,?,?,?,?,?,?)',
            (eid,run_id,fingerprint,db.dumps(payload),'PENDING',db.now(),db.now()))
        engine.enqueue('sheet_export',{'export_id':eid})
        return {'id':eid,'status':'PENDING','url':None}

async def request(method,path,body=None,retry=True):
    # Refresh token off the event loop. Never include secrets in a URL/error body.
    for attempt in range(3 if retry else 1):
        try:
            cred=await asyncio.to_thread(credentials,True)
            async with httpx.AsyncClient(timeout=60) as client:
                res=await client.request(method,'https://sheets.googleapis.com/v4/spreadsheets'+path,
                    headers={'Authorization':'Bearer '+cred.token},json=body)
            if res.status_code>=400:
                if retry and (res.status_code==429 or res.status_code>=500) and attempt<2:
                    await asyncio.sleep(attempt+1);continue
                raise SheetHTTPError(res.status_code)
            return res.json()
        except (httpx.TimeoutException,httpx.TransportError):
            if retry and attempt<2:await asyncio.sleep(attempt+1);continue
            raise IntegrationError('Google Sheets timeout/network error.')

def formatting(payload):
    requests=[]
    for sid,(name,rows) in enumerate(payload['tabs'].items()):
        width=len(rows[0]);height=max(2,len(rows))
        requests.extend([
            {'updateSheetProperties':{'properties':{'sheetId':sid,'gridProperties':{'frozenRowCount':1}},'fields':'gridProperties.frozenRowCount'}},
            {'repeatCell':{'range':{'sheetId':sid,'startRowIndex':0,'endRowIndex':1},'cell':{'userEnteredFormat':{'backgroundColor':{'red':.10,'green':.20,'blue':.34},'textFormat':{'bold':True,'foregroundColor':{'red':1,'green':1,'blue':1}}}},'fields':'userEnteredFormat'}},
            {'repeatCell':{'range':{'sheetId':sid,'startRowIndex':1,'endRowIndex':height},'cell':{'userEnteredFormat':{'wrapStrategy':'WRAP','verticalAlignment':'TOP'}},'fields':'userEnteredFormat.wrapStrategy,userEnteredFormat.verticalAlignment'}},
            {'updateDimensionProperties':{'range':{'sheetId':sid,'dimension':'COLUMNS','startIndex':0,'endIndex':width},'properties':{'pixelSize':180},'fields':'pixelSize'}}])
        if name in ('Shortlist','Evidence','Criteria'):
            requests.append({'setBasicFilter':{'filter':{'range':{'sheetId':sid,'startRowIndex':0,'endRowIndex':height,'startColumnIndex':0,'endColumnIndex':width}}}})
        if name=='Shortlist':
            requests.extend([
                {'setDataValidation':{'range':{'sheetId':sid,'startRowIndex':1,'endRowIndex':height,'startColumnIndex':10,'endColumnIndex':11},'rule':{'condition':{'type':'BOOLEAN'},'strict':True,'showCustomUi':True}}},
                {'setDataValidation':{'range':{'sheetId':sid,'startRowIndex':1,'endRowIndex':height,'startColumnIndex':11,'endColumnIndex':12},'rule':{'condition':{'type':'ONE_OF_LIST','values':[{'userEnteredValue':v} for v in ('GOOD','NOT_GOOD','UNCERTAIN')]},'strict':True,'showCustomUi':True}}},
                {'addProtectedRange':{'protectedRange':{'range':{'sheetId':sid},'warningOnly':True,'description':'Review only K:M. Other columns are frozen matching evidence.','unprotectedRanges':[{'sheetId':sid,'startRowIndex':1,'endRowIndex':height,'startColumnIndex':10,'endColumnIndex':13}]}}}])
    return {'requests':requests}

async def publish(export_id):
    row=db.one('SELECT * FROM sheet_exports WHERE id=?',(export_id,))
    if not row or row['status']=='COMPLETED':return
    if row['status'] in ('CREATING','CREATE_UNCERTAIN'):
        raise IntegrationError('Tạo Sheet chưa xác định; kiểm tra Drive, không tự tạo lại.')
    payload=json.loads(row['payload'])
    db.execute("UPDATE sheet_exports SET status='RUNNING',updated=? WHERE id=?",(db.now(),export_id))
    try:
        sheet_id=row['spreadsheet_id']
        if not sheet_id:
            # Persist before non-idempotent creation. A crash/network ambiguity must
            # never silently create a second Sheet on restart/retry.
            db.execute("UPDATE sheet_exports SET status='CREATING',updated=? WHERE id=?",(db.now(),export_id))
            try:
                result=await request('POST','',{'properties':{'title':f"Recruitment {payload['tabs']['Run_Info'][1][1]} · {export_id[:8]}"},
                    'sheets':[{'properties':{'sheetId':sid,'title':name,'gridProperties':{'rowCount':max(100,len(rows)+10),'columnCount':max(26,len(rows[0]))}}} for sid,(name,rows) in enumerate(payload['tabs'].items())]},retry=False)
                sheet_id=result['spreadsheetId'];url=result['spreadsheetUrl']
            except Exception as exc:
                if isinstance(exc,SheetHTTPError) and 400<=exc.status<500:
                    db.execute("UPDATE sheet_exports SET status='FAILED',error=? WHERE id=?",(str(exc),export_id))
                else:
                    db.execute("UPDATE sheet_exports SET status='CREATE_UNCERTAIN',error=? WHERE id=?",('Chưa xác nhận tạo Sheet. Kiểm tra Google Drive theo mã '+export_id[:8]+'. Không tự tạo lại để tránh trùng.',export_id))
                raise
            db.execute('UPDATE sheet_exports SET spreadsheet_id=?,url=?,status=?,updated=? WHERE id=?',(sheet_id,url,'RUNNING',db.now(),export_id))
        await request('POST',f'/{sheet_id}/values:batchUpdate',{'valueInputOption':'RAW',
            'data':[{'range':f"'{name}'!A1",'values':rows} for name,rows in payload['tabs'].items()]})
        await request('POST',f'/{sheet_id}:batchUpdate',formatting(payload))
        db.execute("UPDATE sheet_exports SET status='COMPLETED',error=NULL,updated=? WHERE id=?",(db.now(),export_id))
    except Exception as exc:
        db.execute("UPDATE sheet_exports SET status=CASE WHEN status='CREATE_UNCERTAIN' THEN status ELSE 'FAILED' END,error=COALESCE(error,?),updated=? WHERE id=?",(redact_error(exc),db.now(),export_id))
        raise

def parse_review(payload,values):
    if not values or values[0][:len(HEADERS)]!=HEADERS:raise IntegrationError('Header Sheet đã đổi. Giữ nguyên cấu trúc export.')
    found={};selected=[];feedback=[]
    for row in values[1:]:
        if not row or not any(str(v).strip() for v in row):continue
        padded=row+['']*max(0,len(HEADERS)-len(row));eid=padded[0]
        if eid not in payload['candidate_ids'] or eid in found or padded[13]!=payload['run_id']:
            raise IntegrationError('Sheet có ID lạ/trùng hoặc sai run. Không nhập một phần.')
        found[eid]=True
        if padded[10] not in (True,False,'TRUE','FALSE',''):
            raise IntegrationError('Choose Round 2 phải là checkbox TRUE/FALSE.')
        if padded[10] is True or padded[10]=='TRUE':selected.append(eid)
        decision=padded[11];note=str(padded[12])
        if decision not in ('','GOOD','NOT_GOOD','UNCERTAIN') or len(note)>5000:
            raise IntegrationError('Feedback hoặc note không hợp lệ; chưa nhập dữ liệu.')
        if decision:feedback.append({'candidate_id':eid,'decision':decision,'note':note})
    if set(found)!=set(payload['candidate_ids']):raise IntegrationError('Sheet thiếu ứng viên export. Không nhập một phần.')
    if len(selected)>20:raise IntegrationError('Sheet tick hơn 20 người. Giảm nhóm trước khi nhập lựa chọn.')
    return selected,feedback

async def import_review(export_id):
    row=db.one('SELECT * FROM sheet_exports WHERE id=?',(export_id,))
    if not row or row['status']!='COMPLETED':raise IntegrationError('Sheet chưa xuất thành công.')
    run=db.one('SELECT * FROM runs WHERE id=?',(row['run_id'],))
    if not engine.current(run):raise IntegrationError('Run đã cũ; không áp dụng lựa chọn từ Sheet cũ.')
    payload=json.loads(row['payload'])
    # Read the whole bounded column set so extra/duplicate rows are detected too.
    result=await request('GET',f"/{row['spreadsheet_id']}/values/Shortlist!A:X")
    selected,feedback=parse_review(payload,result.get('values',[]))
    inserted=0
    snap=json.loads(run['snapshot'])
    with db.LOCK,db.conn() as c:
        if not engine.current(run):raise IntegrationError('Input đã đổi trong khi đọc Sheet; chưa nhập.')
        for f in feedback:
            key='sheet_feedback:'+export_id+':'+f['candidate_id'];encoded=db.dumps(f)
            previous=c.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
            if previous and previous['value']==encoded:continue
            c.execute('INSERT INTO feedback(run_id,candidate_id,decision,note,created) VALUES(?,?,?,?,?)',
                (row['run_id'],f['candidate_id'],f['decision'],f['note'],db.now()))
            c.execute('INSERT INTO labels(job_id,candidate_id,decision,note,profile_revision,job_revision,created) VALUES(?,?,?,?,?,?,?)',
                (run['job_id'],f['candidate_id'],f['decision'],f['note'],snap['candidates'][f['candidate_id']]['revision'],snap['job']['revision'],db.now()))
            c.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(key,encoded));inserted+=1
    return {'selected_ids':selected,'feedback_imported':inserted,'threshold':payload['threshold'],'top_k':payload['top_k'],
        'message':'Đã nhập review. Round 2 chỉ chạy khi bấm So sánh trong localhost.'}
