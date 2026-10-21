"""Published, immutable review packets. No provider, queue or CV-pool access."""
import copy
import json
import re
import uuid
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import Field
from typing import Literal
from . import db, pipeline, online, assessment_scoring
from .models import Strict

VERSION = 'pilot-review-1'
SQL = '''CREATE TABLE IF NOT EXISTS pilot_review_sets (
 id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, package_artifact_id TEXT NOT NULL REFERENCES artifacts(id),
 source_set_id TEXT NOT NULL, created TEXT NOT NULL, published_by TEXT NOT NULL, release TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pilot_review_labels (
 id TEXT PRIMARY KEY, set_id TEXT NOT NULL REFERENCES pilot_review_sets(id), username TEXT NOT NULL,
 candidate_id TEXT NOT NULL, version INTEGER NOT NULL, phase TEXT NOT NULL, data JSONB NOT NULL, created TEXT NOT NULL,
 UNIQUE(set_id,username,candidate_id,version));
CREATE TABLE IF NOT EXISTS pilot_review_reveals (
 set_id TEXT NOT NULL REFERENCES pilot_review_sets(id), username TEXT NOT NULL, revealed TEXT NOT NULL,
 PRIMARY KEY(set_id,username));'''
router = APIRouter(prefix='/api/v1/pilot-review-sets', tags=['Published Jev review'])


def person():
    current = online.actor.get()
    if not current or current['username'] not in ('tri', 'dat'):
        raise HTTPException(403, 'Cần phiên đăng nhập pilot để review.')
    return current['username']


def get_set(rid):
    r = db.one('SELECT * FROM pilot_review_sets WHERE id=?', (rid,))
    if not r:
        raise HTTPException(404, 'Không tìm thấy tập review đã xuất bản.')
    return r


def package(r):
    return pipeline.thaw(pipeline.ingest(r['package_artifact_id'], 'pilot-review-package'))


def revealed(rid, username):
    return bool(db.one('SELECT revealed FROM pilot_review_reveals WHERE set_id=? AND username=?', (rid, username)))


def require_reveal(rid):
    username = person()
    get_set(rid)
    if not revealed(rid, username):
        raise HTTPException(409, 'Review đủ hồ sơ trước khi mở kết quả, trace hoặc artifact AI.')
    return username


def label_history(rid, username):
    return [{**r, 'data': pipeline.decode(r['data'])} for r in db.rows(
        'SELECT candidate_id,version,phase,data,created,username FROM pilot_review_labels WHERE set_id=? AND username=? ORDER BY created,version', (rid, username))]


def latest(rid, username):
    return {r['candidate_id']: {**r['data'], 'version': r['version'], 'phase': r['phase'], 'created': r['created']}
            for r in label_history(rid, username)}


def validate_bundle(value):
    """Fail closed on identity, lineage, redaction offsets and server-owned scores."""
    def fail(message):
        raise ValueError('Review bundle: ' + message)
    if value.get('schema_version') != VERSION:
        fail('schema không hỗ trợ')
    if set(value) != {'schema_version', 'origin', 'job', 'engine', 'usage', 'candidates', 'trace', 'notice'}:
        fail('trường không được phép')
    candidates = value['candidates']
    if not candidates or len(candidates) > 20 or len({c['candidate_id'] for c in candidates}) != len(candidates):
        fail('danh tính CV trùng hoặc rỗng')
    if value['engine']['model'] != 'jev-1.13.0' or value['engine']['scorer'] != assessment_scoring.VERSION:
        fail('model/scorer khác phiên bản được kiểm chứng')
    criteria = {c['id'] for c in value['job']['config']['criteria'] if c['enabled']}
    for candidate in candidates:
        text = candidate['review_text']
        if not isinstance(text, str) or not text.strip() or pipeline.digest(text) != candidate['review_content_hash']:
            fail('hash text không khớp')
        if not re.fullmatch('[0-9a-f]{64}', candidate['original_content_hash']):
            fail('thiếu hash nội dung gốc')
        for span in candidate['redactions']:
            a, b = span['start'], span['end']
            if not 0 <= a < b <= len(text) or any(ch not in ('█', '\r', '\n') for ch in text[a:b]):
                fail('vùng che không hợp lệ')
        result = candidate['result']
        if result['candidate_id'] != candidate['candidate_id'] or result['assessment_engine'] != 'JEV':
            fail('kết quả sai ứng viên/engine')
        if {a['criterion_id'] for a in result['assessments']} != criteria or len(result['assessments']) != len(criteria):
            fail('thiếu hoặc trùng kết luận tiêu chí')
        for a in result['assessments']:
            if a['status'] not in ('MET', 'PARTIAL', 'NOT_MET', 'UNKNOWN'):
                fail('trạng thái tiêu chí sai')
            if a['status'] != 'UNKNOWN' and not a['evidence']:
                fail('kết luận không có evidence')
            for e in a['evidence']:
                start, end = e['start'], e['end']
                if e['source_id'] != candidate['source_id'] or e['revision'] != candidate['source_revision']:
                    fail('evidence sai nguồn/revision')
                if not 0 <= start < end <= len(text) or text[start:end] != e['quote']:
                    fail('evidence sai quote/vị trí')
                if e.get('review_content_hash') != candidate['review_content_hash']:
                    fail('evidence sai hash bản review')
        rescored = assessment_scoring.score(value['job']['config'], result, result['be_rank'])
        for key in ('score', 'coverage', 'lane', 'scorer_version'):
            if rescored[key] != result[key]:
                fail('điểm BE không khớp')
    # Trace is metadata only; the local provider payloads are deliberately not published.
    for attempt in value['trace']['attempts']:
        if set(attempt) - {'id', 'node_id', 'attempt_no', 'status', 'versions', 'usage', 'error', 'started', 'finished', 'elapsed_seconds', 'original_artifacts'}:
            fail('trace chứa trường không được phép')
        if attempt['status'] != 'COMPLETED':
            fail('tập bàn giao chưa hoàn tất')
    forbidden = {'authorization', 'headers', 'credentials', 'api_key', 'apikey', 'password', 'access_token', 'refresh_token', 'private_note', 'private_notes'}
    def clean(v):
        if isinstance(v, dict):
            if any(k.lower() in forbidden for k in v):
                fail('metadata chứa credentials/private note')
            for child in v.values(): clean(child)
        elif isinstance(v, list):
            for child in v: clean(child)
    clean(value)
    return value


