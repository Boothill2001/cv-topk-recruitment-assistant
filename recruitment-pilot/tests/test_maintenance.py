import pytest
from backend import maintenance


@pytest.mark.parametrize('url', ['https://example.com', 'http://127.0.0.1.evil.test',
                               'http://user:secret@localhost', 'http://localhost/?key=secret'])
def test_check_never_sends_local_status_to_external_or_credential_url(url):
    with pytest.raises(ValueError):
        maintenance.local_url(url)


def test_readiness_redacts_database_and_network_failures(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError('password=DO_NOT_EXPOSE')
    monkeypatch.setattr(maintenance, 'urlopen', fail)
    monkeypatch.setattr(maintenance.db, 'one', fail)
    monkeypatch.setattr(maintenance.db, 'rows', fail)
    result = maintenance.inspect(require_idle=True)
    assert not result['ok']
    assert 'DO_NOT_EXPOSE' not in str(result)
    assert result['llm_calls'] == 0 and result['database_writes'] == 0
