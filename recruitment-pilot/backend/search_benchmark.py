"""Separate retrieval recall from reranking quality; never create customer labels."""
import json
from . import db,assessment_scoring

def evaluate(search):
    snap=json.loads(search['snapshot'])
    latest={r['candidate_id']:r for r in db.rows('SELECT * FROM labels WHERE job_id=? ORDER BY id',(search['job_id'],))}
    run=db.one('SELECT snapshot FROM runs WHERE id=?',(search['retrieval_run_id'],))
    pool=json.loads(run['snapshot'])['candidates']
    usable={eid:r['decision'] for eid,r in latest.items() if eid in pool and r['decision'] in ('GOOD','NOT_GOOD')
            and r['profile_revision']==pool[eid]['revision'] and r['job_revision']==snap['job']['revision']}
    good={eid for eid,d in usable.items() if d=='GOOD'};group=set(snap['group'])
    ai=assessment_scoring.ordered([json.loads(r['data']) for r in db.rows('SELECT data FROM search_assessments WHERE search_id=?',(search['id'],))])
    top={r['candidate_id'] for r in ai[:10]};judged=top & set(usable)
    return {'status':'PILOT_ONLY' if good else 'INSUFFICIENT_LABELS','labeled_pairs':len(usable),
            'retrieval':{'input_count':len(group),'recall_at_input_count':len(good & group)/len(good) if good else None,
                         'good_outside_group':sorted(good-group),'positive_labels':len(good)},
            'ai_ranking':{'precision_on_judged_at_10':len(judged & good)/len(judged) if judged else None,
                          'judged_at_10':len(judged),'unjudged_at_10':len(top-judged),'good_in_top10':sorted(top & good)},
            'note':'Nhãn chỉ do khách xác nhận. Recall BE và chất lượng thứ hạng AI là hai phép đo riêng; chưa có nhãn thì không kết luận accuracy.'}