def import_bundle(value, publisher, release):
    validate_bundle(value)
    fp = pipeline.digest(pipeline.canonical(value))
    rid = str(uuid.uuid5(uuid.NAMESPACE_URL, 'published-review:' + fp))
    with db.LOCK, db.conn() as c:
        if db.database_url(): c.execute('LOCK TABLE pilot_review_sets IN EXCLUSIVE MODE')
        existing = c.execute('SELECT id FROM pilot_review_sets WHERE fingerprint=?', (fp,)).fetchone()
        if existing: return existing['id']
        frozen = copy.deepcopy(value)
        for candidate in frozen['candidates']:
            aid = pipeline._artifact(c, 'source-text', body=candidate['review_text'])
            candidate['review_text'] = {'$text': aid}
            for assessment in candidate['result']['assessments']:
                for evidence in assessment['evidence']:
                    evidence['quote'] = {'$slice': aid, 'start': evidence['start'], 'end': evidence['end']}
        package_id = pipeline._artifact(c, 'pilot-review-package', payload=frozen)
        c.execute('INSERT INTO pilot_review_sets VALUES(?,?,?,?,?,?,?)',
                  (rid, fp, package_id, value['origin']['set_id'], db.now(), publisher, release))
    return rid


@router.get('')
def list_sets():
    username = person()
    return [summary(r, username) for r in db.rows('SELECT * FROM pilot_review_sets ORDER BY created DESC')]


def summary(r, username):
    p = package(r)
    return {'id': r['id'], 'title': p['job']['title'], 'job_id': p['job']['id'], 'created': r['created'],
            'candidate_count': len(p['candidates']), 'scored_count': len(p['candidates']),
            'reviewed_count': len(latest(r['id'], username)), 'revealed': revealed(r['id'], username),
            'model': p['engine']['model'], 'config_version': p['job']['config_version']}


@router.get('/{rid}')
def detail(rid: str):
    username = person(); r = get_set(rid); p = package(r)
    # Explicit DTO: never serialize the package before this user's reveal.
    candidates = sorted(p['candidates'], key=lambda c: pipeline.digest(rid + c['candidate_id']))
    return {**summary(r, username), 'job': p['job'], 'engine': p['engine'], 'origin': p['origin'],
            'usage': p['usage'], 'notice': p['notice'], 'labels': latest(rid, username),
            'candidates': [{k: c[k] for k in ('candidate_id', 'title', 'source_id', 'source_revision', 'original_content_hash', 'review_content_hash', 'redactions')} for c in candidates]}


@router.get('/{rid}/sources/{cid}')
def source(rid: str, cid: str):
    person(); p = package(get_set(rid))
    found = next((c for c in p['candidates'] if c['candidate_id'] == cid), None)
    if not found: raise HTTPException(404, 'CV không thuộc tập review này.')
    return {k: v for k, v in found.items() if k != 'result'}


class Label(Strict):
    candidate_id: str
    version: int = Field(ge=0)
    decision: Literal['CONTACT', 'VERIFY', 'NOT_FIT']
    reason: str = Field(min_length=1, max_length=3000)
    criterion_ids: list[str] = Field(min_length=1, max_length=100)


