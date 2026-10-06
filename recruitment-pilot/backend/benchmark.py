"""Policy evaluation against explicit recruiter labels; uncertain is never negative."""
import itertools
import json
from . import db,retrieval

def evaluate():
    labels=db.rows('SELECT * FROM labels ORDER BY id')
    latest={(r['job_id'],r['candidate_id']):r for r in labels}
    profiles={r['id']:r for r in db.rows("SELECT * FROM profiles WHERE kind='candidate' AND status='READY'")}
    jobs={r['id']:r for r in db.rows("SELECT * FROM profiles WHERE kind='job' AND status='READY'")}
    configs={r['job_id']:json.loads(r['data']) for r in db.rows('SELECT * FROM configs')}
    usable={key:r for key,r in latest.items() if r['decision']!='UNCERTAIN' and key[0] in jobs and key[1] in profiles
            and r['profile_revision']==profiles[key[1]]['revision'] and r['job_revision']==jobs[key[0]]['revision']}
    metrics=[]
    for gate,threshold,alpha,k in itertools.product(['ALL_MET','NO_CONFIRMED_FAILURE','REVIEW_ONLY'],[0,70,80,85,90],[0,.1,.2],[5,10,20]):
        tp=positives=negatives=fp=gate_misses=threshold_misses=unjudged=0;per_job=[]
        for job,config in configs.items():
            job_labels={eid:r['decision'] for (jid,eid),r in usable.items() if jid==job}
            if not job_labels:continue
            cfg={**config,'policy':{**retrieval.policy(config),'gate':gate,'threshold':threshold,'alpha':alpha}}
            results={eid:retrieval.evaluate(cfg,json.loads(p['data'])) for eid,p in profiles.items() if json.loads(p['data']).get('_meta',{}).get('schema_version')==2}
            ranked=sorted((eid for eid,d in results.items() if d['lane']=='QUALIFIED'),key=lambda eid:(-results[eid]['retrieval_score'],eid))[:k]
            good={eid for eid,d in job_labels.items() if d=='GOOD'};bad={eid for eid,d in job_labels.items() if d=='NOT_GOOD'}
            hits=len(good&set(ranked));wrong=len(bad&set(ranked));missing=sorted(good-set(ranked))
            tp+=hits;fp+=wrong;positives+=len(good);negatives+=len(bad);unjudged+=len(set(ranked)-set(job_labels))
            for eid in missing:
                d=results.get(eid)
                if not d:continue
                if d['lane']=='NEEDS_VERIFICATION' or (gate=='ALL_MET' and not d['must_passed']) or (gate=='NO_CONFIRMED_FAILURE' and d['must_failure']):gate_misses+=1
                elif d['score'] is None or d['score']<=threshold:threshold_misses+=1
            per_job.append({'job_id':job,'positive_labels':len(good),'true_positive':hits,'false_positive':wrong,'recall':hits/len(good) if good else None,'missed_good_ids':missing,'ranked_ids':ranked})
        metrics.append({'gate':gate,'threshold':threshold,'alpha':alpha,'top_k':k,'recall':tp/positives if positives else None,
                        'precision_on_judged':tp/(tp+fp) if tp+fp else None,'positive_labels':positives,'negative_labels':negatives,
                        'unjudged_in_topk':unjudged,'missed_by_gate_or_unknown':gate_misses,'missed_by_threshold':threshold_misses,'per_job':per_job})
    distinct=sorted({job for job,_ in usable})
    # Leave-one-job-out: choose policy on the other jobs, report its held-out recall.
    folds=[]
    if len(distinct)>=2:
        for held in distinct:
            def train_score(m):
                values=[j['recall'] for j in m['per_job'] if j['job_id']!=held and j['recall'] is not None]
                return sum(values)/len(values) if values else -1
            def train_precision(m):
                rows=[j for j in m['per_job'] if j['job_id']!=held]
                tp=sum(j['true_positive'] for j in rows);fp=sum(j['false_positive'] for j in rows)
                return tp/(tp+fp) if tp+fp else 0
            winner=max(metrics,key=lambda m:(train_score(m),train_precision(m),-m['top_k']))
            held_result=next((j for j in winner['per_job'] if j['job_id']==held),None)
            folds.append({'held_out_job':held,'chosen_policy':{k:winner[k] for k in ('gate','threshold','alpha','top_k')},'held_out':held_result})
    result={'at':db.now(),'scorer_version':retrieval.VERSION,
            'input_revisions':{'configs':{r['job_id']:{'version':r['version'],'approved':bool(r['approved'])} for r in db.rows('SELECT job_id,version,approved FROM configs')},
                               'candidates':{eid:p['revision'] for eid,p in profiles.items()},'jobs':{eid:p['revision'] for eid,p in jobs.items()}},
            'labeled_pairs':len(usable),'uncertain_or_stale_or_missing_pairs':len(latest)-len(usable),'jobs':distinct,
            'status':'PILOT_ONLY' if len(distinct)>=2 else 'INSUFFICIENT_LABELS','metrics':metrics,'leave_one_job_out':folds,
            'note':'Không tự đổi policy. Recall/precision chỉ có ý nghĩa trên nhãn đã review; unjudged không phải negative.'}
    ident=db.execute('INSERT INTO benchmark_runs(data,created) VALUES(?,?)',(db.dumps(result),db.now()))
    return {'id':ident,**result}
