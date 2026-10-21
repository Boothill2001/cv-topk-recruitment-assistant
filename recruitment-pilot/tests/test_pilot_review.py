import copy
import json
import pytest
from backend import db, pipeline, pilot_review, online, assessment_scoring
from backend.pilot_review_transfer import redact
from test_engine import database, CONFIG
from test_online import web, sign_in


def fixture_bundle():
    candidates = []
    for rank in range(1, 4):
        cid = 'CV' + str(rank)
        original = 'Date of Birth: 1991\nPython developer with SQL.\nEmail: qa@example.com'
        text, redactions = redact(original)
        start = text.index('Python'); end = text.index('\n', start)
        h = pipeline.digest(text)
        item = {'candidate_id': cid, 'title': cid, 'assessment_engine': 'JEV', 'adapter_version': 'jev-evidence-3',
                'assessments': [{'criterion_id': c['id'], 'status': 'MET', 'explanation': 'Fixture only',
                                  'evidence': [{'source_id': 'source-' + cid, 'revision': 1,
                                                'paragraph_id': 'PUB001.F001.P00000', 'start': start, 'end': end,
                                                'quote': text[start:end], 'review_content_hash': h}]} for c in CONFIG['criteria']]}
        item = assessment_scoring.score(CONFIG, item, rank)
        candidates.append({'candidate_id': cid, 'title': cid + ' · fixture', 'source_id': 'source-' + cid,
                           'source_revision': 1, 'original_content_hash': pipeline.digest(original),
                           'review_text': text, 'review_content_hash': h, 'redactions': redactions, 'result': item})
    return {'schema_version': pilot_review.VERSION,
            'origin': {'set_id': 'fixture-local', 'search_id': 'fixture-search', 'pipeline_run_id': 'fixture-pipeline', 'created': db.now()},
            'job': {'id': 'JD_TEST', 'title': 'Jev fixture — không phải kết quả thật', 'revision': 1, 'config_version': 2, 'config': copy.deepcopy(CONFIG)},
            'engine': {'model': 'jev-1.13.0', 'adapter': 'jev-evidence-3', 'scorer': assessment_scoring.VERSION},
            'usage': {'estimated_cost_dollars': 0, 'input_tokens': 0, 'total_tokens': 0, 'provider_confirmed_cost_dollars': None},
            'candidates': candidates,
            'trace': {'id': 'fixture-pipeline', 'status': 'COMPLETED', 'attempts': [
                {'id': 'fixture-attempt', 'node_id': 'fixture-selection', 'attempt_no': 1, 'status': 'COMPLETED',
                 'versions': {'adapter': 'fixture'}, 'usage': {}, 'error': None, 'started': db.now(), 'finished': db.now(),
                 'elapsed_seconds': 0, 'original_artifacts': []}]}, 'notice': 'Synthetic fixture; no provider call.'}


@pytest.fixture
def published():
    return pilot_review.import_bundle(fixture_bundle(), 'tri', 'fixture-release')


def post_label(client, headers, rid, cid, version=0, reason='QA fixture, not recruiter judgement'):
    return client.post(f'/api/v1/pilot-review-sets/{rid}/labels', headers=headers,
                       json={'candidate_id': cid, 'version': version, 'decision': 'VERIFY',
                             'reason': reason, 'criterion_ids': ['C1']})


def test_redaction_preserves_unicode_offsets_and_professional_dates():
    text = 'Ngày sinh: 1991\r\nEducation 2009-2013\nInformatics cohort 201202108003\nMobile: +84 901 234 567\nEmail: demo@example.com\nPython 2018–2024'
    masked, spans = redact(text)
    assert len(masked) == len(text) and masked.count('\n') == text.count('\n')
    assert '1991' not in masked and 'example.com' not in masked and '901' not in masked
    assert 'Education 2009-2013' in masked and 'Python 2018–2024' in masked
    assert 'Informatics cohort 201202108003' in masked
    for span in spans: assert set(masked[span['start']:span['end']]) <= set('█\r\n')


def test_import_idempotent_transaction_and_no_pool_rows(published, monkeypatch):
    count = db.one('SELECT COUNT(*) AS n FROM artifacts')['n']
    assert pilot_review.import_bundle(fixture_bundle_stable(published), 'tri', 'same') == published
    assert db.one('SELECT COUNT(*) AS n FROM artifacts')['n'] == count
    assert not db.rows("SELECT * FROM profiles WHERE kind='candidate'")
    assert not db.rows('SELECT * FROM files')
    value = fixture_bundle(); value['origin']['set_id'] = 'fail-after-artifact'
    original = pipeline._artifact
    calls = []
    def fail(c, *a, **kw):
        calls.append(1)
        if len(calls) == 2: raise RuntimeError('crash inside transaction')
        return original(c, *a, **kw)
    monkeypatch.setattr(pipeline, '_artifact', fail)
    with pytest.raises(RuntimeError): pilot_review.import_bundle(value, 'tri', 'fixture')
    assert db.one('SELECT COUNT(*) AS n FROM artifacts')['n'] == count
    assert db.one('SELECT COUNT(*) AS n FROM pilot_review_sets')['n'] == 1


def fixture_bundle_stable(rid):
    return pilot_review.package(pilot_review.get_set(rid))


