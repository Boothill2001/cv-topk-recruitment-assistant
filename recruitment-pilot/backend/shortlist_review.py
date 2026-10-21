"""Frozen blind review samples and human labels, never fed into AI prompts."""
from . import db, engine, assessment_groups
from .errors import IntegrationError
from .candidate_ranking import identity

@engine.serialized
def sample(gid):
    key='shortlist_review:'+gid
    saved=db.setting(key,None)
    if saved is None:
        report=assessment_groups.detail(gid)
        rows=report['results']
        selected=rows[:10]+rows[10:15]
        # Stable presentation independent from AI rank. Hidden rank remains server-side.
        items=[{'id':r['canonical_candidate_id'],'url':r['url'],'title':r.get('title'),
                'source_urls':r['source_urls'],'assessment_id':r['assessment_id'],
                'source_id':r['child_source_id'],'top10':i<10} for i,r in enumerate(selected)]
        items.sort(key=lambda r:engine.digest(gid+r['id']))
        saved={'group_id':gid,'config_version':report['config_version'],'job_id':report['job_id'],
               'created':db.now(),'items':items,'labels':{},'history':[]}
        db.set_setting(key,saved)
    return saved

def blind(gid):
    saved=sample(gid)
    return {k:v for k,v in saved.items() if k not in ('items','history')} | {
        'items':[{k:v for k,v in r.items() if k!='top10'} for r in saved['items']],
        'blind':True,'quality_accepted':False}

@engine.serialized
def label(gid,cid,value,reason,criteria,expected):
    saved=sample(gid)
    if cid not in {r['id'] for r in saved['items']}:raise IntegrationError('Hồ sơ không thuộc tập review đã chốt.')
    previous=saved['labels'].get(cid)
    if (previous or {}).get('version',0)!=expected:raise IntegrationError('Review đã thay đổi; tải lại trước khi lưu.')
    item={'label':value,'reason':reason,'criterion_ids':criteria,'version':expected+1,'updated':db.now()}
    saved['labels'][cid]=item;saved['history'].append({'candidate_id':cid,**item})
    db.set_setting('shortlist_review:'+gid,saved)
    return blind(gid)

def metrics(gid):
    saved=sample(gid); labels=saved['labels']; reviewed=[r for r in saved['items'] if r['id'] in labels]
    return {'reviewed':len(reviewed),'sample_size':len(saved['items']),
      'top10_worth_contact':sum(r['top10'] and labels[r['id']]['label']=='CONTACT' for r in reviewed),
      'outside_top10_worth_contact':sum(not r['top10'] and labels[r['id']]['label']=='CONTACT' for r in reviewed),
      'needs_verification':sum(labels[r['id']]['label']=='VERIFY' for r in reviewed),
      'controls':saved.get('controls',[]),
      'quality_accepted':False,'interpretation':'Observed labels only; no acceptance threshold has been agreed.'}

@engine.serialized
def add_control(gid,url,reason):
    from urllib.parse import urlsplit
    if urlsplit(url).scheme not in ('http','https') or not urlsplit(url).hostname:
        raise IntegrationError('Cần URL hồ sơ http/https hợp lệ.')
    saved=sample(gid);controls=saved.setdefault('controls',[])
    if len(controls)>=5:raise IntegrationError('Tối đa 5 hồ sơ đối chứng cho một tập review.')
    if any(identity(c['url'])==identity(url) for c in controls):raise IntegrationError('Hồ sơ đối chứng đã có.')
    sources=assessment_groups.row(gid)['snapshot']['sources']
    controls.append({'url':url,'reason':reason,'created':db.now(),
        'found_in_search':any(identity(s['url'])==identity(url) for s in sources)})
    db.set_setting('shortlist_review:'+gid,saved)
    return blind(gid)
