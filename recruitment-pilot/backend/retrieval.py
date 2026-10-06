"""Deterministic evidence-based scoring. This module has no LLM or file-reader imports."""
import unicodedata
from functools import lru_cache
from .models import RetrievalPolicy, Rule

VERSION = 'structured-2.0'
MAPPING_VERSION = 'aliases-1.1'
ALIASES = {'node.js':'nodejs','node js':'nodejs','postgres':'postgresql','react.js':'react',
           'reactjs':'react','js':'javascript','ts':'typescript','py':'python','go':'golang','nodejs':'nodejs'}

@lru_cache(maxsize=65536)
def canonical(value):
    value=' '.join(unicodedata.normalize('NFKC',str(value)).casefold().split())
    return ALIASES.get(value,value)

def policy(config):
    p=RetrievalPolicy.model_validate(config.get('policy',{})).model_dump()
    if set(p['weights'])!={'MUST','NICE','OPTIONAL'} or any(v<=0 for v in p['weights'].values()):
        raise ValueError('Cần trọng số dương cho MUST, NICE và OPTIONAL.')
    return p

def predicate_assessment(predicate,facts):
    relevant=facts.get(predicate['field'],[]) if isinstance(facts,dict) else [f for f in facts if f['field']==predicate['field'] and f.get('evidence')]
    values={canonical(v) for v in predicate['values']}
    hits=[];failures=[]
    numeric=predicate['operator'] in ('gte','lte')
    for f in relevant:
        if numeric:
            n=f.get('numeric_value')
            if n is None or f.get('polarity','POSITIVE')!='POSITIVE':continue
            hit=n>=predicate['number'] if predicate['operator']=='gte' else n<=predicate['number']
            (hits if hit else failures).append(f)
        elif canonical(f['value']) in values:
            (hits if f.get('polarity','POSITIVE')=='POSITIVE' else failures).append(f)
    if numeric:
        status='UNKNOWN' if hits and failures else 'MET' if hits else 'NOT_MET' if failures else 'UNKNOWN'
        selected=[] if status=='UNKNOWN' else hits or failures
        certainty=0 if status=='UNKNOWN' else 1
    else:
        positive={canonical(f['value']) for f in hits};negative={canonical(f['value']) for f in failures}
        conflicts=positive & negative
        positive-=conflicts;negative-=conflicts
        minimum=int(predicate['number']) if predicate['operator']=='at_least' else 1
        if len(positive)>=minimum:
            status='MET';selected=[f for f in hits if canonical(f['value']) in positive];certainty=1
        elif len(values)-len(negative)<minimum:
            status='NOT_MET';selected=[f for f in failures if canonical(f['value']) in negative];certainty=1
        elif positive:
            status='PARTIAL';selected=[f for f in hits if canonical(f['value']) in positive];certainty=(len(positive)+len(negative))/len(values)
        else:status='UNKNOWN';selected=[];certainty=0
    return {'status':status,'evidence':[e for f in selected for e in f['evidence']],'certainty':certainty,
            'explanation':'Đối chiếu fact có evidence.' if selected else 'Chưa có fact chứng minh hoặc nguồn mâu thuẫn; cần xác minh.'}

def assess(rule,facts):
    if not rule:
        return {'status':'UNKNOWN','evidence':[],'explanation':'Criterion chưa có rule thực thi; cần review thủ công.'}
    parsed=rule # Approved configs have already passed Pydantic and whitelist validation.
    parts=[predicate_assessment(p,facts) for p in parsed['predicates']]
    states=[p['status'] for p in parts]
    if parsed['mode']=='ANY':
        status='MET' if 'MET' in states else 'PARTIAL' if 'PARTIAL' in states else 'NOT_MET' if all(s=='NOT_MET' for s in states) else 'UNKNOWN'
    else:
        status=('MET' if all(s=='MET' for s in states) else 'NOT_MET' if 'NOT_MET' in states
                else 'PARTIAL' if any(v in ('MET','PARTIAL') for v in states) else 'UNKNOWN')
    certainty=(1 if status=='MET' and parsed['mode']=='ANY' else
               sum(p['weight']*a.get('certainty',0 if a['status']=='UNKNOWN' else 1) for p,a in zip(parsed['predicates'],parts))/sum(p['weight'] for p in parsed['predicates']))
    return {'status':status,'evidence':[] if status=='UNKNOWN' else [e for p in parts for e in p['evidence']],
            'explanation':' / '.join(p['explanation'] for p in parts), 'predicate_results':parts,'certainty':certainty}

