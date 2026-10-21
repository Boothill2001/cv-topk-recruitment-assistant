"""Lossless local request planning. No provider calls or token-count API."""
import re
from . import db,engine
from .errors import IntegrationError

VERSION = 'lossless-parts-1'

def slices(text, size=6000):
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            candidates = [m.end() + start for m in re.finditer(r'[.!?。！？]\s+|\n', text[start:end])]
            if candidates and candidates[-1] > start + size//2: end = candidates[-1]
        yield start, end, text[start:end]
        start = end

def parts(source):
    """Partition the original string exactly; catalog entries carry only offsets."""
    raw = source.get('raw_text')
    if not raw: raw = '\n\n'.join([*source['evidence'].values(), *source.get('context', [])])
    if not raw.strip(): raise IntegrationError('Nguồn không có text khả dụng.')
    locations = []
    for ref, text in source['evidence'].items():
        offset = source.get('evidence_offsets',{}).get(ref)
        pos = offset['start'] if offset else raw.find(text)
        if pos < 0: raise IntegrationError('Catalog không khớp văn bản gốc; cần kiểm tra revision.')
        if raw[pos:pos+len(text)]!=text or (offset and offset['end']!=pos+len(text)):
            raise IntegrationError('Evidence sai vị trí/revision; không gửi provider.')
        locations.append((ref, pos, pos + len(text)))
    result = []
    for i, (start, end, text) in enumerate(slices(raw, len(raw) if len(raw.encode('utf-8'))<=12000 else 6000)):
        refs = {ref: {'start': max(a,start)-start, 'end': min(b,end)-start}
                for ref,a,b in locations if a < end and b > start}
        result.append({'source_id':source['id'],'revision':source['revision'],
                       'part_id':f'PART{i:05d}','start':start,'end':end,
                       'text':text,'professional_paragraphs':refs})
    if ''.join(p['text'] for p in result) != raw: raise IntegrationError('Phân đoạn mất nội dung.')
    return result

def rubric(criteria):
    """Preserve approved meaning verbatim; executable predicates are optional."""
    result = []
    for c in criteria:
        rule = c.get('rule') or {}
        if rule.get('mode','ALL') not in ('ALL','ANY'):
            raise IntegrationError('Rubric ALL/ANY không hợp lệ; cần recruiter review.')
        result.append({'criterion_id':c['id'],'approved_criterion':c,
                       'mode':rule.get('mode','ALL'), 'predicates':rule.get('predicates',[]),
                       'components':[{'id':f'{c["id"]}.R{i+1:03d}','predicate':p,
                                      'scope':c.get('description',c.get('name',''))}
                                     for i,p in enumerate(rule.get('predicates') or [])],
                       'durations':re.findall(r'\d+(?:[.,]\d+)?\s*(?:\+|–|-)?\s*(?:years?|năm)',db.dumps(c),re.I),
                       'meaning_policy':'Verbatim approved criterion; never reinterpret missing predicates.'})
    return result

def combine(states, mode):
    if not states:return 'UNKNOWN'
    if mode=='ANY':
        return 'MET' if 'MET' in states else 'PARTIAL' if 'PARTIAL' in states else 'NOT_MET' if all(s=='NOT_MET' for s in states) else 'UNKNOWN'
    return 'MET' if all(s=='MET' for s in states) else 'NOT_MET' if 'NOT_MET' in states else 'PARTIAL' if any(s in ('MET','PARTIAL') for s in states) else 'UNKNOWN'

def atomic_criteria(criteria):
    """Only approved executable predicates; never synthesize requirements from prose."""
    atoms=[]; layout={}
    for c in criteria:
        rule=c.get('rule') or {}; predicates=rule.get('predicates')
        if not isinstance(predicates,list) or not predicates or rule.get('mode') not in ('ALL','ANY'):
            layout[c['id']]={'mode':'ALL','groups':[],'requires_review':True,
                'reason':'Chưa có rubric thành phần đã duyệt; giữ UNKNOWN, không tự diễn giải.'}
            continue
        groups=[]
        for i,p in enumerate(predicates):
            if not isinstance(p,dict) or not p.get('field') or not p.get('operator'):
                raise IntegrationError('Predicate không hợp lệ; cần review rubric.')
            values=p.get('values') or []
            split=p['operator'] in ('all_of','any_of') and bool(values)
            ids=[]
            for j,value in enumerate(values if split else [None]):
                cid=f'{c["id"]}.R{i:03d}.V{j:03d}'; ids.append(cid)
                predicate={**p,'values':[value]} if split else p
                atoms.append({'id':cid,'name':c.get('name',c['id']),
                    'approved_criterion':c,'component_predicate':predicate,
                    'scope_policy':'Evaluate only this approved predicate. Preserve relevant specialised scope, duration and negation from the approved criterion. Never infer specialised duration from general experience; never sum dates across parts.',
                    'parent_criterion_id':c['id']})
            groups.append({'mode':'ANY' if p['operator']=='any_of' else 'ALL','ids':ids})
        layout[c['id']]={'mode':rule['mode'],'groups':groups}
    return atoms,layout

def manifest(source,criteria):
    chunks=parts(source)
    return {'source_id':source['id'],'revision':source['revision'],
            'content_hash':engine.digest(''.join(p['text'] for p in chunks)),
            'parts':[{k:v for k,v in p.items() if k!='text'} for p in chunks],
            'rubric':rubric(criteria),'planner_version':VERSION}

def validate_manifest(value,source,criteria):
    if value!=manifest(source,criteria):
        raise IntegrationError('Sai hash, revision, offset hoặc rubric của kế hoạch Jev.')

def evidence_plan(source, criteria, pack, policy):
    chunks = parts(source); requests = []; mappings = {}; serial = 0
    for part in chunks:
        questions = {}
        for c in criteria:
            for ref in part['professional_paragraphs']:
                serial += 1; qid = f'Q{serial}'; mappings[qid] = (c['id'],ref)
                questions[qid] = {'type':'choice','instructions':{'policy':policy,'criterion':c,
                    'question':f'Classify only the text interval professional_paragraphs[{ref!r}] in this part. Offsets are Unicode character positions within text. A partial span may support only part of a requirement; never infer missing context.'},
                    'criteria':{'SUPPORT':'Direct sufficient personal evidence in this span.',
                                'PARTIAL':'Direct personal evidence supports part of the criterion.',
                                'CONTRARY':'Explicit personal contradiction.',
                                'NONE':'No admissible personal evidence, company/social context or ambiguous attribution.'}}
        requests.extend(pack(part,questions))
    return requests,mappings,chunks

def evidence_groups(source, selections):
    """No text duplication, including when a paragraph spans multiple requests."""
    selected = set(selections)
    for part in parts(source):
        refs = {r:v for r,v in part['professional_paragraphs'].items() if r in selected}
        if refs: yield {**part,'professional_paragraphs':refs}