@pytest.mark.parametrize('case', ['hash', 'source', 'revision', 'offset', 'quote', 'score', 'schema', 'secret', 'missing', 'duplicate'])
def test_reject_invalid_bundle_without_ingestion(case):
    value = fixture_bundle(); candidate = value['candidates'][0]; evidence = candidate['result']['assessments'][0]['evidence'][0]
    if case == 'hash': candidate['review_content_hash'] = '0' * 64
    elif case == 'source': evidence['source_id'] = 'other'
    elif case == 'revision': evidence['revision'] = 2
    elif case == 'offset': evidence['start'] += 1
    elif case == 'quote': evidence['quote'] = 'not in CV'
    elif case == 'score': candidate['result']['score'] += 1
    elif case == 'schema': value['schema_version'] = 'unknown'
    elif case == 'secret': value['origin']['headers'] = {'Authorization': 'secret'}
    elif case == 'missing': candidate['result']['assessments'] = []
    else: value['candidates'].append(copy.deepcopy(candidate))
    with pytest.raises(ValueError): pilot_review.import_bundle(value, 'tri', 'fixture')
    assert db.one('SELECT COUNT(*) AS n FROM pilot_review_sets')['n'] == 0


def test_auth_csrf_and_blind_gates(web, published):
    base = '/api/v1/pilot-review-sets/' + published
    for url in [base, base + '/sources/CV1', base + '/results', base + '/trace', base + '/export', base + '/feedback-export']:
        assert web.get(url).status_code == 401
    h = sign_in(web)
    d = web.get(base).json()
    assert d['reviewed_count'] == 0 and not d['revealed']
    encoded = json.dumps(d)
    assert 'assessments' not in encoded and 'be_rank' not in encoded and 'result' not in encoded
    assert web.get(base + '/sources/CV1').status_code == 200
    for route in ('results', 'trace', 'artifacts/missing', 'export'):
        assert web.get(base + '/' + route).status_code == 409
    assert web.post(base + '/reveal', headers=h).status_code == 409
    assert web.post(base + '/labels', json={}).status_code == 403
    for cid in ('CV1','CV2','CV3'): assert post_label(web, h, published, cid).status_code == 200
    assert web.post(base + '/reveal', headers=h).status_code == 200
    assert web.get(base + '/results').status_code == 200
    t = web.get(base + '/trace').json()
    assert web.get(base + '/artifacts/' + t['published_artifacts'][0]['id']).status_code == 200
    assert web.get(base + '/artifacts/not-in-set').status_code == 404
    assert web.get(base + '/reviews').status_code == 403


def test_per_account_reveal_history_conflicts_restart_and_no_provider(web, published, monkeypatch):
    def forbidden(*a, **kw): pytest.fail('review must not call provider or queue')
    monkeypatch.setattr('backend.engine.enqueue', forbidden)
    monkeypatch.setattr('backend.jev.key', forbidden)
    before = {t: db.one('SELECT COUNT(*) AS n FROM ' + t)['n'] for t in ('tasks','calls','jev_ledger')}
    base = '/api/v1/pilot-review-sets/' + published
    tri = sign_in(web, 'tri')
    for cid in ('CV1','CV2','CV3'): assert post_label(web, tri, published, cid).status_code == 200
    assert web.post(base + '/reveal', headers=tri).status_code == 200
    dat = sign_in(web, 'dat')
    assert not web.get(base).json()['revealed']
    assert web.get(base + '/results').status_code == 409
    assert post_label(web, dat, published, 'CV1').status_code == 200
    assert post_label(web, dat, published, 'CV1').status_code == 409
    assert post_label(web, dat, published, 'CV1', 1, 'Updated independent judgement').status_code == 200
    for cid in ('CV2','CV3'): assert post_label(web, dat, published, cid).status_code == 200
    assert web.post(base + '/reveal', headers=dat).status_code == 200
    assert post_label(web, dat, published, 'CV1', 2, 'After comparison').status_code == 200
    exported = web.get(base + '/export').json()
    history = exported['feedback']['label_history']
    assert [r['phase'] for r in history if r['candidate_id']=='CV1'] == ['BEFORE','BEFORE','AFTER']
    assert all(r['username']=='dat' for r in history)
    db.init()  # fixture DB restart: does not replay any assessment
    assert web.get(base).json()['revealed']
    assert len(web.get(base + '/results').json()['results']) == 3
    assert web.get(base + '/feedback-export').status_code == 200
    tri = sign_in(web, 'tri')
    assert set(web.get(base + '/reviews').json()) == {'tri','dat'}
    assert before == {t: db.one('SELECT COUNT(*) AS n FROM ' + t)['n'] for t in before}
    for url in ('/api/v1/inbound-candidates', '/api/v1/inbound-review-sets/local'):
        assert web.get(url).status_code == 403
    assert web.post('/api/v1/inbound-review-sets/local/assess', headers=tri, json={'confirmed': True}).status_code == 403


def test_other_set_artifact_denied(web, published):
    other = fixture_bundle(); other['origin']['set_id'] = 'other-set'
    other['candidates'][0]['review_text'] += ' extra'
    other['candidates'][0]['review_content_hash'] = pipeline.digest(other['candidates'][0]['review_text'])
    for a in other['candidates'][0]['result']['assessments']:
        for e in a['evidence']: e['review_content_hash'] = other['candidates'][0]['review_content_hash']
    other_id = pilot_review.import_bundle(other, 'tri', 'fixture')
    h = sign_in(web)
    for cid in ('CV1','CV2','CV3'): post_label(web, h, published, cid)
    web.post('/api/v1/pilot-review-sets/' + published + '/reveal', headers=h)
    aid = pilot_review.get_set(other_id)['package_artifact_id']
    assert web.get(f'/api/v1/pilot-review-sets/{published}/artifacts/{aid}').status_code == 404