def validate_rule(rule):
    r=Rule.model_validate(rule).model_dump()
    for p in r['predicates']:
        numeric=p['operator'] in ('gte','lte')
        if numeric and (p['field'] not in ('years_experience','management_years') or p['number'] is None):
            raise ValueError('Rule số chỉ áp dụng years_experience/management_years với number rõ ràng.')
        if not numeric and not p['values']:raise ValueError('Rule text cần values.')
        if p['operator']=='at_least' and (p['number'] is None or p['number']!=int(p['number']) or not 1<=p['number']<=len({canonical(v) for v in p['values']})):
            raise ValueError('at_least cần number nguyên từ 1 đến số giá trị khác nhau.')
    return r

def calculate(config,evaluation):
    p=policy(config);criteria=[c for c in config['criteria'] if c['enabled']]
    given={a['criterion_id']:a for a in evaluation['assessments']}
    total=sum(p['weights'][c['type']] for c in criteria)
    known=[c for c in criteria if given.get(c['id'],{}).get('status','UNKNOWN')!='UNKNOWN']
    earned=sum(p['weights'][c['type']]*{'MET':100,'PARTIAL':50,'NOT_MET':0}[given[c['id']]['status']] for c in known)
    must=[given.get(c['id'],{}).get('status','UNKNOWN') for c in criteria if c['type']=='MUST']
    unknown_must=any(given.get(c['id'],{}).get('status','UNKNOWN')=='UNKNOWN' or
                     (given.get(c['id'],{}).get('status')=='PARTIAL' and given[c['id']].get('certainty',1)<1)
                     for c in criteria if c['type']=='MUST')
    return {'score':round(earned/total,2) if total else None,
            'coverage':round(sum(p['weights'][c['type']]*given[c['id']].get('certainty',1) for c in known)/total*100,2) if total else 0,
            'must_complete':not unknown_must,'must_passed':all(s=='MET' for s in must),
            'must_failure':any(s=='NOT_MET' for s in must),'must_partial':any(s=='PARTIAL' for s in must)}

def lane(data,config,threshold=None):
    p=policy(config)
    if threshold is not None:p['threshold']=threshold
    if not data.get('must_complete'):return 'NEEDS_VERIFICATION'
    gate_ok=(p['gate']=='REVIEW_ONLY' or (not data.get('must_failure') if p['gate']=='NO_CONFIRMED_FAILURE' else data.get('must_passed',False)))
    score=data.get('score')
    score_ok=(not p['threshold_enabled'] or (score is not None and (score>p['threshold'] if p['threshold_operator']=='>' else score>=p['threshold'])))
    if gate_ok and score_ok:return 'QUALIFIED'
    if data.get('must_partial') and not data.get('must_failure'):return 'NEEDS_VERIFICATION'
    return 'BELOW_POLICY'

def evaluate(config,profile):
    facts={}
    for f in profile.get('facts',[]):
        if f.get('evidence'):facts.setdefault(f['field'],[]).append(f)
    assessments=[{'criterion_id':c['id'],**assess(c.get('rule'),facts)} for c in config['criteria'] if c['enabled']]
    result={'assessments':assessments,'strategy_ids':[],'strengths':[],'gaps':[],'strategy_details':[]}
    result.update(calculate(config,result))
    for strategy in config.get('strategies',[]):
        if not strategy['enabled']:continue
        rule=strategy.get('rule')
        if not rule:continue
        detail=assess(rule,facts)
        predicates=rule['predicates']
        # ALL rewards coverage of the conjunction; ANY rewards its best supported branch.
        points=[100 if a['status']=='MET' else 50 if a['status']=='PARTIAL' else 0 for a in detail['predicate_results']]
        s=(max(points) if rule['mode']=='ANY' else sum(v*p.get('weight',1) for v,p in zip(points,predicates))/sum(p.get('weight',1) for p in predicates))
        result['strategy_details'].append({'strategy_id':strategy['id'],'score':round(s,2),**detail})
        if s>0:result['strategy_ids'].append(strategy['id'])
    result['strategy_score']=max((s['score'] for s in result['strategy_details']),default=0)
    alpha=policy(config)['alpha'] if result['strategy_details'] else 0
    result['retrieval_score']=round((1-alpha)*(result['score'] or 0)+alpha*result['strategy_score'],2)
    result['lane']=lane(result,config)
    result['strengths']=[c['name'] for c in config['criteria'] if any(a['criterion_id']==c['id'] and a['status']=='MET' for a in assessments)]
    result['gaps']=[a['criterion_id']+': '+a['status'] for a in assessments if a['status']!='MET']
    # Keep evidence once per assessment, not duplicated again in every predicate.
    for a in assessments+result['strategy_details']:
        a['predicate_statuses']=[p['status'] for p in a.pop('predicate_results',[])]
    result['scorer_version']=VERSION
    return result

class SearchProvider:
    """Future Jev adapter returns IDs and evidence references, never SQL or opaque merged scores."""
    async def retrieve(self,job,strategies,top_k):raise NotImplementedError
