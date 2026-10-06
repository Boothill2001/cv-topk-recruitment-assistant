"""Proposals only. Saving one uses the normal versioned, human approval flow."""
import copy,re
from .retrieval import canonical

def propose(config):
    result=copy.deepcopy(config);changes=[]
    for c in result['criteria']:
        rule=c.get('rule')
        if not rule:continue
        text=c['name']+' '+c.get('description','')
        alternatives=re.search(r'\b(or|either)\b|hoặc|một trong',text,re.I)
        for p in rule['predicates']:
            if p['field']=='skills' and p['operator']=='any_of' and len(p['values'])>1:
                # Conservative: require an explicit conjunction, and every listed term in the name.
                name=c['name'].casefold()
                if not alternatives and ('&' in name or ' và ' in name or ' and ' in name) and all(v.casefold() in name for v in p['values']):
                    p['operator']='at_least';p['number']=len({canonical(v) for v in p['values']})
                    changes.append({'criterion_id':c['id'],'message':'Cần tất cả kỹ năng đã nêu, không phải một trong.'})
        if re.search(r'adtech|high[ -]?load|tải cao',text,re.I) and any(p['field']=='company_context' and 'multinational' in [canonical(v) for v in p['values']] for p in rule['predicates']):
            # A null rule is honest when the expression cannot capture the business requirement.
            c['rule']=None
            changes.append({'criterion_id':c['id'],'message':'Bỏ proxy multinational cho AdTech/high-load. Chờ khách xác nhận điều kiện chuyên môn; AI đối chiếu mô tả và evidence.'})
    return {'config':result,'changes':changes,'requires_new_version':bool(changes)}
