"""Operator-only transfer of a selected, already-assessed review set."""
import argparse
import copy
import json
import os
import re
from pathlib import Path
from . import db, pipeline, inbound_review, pilot_review


def redact(text):
    spans = []
    patterns = [
        ('birth', r'(?im)^[^\S\r\n]*(?:date of birth|d\.?o\.?b\.?|birthday|birth date|ngày sinh|năm sinh)\s*[:：]?[^\r\n]*'),
        ('contact', r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}'),
        ('contact', r'(?im)^[^\S\r\n]*(?:phone|telephone|mobile|điện thoại|số điện thoại|địa chỉ liên hệ|home address|personal address)\s*[:：][^\r\n]*'),
    ]
    for kind, pattern in patterns:
        spans += [{'start': m.start(), 'end': m.end(), 'kind': kind} for m in re.finditer(pattern, text)]
    for m in re.finditer(r'(?<!\w)(?:\+\d|0[35789])[\d .()\-]{7,}\d(?!\w)', text):
        if len(re.sub(r'\D', '', m[0])) >= 10:
            spans.append({'start': m.start(), 'end': m.end(), 'kind': 'contact'})
    # Merge overlapping redactions without ever removing a Unicode character.
    merged = []
    for span in sorted(spans, key=lambda x: (x['start'], x['end'])):
        if merged and span['start'] <= merged[-1]['end']:
            merged[-1]['end'] = max(merged[-1]['end'], span['end'])
        else: merged.append(dict(span))
    chars = list(text)
    for span in merged:
        for index in range(span['start'], span['end']):
            if chars[index] not in '\r\n': chars[index] = '█'
    return ''.join(chars), merged


def build_bundle(rid):
    r = inbound_review.row(rid); snap = inbound_review.stored_snapshot(r)
    results = {v['candidate_id']: v for v in inbound_review.results_for(r)}
    if set(results) != set(snap['group']): raise ValueError('Tập chưa chấm đủ; không xuất bản.')
    if inbound_review.labels(rid): raise ValueError('Chỉ chuyển tập chưa có nhãn recruiter; không đưa lịch sử cá nhân lên host.')
    candidates = []
    for source in snap['jev']['sources']:
        cid = source['url']; refs = snap['group'][cid]['sources']
        if len(refs) != 1: raise ValueError('Bản chuyển hiện tại yêu cầu một file đã đóng băng cho mỗi CV.')
        meta = refs[0]
        record = json.loads(db.one('SELECT data FROM source_revisions WHERE file_id=? AND revision=?', (meta['source_id'], meta['revision']))['data'])
        raw = record['text']
        if raw != source['raw_text'] or pipeline.digest(raw) != meta['hash']:
            raise ValueError('Nguồn gốc khác text/hash trong lượt đã chấm.')
        text, spans = redact(raw); h = pipeline.digest(text)
        result = copy.deepcopy(results[cid])
        # No live links, contact data or incidental structured profile facts.
        result.pop('source_urls', None)
        for a in result['assessments']:
            for evidence in a['evidence']:
                start, end = evidence['start'], evidence['end']
                if evidence['source_id'] != meta['source_id'] or evidence['revision'] != meta['revision'] or raw[start:end] != evidence['quote']:
                    raise ValueError('Evidence gốc sai nguồn/revision/offset.')
                evidence['quote'] = text[start:end]
                evidence['review_content_hash'] = h
        candidates.append({'candidate_id': cid, 'title': cid + ' · CV review', 'source_id': meta['source_id'],
                           'source_revision': meta['revision'], 'original_content_hash': pipeline.digest(raw),
                           'review_text': text, 'review_content_hash': h, 'redactions': spans, 'result': result})
    run = db.one("SELECT id FROM pipeline_runs WHERE origin_kind='assessment' AND origin_id=?", (r['search_id'],))
    trace = pipeline.manifest(run['id'])
    attempts = []
    for attempt in trace['attempts']:
        item = {k: attempt[k] for k in ('id', 'node_id', 'attempt_no', 'status', 'versions', 'usage', 'error', 'started', 'finished', 'elapsed_seconds')}
        item['original_artifacts'] = [{**link, 'payload_copied': False} for link in attempt['links']]
        attempts.append(item)
    profile = db.one("SELECT data FROM profile_revisions WHERE id=? AND kind='job' AND revision=?", (r['job_id'], snap['job']['revision']))
    title = json.loads(profile['data'])['title'] if profile else r['job_id']
    report = db.setting('internal_jev_report:' + r['search_id'], {})
    value = {'schema_version': pilot_review.VERSION,
             'origin': {'set_id': rid, 'search_id': r['search_id'], 'pipeline_run_id': run['id'], 'created': r['created'],
                        'fingerprint': r['fingerprint'], 'assessment_location': 'LOCAL', 'original_text_sent_to_jev': True},
             'job': {'id': r['job_id'], 'title': title, 'revision': snap['job']['revision'],
                     'config_version': snap['config_version'], 'config': snap['config']},
             'engine': {'model': snap['jev']['model'], 'adapter': snap['jev']['adapter_version'], 'scorer': snap['scorer_version']},
             'usage': report['usage'], 'candidates': candidates,
             'trace': {'id': run['id'], 'status': trace['status'], 'attempts': attempts},
             'notice': 'Jev đã chạy trên text gốc tại localhost. Bản review che thông tin cá nhân bằng ký tự cùng độ dài; hash bản review khác hash gốc. Đây là 3 CV được chọn, chưa kiểm chứng chất lượng TopK toàn kho.'}
    pilot_review.validate_bundle(value)
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    export = commands.add_parser('export-local'); export.add_argument('--set', required=True); export.add_argument('--output', required=True)
    load = commands.add_parser('import-online'); load.add_argument('--bundle', required=True); load.add_argument('--sha256', required=True)
    args = parser.parse_args()
    if args.command == 'export-local':
        value = build_bundle(args.set); text = pipeline.canonical(value)
        target = Path(args.output); target.parent.mkdir(parents=True, exist_ok=True); target.write_text(text, encoding='utf-8')
        print(json.dumps({'sha256': pipeline.digest(text), 'candidates': len(value['candidates']), 'redactions': sum(len(c['redactions']) for c in value['candidates'])}))
    else:
        if not os.environ.get('PILOT_RELEASE'): raise ValueError('PILOT_RELEASE is required')
        target = Path(args.bundle)
        if target.stat().st_size > 8 * 1024 * 1024: raise ValueError('Bundle vượt giới hạn chuyển nội bộ.')
        text = target.read_text(encoding='utf-8')
        if pipeline.digest(text) != args.sha256: raise ValueError('Checksum gói chuyển không khớp.')
        rid = pilot_review.import_bundle(json.loads(text), 'tri', os.environ['PILOT_RELEASE'])
        print(json.dumps({'id': rid, 'path': '/?review_set=' + rid, 'provider_calls': 0}))


if __name__ == '__main__': main()
