"""Jev on immutable stored CV text. Never fetch PDFs or send private notes."""
import json
import uuid
from . import db, engine, jev, assessment_scoring
from .content_fetch import blocks
from .public_assessment import paragraph_catalog
from .errors import IntegrationError

VERSION = 'internal-jev-1'

def build(snapshot, exact_offsets=False):
    sources=[]; missing={}
    for rank,(eid,candidate) in enumerate(snapshot['group'].items(),1):
        sid=f'PUB{rank:03d}'; evidence={}; kinds={}; context=[]; texts=[]; provenance={}; offsets={}; invalid=False
        for index,meta in enumerate(candidate['sources'],1):
            if meta.get('group')!='candidate_public':continue
            row=db.one('SELECT data FROM source_revisions WHERE file_id=? AND revision=?',(meta['source_id'],meta['revision']))
            if not row:
                invalid=True
                continue
            record=json.loads(row['data']); text=record.get('text') or ''
            if record.get('entity_id')!=eid or record.get('group_name')!='candidate_public' or not text.strip():
                invalid=True
                continue
            if exact_offsets:
                if record.get('status') not in ('READY','DUPLICATE') or (meta.get('hash') and engine.digest(text)!=meta['hash']):
                    invalid=True
                    continue
                chunks=blocks(text,deduplicate=False,company_headers=True); cursor=0; base=sum(len(t)+2 for t in texts)
                for n,p in enumerate(chunks):
                    pos=text.find(p['text'],cursor)
                    if pos<0:raise IntegrationError('Catalog không khớp vị trí trong CV.')
                    cursor=pos+len(p['text']);p['id']=f'P{n:05d}'
                    ref=sid+f'.F{index:03d}.'+p['id']
                    offsets[ref]={'start':base+pos,'end':base+cursor}
                catalog=paragraph_catalog(chunks,sid+f'.F{index:03d}')
            else:catalog=paragraph_catalog(blocks(text),sid+f'.F{index:03d}')
            evidence.update(catalog['evidence']);kinds.update(catalog['paragraph_kinds']);context.extend(catalog['context']);texts.append(text)
            for ref in catalog['evidence']:
                provenance[ref]={'source_id':meta['source_id'],'revision':meta['revision'],'paragraph_id':ref,
                                 **({'start':offsets[ref]['start']-base,'end':offsets[ref]['end']-base} if exact_offsets else {})}
        if not texts or (exact_offsets and invalid):
            missing[eid]=('Thiếu text CV được phép đánh giá hoặc sai revision/hash; kiểm tra nguồn, không tự đọc lại PDF.' if exact_offsets else 'Không có text CV public đúng revision đã lưu; không đọc lại PDF hoặc dùng private note.')
            continue
        sources.append({'id':sid,'url':eid,'title':candidate['profile'].get('title') or eid,
          'revision':engine.digest(db.dumps({'texts':texts,'provenance':provenance})),
          'raw_text':'\n\n'.join(texts),'evidence':evidence,'paragraph_kinds':kinds,'context':context,
          'provenance':provenance,**({'evidence_offsets':{ref:offsets[ref] for ref in evidence}} if exact_offsets else {}),
          'provider_source':'stored-cv-revisions','be_rank':candidate['be_rank']})
    return {'sources':sources,'criteria':[c for c in snapshot['config']['criteria'] if c['enabled']],
            'config':snapshot['config'],'content_fetch_id':'stored-cv','model':jev.MODEL,
            'adapter_version':jev.VERSION,'assessment_engine':'JEV','missing':missing,
            'policy':jev.POLICY,'rubric':jev.RUBRIC,'prompt_hash':engine.digest(db.dumps([jev.POLICY,jev.RUBRIC]))}

def remap(report,snapshot):
    sources={s['url']:s for s in snapshot['jev']['sources']}; results=[]
    for raw in report['results']:
        eid=raw['url']; item={**raw,'candidate_id':eid,'be_rank':snapshot['group'][eid]['be_rank']}
        for assessment in item['assessments']:
            assessment['evidence']=[{**e,**sources[eid]['provenance'][e['paragraph_id']]} for e in assessment['evidence']]
        be=snapshot['group'][eid]['be_evaluation'];item.update(be_score=be['score'],be_retrieval_score=be.get('retrieval_score',be['score']))
        results.append(item)
    return {**report,'results':assessment_scoring.ordered(results),'source_errors':{**snapshot['jev']['missing'],**report['source_errors']}}

async def assess(s):
    from .search_orchestration import current
    sid=s['id'];snapshot=json.loads(s['snapshot'])
    if not current(s):raise IntegrationError('JD hoặc CV đã đổi; tạo lượt mới để giữ đúng revision.')
    if s['model']!=jev.MODEL or s['prompt_version']!=VERSION or snapshot.get('internal_adapter_version')!=VERSION or snapshot['jev']['adapter_version']!=jev.VERSION:raise IntegrationError('Phiên bản Jev đã đổi; không fallback.')
    if snapshot['jev'].get('prompt_hash')!=engine.digest(db.dumps([jev.POLICY,jev.RUBRIC])):raise IntegrationError('Policy Jev đã đổi; tạo lượt mới, giữ lịch sử cũ.')
    db.execute("UPDATE searches SET status='RUNNING',attempts=attempts+1,error=NULL,updated=? WHERE id=?",(db.now(),sid))
    def progress(report):
        mapped=remap(report,snapshot)
        db.set_setting('search_preview:'+sid,{'items':mapped['results'],'jev':mapped})
    report=remap(await jev.judge(sid,snapshot['jev'],on_progress=progress),snapshot)
    final=str(uuid.uuid4()); original=json.loads(db.one('SELECT snapshot FROM runs WHERE id=?',(s['retrieval_run_id'],))['snapshot'])
    original.update(assessment_engine=VERSION,search_id=sid)
    state='PARTIAL' if report['source_errors'] else 'COMPLETED'
    if not current(s):state='STALE'
    with db.LOCK,db.conn() as conn:
        conn.execute('INSERT INTO runs(id,job_id,status,config_version,snapshot,model,prompt_version,created,updated,provider) VALUES(?,?,?,?,?,?,?,?,?,?)',
          (final,s['job_id'],state,s['config_version'],db.dumps(original),s['model'],VERSION,db.now(),db.now(),'typesafe'))
        for item in report['results']:
            item['retrieval_score']=item['score']
            conn.execute('INSERT INTO search_assessments VALUES(?,?,?)',(sid,item['candidate_id'],db.dumps(item)))
            conn.execute('INSERT INTO evaluations VALUES(?,?,?,?,?)',(final,item['candidate_id'],db.dumps(item),'COMPLETED',None))
        conn.execute('UPDATE searches SET status=?,result_run_id=?,usage=?,error=?,updated=? WHERE id=?',
          (state,final,db.dumps(report['usage']),'Có nguồn chưa chấm; xem lý do từng nguồn.' if report['source_errors'] else None,db.now(),sid))
    db.set_setting('internal_jev_report:'+sid,report)

def recover():
    for row in db.rows("SELECT id,snapshot FROM searches WHERE status='INTERRUPTED'"):
        if json.loads(row['snapshot']).get('inbound_review_set'):continue  # Explicit continuation, never replay uncertain paid work.
        if json.loads(row['snapshot']).get('assessment_engine')!='JEV':continue
        db.execute("UPDATE searches SET status='PENDING' WHERE id=?",(row['id'],))
        engine.enqueue('batch_assessment',{'search_id':row['id']})
