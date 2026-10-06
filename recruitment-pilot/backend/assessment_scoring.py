"""Server-owned scoring and stable ordering after the AI has supplied statuses."""
from . import retrieval

VERSION='assessment-scoring-1.0'

def score(config,item,be_rank):
    result={**item,**retrieval.calculate(config,item),'be_rank':be_rank,'scorer_version':VERSION}
    result['lane']=retrieval.lane(result,config)
    return result

def ordered(items):
    return sorted(items,key=lambda r:(-(r.get('score') or 0),-r['coverage'],r['be_rank'],r['candidate_id']))
