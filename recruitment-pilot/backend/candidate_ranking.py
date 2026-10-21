"""Read-side identity and recruiter review ordering; stored scores stay immutable."""
from . import engine
from .source_identity import profile_key

VERSION = 'must-review-1'
GROUPS = {'MUST_MET': 0, 'MUST_UNCLEAR': 1, 'MUST_NOT_MET': 2}

def canonical_url(url):
    return profile_key(url)

def identity(url):
    return 'CAN' + engine.digest(canonical_url(url))[:24]

def review_group(result, criteria):
    statuses = {a['criterion_id']: a['status'] for a in result.get('assessments', [])}
    must = [statuses.get(c['id'], 'UNKNOWN') for c in criteria if c.get('enabled', True) and c['type'] == 'MUST']
    if 'NOT_MET' in must:
        return 'MUST_NOT_MET'
    return 'MUST_MET' if must and all(s == 'MET' for s in must) else 'MUST_UNCLEAR'

def ranked_candidates(results, criteria):
    """Choose earliest frozen evaluated source; never combine different revisions."""
    grouped = {}
    for result in sorted(results, key=lambda r: (r['be_rank'], r['candidate_id'])):
        cid = identity(result['url'])
        if cid not in grouped:
            grouped[cid] = {**result, 'canonical_candidate_id': cid, 'source_urls': [],
                            'review_group': review_group(result, criteria),
                            'ranking_policy_version': VERSION, 'source_results': []}
        row = grouped[cid]
        if result['url'] not in row['source_urls']:
            row['source_urls'].append(result['url'])
        row['source_results'].append({k: result.get(k) for k in ('url', 'assessment_id', 'child_source_id', 'adapter_version')})
    return sorted(grouped.values(), key=lambda r: (GROUPS[r['review_group']], -(r.get('score') or 0), -r['coverage'], r['be_rank'], r['candidate_id']))