@router.post('/{rid}/labels')
def save_label(rid: str, body: Label):
    username = person(); p = package(get_set(rid))
    criteria = {c['id'] for c in p['job']['config']['criteria'] if c['enabled']}
    if body.candidate_id not in {c['candidate_id'] for c in p['candidates']} or not set(body.criterion_ids) <= criteria or not body.reason.strip():
        raise HTTPException(422, 'Chọn đúng CV, tiêu chí liên quan và nhập lý do.')
    with db.LOCK, db.conn() as c:
        if db.database_url(): c.execute('SELECT id FROM pilot_review_sets WHERE id=? FOR UPDATE', (rid,))
        current = c.execute('SELECT version FROM pilot_review_labels WHERE set_id=? AND username=? AND candidate_id=? ORDER BY version DESC LIMIT 1', (rid, username, body.candidate_id)).fetchone()
        if (current['version'] if current else 0) != body.version:
            raise HTTPException(409, 'Nhãn đã được lưu ở tab khác. Giữ nháp, tải bản server để đối chiếu.')
        phase = 'AFTER' if c.execute('SELECT revealed FROM pilot_review_reveals WHERE set_id=? AND username=?', (rid, username)).fetchone() else 'BEFORE'
        c.execute('INSERT INTO pilot_review_labels VALUES(?,?,?,?,?,?,?,?)',
                  (str(uuid.uuid4()), rid, username, body.candidate_id, body.version + 1, phase,
                   pipeline.wire(body.model_dump()), db.now()))
    return {'labels': latest(rid, username)}


@router.post('/{rid}/reveal')
def reveal(rid: str):
    username = person(); p = package(get_set(rid))
    with db.LOCK, db.conn() as c:
        if db.database_url(): c.execute('SELECT id FROM pilot_review_sets WHERE id=? FOR UPDATE', (rid,))
        rows = c.execute('SELECT DISTINCT candidate_id FROM pilot_review_labels WHERE set_id=? AND username=?', (rid, username)).fetchall()
        if {r['candidate_id'] for r in rows} != {v['candidate_id'] for v in p['candidates']}:
            raise HTTPException(409, 'Lưu nhận xét và tiêu chí liên quan cho đủ 3 CV trước khi mở AI.')
        c.execute('INSERT INTO pilot_review_reveals VALUES(?,?,?) ON CONFLICT DO NOTHING', (rid, username, db.now()))
    return detail(rid)


@router.get('/{rid}/results')
def results(rid: str):
    require_reveal(rid); p = package(get_set(rid))
    return {'results': assessment_scoring.ordered([v['result'] for v in p['candidates']]),
            'quality_accepted': False, 'note': 'Tập 3 CV có sẵn; không đo chất lượng Top10 hoặc recall toàn kho.'}


@router.get('/{rid}/trace')
def trace(rid: str):
    require_reveal(rid); r = get_set(rid); p = package(r)
    frozen = pipeline.ingest(r['package_artifact_id'], 'pilot-review-package')
    ids = {r['package_artifact_id'], *pipeline.refs(frozen)}
    return {**p['trace'], 'origin': p['origin'], 'engine': p['engine'], 'usage': p['usage'],
            'published_artifacts': [{k: a[k] for k in ('id', 'kind', 'schema_version', 'content_hash', 'validated')}
                                    for aid in sorted(ids) for a in [db.one('SELECT * FROM artifacts WHERE id=?', (aid,))]],
            'notice': 'Trace metadata từ lượt gốc; payload provider gốc không được đưa lên host. Artifact online là bản review đã che, có hash riêng.'}


@router.get('/{rid}/artifacts/{aid}')
def artifact(rid: str, aid: str):
    require_reveal(rid); r = get_set(rid)
    frozen = pipeline.ingest(r['package_artifact_id'], 'pilot-review-package')
    if aid not in {r['package_artifact_id'], *pipeline.refs(frozen)}:
        raise HTTPException(404, 'Artifact không thuộc tập review.')
    value = pipeline.ingest(aid)
    a = db.one('SELECT * FROM artifacts WHERE id=?', (aid,))
    return {'id': aid, 'kind': a['kind'], 'content_hash': a['content_hash'], 'integrity_ok': True,
            'data': pipeline.thaw(value)}


@router.get('/{rid}/feedback-export')
def feedback_export(rid: str):
    username = person(); get_set(rid)
    return {'set_id': rid, 'username': username, 'label_history': label_history(rid, username),
            'revealed': revealed(rid, username), 'quality_accepted': False}


@router.get('/{rid}/reviews')
def all_reviews(rid: str):
    username = person(); get_set(rid)
    if username != 'tri': raise HTTPException(403, 'Chỉ Tri được xem tổng hợp nhận xét.')
    # Before Tri's independent review, do not expose labels influenced by AI.
    if not revealed(rid, username): raise HTTPException(409, 'Hoàn tất review riêng trước khi xem tổng hợp.')
    return {name: label_history(rid, name) for name in ('tri', 'dat')}


@router.get('/{rid}/export')
def export(rid: str):
    username = require_reveal(rid); r = get_set(rid)
    data = {'set': detail(rid), 'packet': package(r), 'feedback': feedback_export(rid),
            'contains_cv_data': True, 'exported_by': username, 'quality_accepted': False}
    return Response(db.dumps(data), media_type='application/json',
                    headers={'Content-Disposition': f'attachment; filename="jev-review-{rid}.json"'})
