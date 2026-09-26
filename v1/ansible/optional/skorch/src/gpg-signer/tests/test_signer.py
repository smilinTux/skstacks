"""Unit tests for the gpg-signer sidecar's REST API.

Covers the security review findings fixed in this change: fail-closed auth
when no secret is configured, constant-time secret comparison, the
passphrase never appearing as a subprocess argv, upload size limits and the
import-key kill switch. GPG itself is mocked throughout: these tests check
the HTTP/auth/process-invocation contract, not GPG's own behavior.
"""
import importlib
import io
import sys
import pathlib
from unittest import mock

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


def load_signer(monkeypatch, **env):
    """Import signer.py fresh with the given environment, so module-level
    config (API_SECRET, ALLOW_KEY_IMPORT, ...) picks up each test's env."""
    monkeypatch.setenv('GNUPGHOME', '/tmp')
    for key in ('GPG_SIGNER_API_SECRET', 'GPG_SIGNER_ALLOW_KEY_IMPORT',
                'TRUSTEE_A_KEY_ID', 'TRUSTEE_B_KEY_ID', 'TRUSTEE_C_KEY_ID',
                'GPG_SIGNER_MAX_UPLOAD_BYTES'):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    sys.modules.pop('signer', None)
    return importlib.import_module('signer')


@pytest.fixture
def client_no_secret(monkeypatch):
    signer = load_signer(monkeypatch)
    signer.app.config['TESTING'] = True
    return signer, signer.app.test_client()


@pytest.fixture
def client(monkeypatch):
    signer = load_signer(monkeypatch, GPG_SIGNER_API_SECRET='s3cr3t-token',
                          TRUSTEE_A_KEY_ID='ABCDEF0123456789')
    signer.app.config['TESTING'] = True
    return signer, signer.app.test_client()


def test_fails_closed_when_secret_not_configured(client_no_secret):
    _signer, c = client_no_secret
    resp = c.get('/keys', headers={'Authorization': 'Bearer anything'})
    assert resp.status_code == 503


def test_missing_authorization_header_rejected(client):
    _signer, c = client
    resp = c.get('/keys')
    assert resp.status_code == 401


def test_wrong_secret_rejected(client):
    _signer, c = client
    resp = c.get('/keys', headers={'Authorization': 'Bearer wrong'})
    assert resp.status_code == 403


def test_correct_secret_accepted(client):
    signer, c = client
    with mock.patch.object(signer, 'run_gpg_command', return_value=(b'sec ...', b'', 0)):
        resp = c.get('/keys', headers={'Authorization': 'Bearer s3cr3t-token'})
    assert resp.status_code == 200


def test_secret_comparison_is_constant_time(client, monkeypatch):
    """require_auth must compare via hmac.compare_digest, not , so a
    timing side channel cannot be used to guess the secret one byte at a
    time."""
    signer, _c = client
    calls = []
    real_compare = signer.hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real_compare(a, b)

    monkeypatch.setattr(signer.hmac, 'compare_digest', spy)
    signer.app.test_client().get('/keys', headers={'Authorization': 'Bearer wrong'})
    assert calls, 'require_auth must call hmac.compare_digest for the secret check'


def test_import_key_disabled_by_default(client):
    _signer, c = client
    resp = c.post('/import-key', data={'trustee': 'trustee-a',
                                        'key': (io.BytesIO(b'fake-key'), 'k.asc')},
                   headers={'Authorization': 'Bearer s3cr3t-token'},
                   content_type='multipart/form-data')
    assert resp.status_code == 403


def test_import_key_allowed_when_opted_in(monkeypatch):
    signer = load_signer(monkeypatch, GPG_SIGNER_API_SECRET='s3cr3t-token',
                          GPG_SIGNER_ALLOW_KEY_IMPORT='true')
    signer.app.config['TESTING'] = True
    c = signer.app.test_client()
    with mock.patch.object(signer, 'run_gpg_command', return_value=(b'', b'imported', 0)):
        resp = c.post('/import-key', data={'trustee': 'trustee-a',
                                            'key': (io.BytesIO(b'fake-key'), 'k.asc')},
                       headers={'Authorization': 'Bearer s3cr3t-token'},
                       content_type='multipart/form-data')
    assert resp.status_code == 200


def test_sign_rejects_unknown_trustee(client):
    _signer, c = client
    resp = c.post('/sign', data={'trustee': 'trustee-z',
                                  'file': (io.BytesIO(b'doc'), 'd.txt')},
                   headers={'Authorization': 'Bearer s3cr3t-token'},
                   content_type='multipart/form-data')
    assert resp.status_code == 400


def test_passphrase_is_never_passed_as_a_cli_argument(client):
    """The passphrase must reach gpg via --passphrase-fd (a pipe), never as
    a literal argv element, because argv is visible to any other process on
    the host via /proc/<pid>/cmdline."""
    signer, c = client
    seen_argv = []

    def fake_run(*args, **kwargs):
        seen_argv.append(list(args[0]))
        result = mock.Mock()
        result.stdout = b'-----BEGIN PGP SIGNATURE-----\n'
        result.stderr = b''
        result.returncode = 0
        return result

    with mock.patch.object(signer.subprocess, 'run', side_effect=fake_run):
        resp = c.post('/sign', data={'trustee': 'trustee-a',
                                      'passphrase': 'super-secret-passphrase',
                                      'file': (io.BytesIO(b'doc'), 'd.txt')},
                       headers={'Authorization': 'Bearer s3cr3t-token'},
                       content_type='multipart/form-data')

    assert resp.status_code == 200
    assert seen_argv, 'gpg was never invoked'
    for argv in seen_argv:
        assert 'super-secret-passphrase' not in argv
        joined = ' '.join(argv)
        assert 'super-secret-passphrase' not in joined
    assert any('--passphrase-fd' in argv for argv in seen_argv)


def test_max_content_length_is_bounded(client):
    signer, _c = client
    assert signer.app.config['MAX_CONTENT_LENGTH']
    assert signer.app.config['MAX_CONTENT_LENGTH'] <= 100 * 1024 * 1024


def test_health_does_not_require_auth(client):
    signer, c = client
    with mock.patch.object(signer, 'run_gpg_command', return_value=(b'gpg (GnuPG) 2.4.0', b'', 0)):
        resp = c.get('/health')
    assert resp.status_code == 200
