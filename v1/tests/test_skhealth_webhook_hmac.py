"""skhealth webhook signing (HMAC-SHA256) and an idempotency X-Request-ID.

Hermes's webhook platform rejects unsigned posts. With the optional
`alerts.webhook_hmac_secret` (rendered into the root-only secrets.env as
SKHEALTH_WEBHOOK_HMAC_SECRET) every webhook POST carries
`X-Webhook-Signature: <hex HMAC-SHA256 of the raw body>` and an
`X-Request-ID` derived from cluster, host, the saved state's last run and a
hash of the (kind, key) pairs: a retry of an undelivered change set repeats
the same id, any later change set gets a new one. Unset: neither header, the
request is exactly as before. The secret is never printed.

End to end against the real engine and a local fake webhook that records
request headers (skhealth_support.Hook).
"""
import hashlib
import hmac
import json
import subprocess
import sys

import pytest

from skhealth_support import ENGINE, FakeHost, Hook, env_without_path, load_engine

eng = load_engine()
SECRET = "example-hmac-secret-not-a-real-one"


@pytest.fixture
def hook():
    h = Hook()
    yield h
    h.close()


def make_host(tmp_path, hook, secret=SECRET):
    h = FakeHost(tmp_path)
    lines = f"SKHEALTH_WEBHOOK_URL='{hook.url}/hook/path-token'\n"
    if secret is not None:
        lines += f"SKHEALTH_WEBHOOK_HMAC_SECRET='{secret}'\n"
    h.secrets.write_text(lines)
    return h


def skhealth(host, *args, now=None):
    if now is not None:
        host.cfg["now"] = now
        host.save()
    proc = subprocess.run([sys.executable, str(ENGINE), "--config", str(host.config_path), *args],
                          capture_output=True, text=True, env=env_without_path(), timeout=60)
    assert SECRET not in proc.stdout + proc.stderr, "the HMAC secret was printed"
    return proc


def hook_posts(hook):
    return [p for p in hook.posts if p["path"].startswith("/hook/")]


def expected_sig(body: str, secret=SECRET) -> str:
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def test_signed_post_carries_a_valid_signature_over_the_raw_body(tmp_path, hook):
    host = make_host(tmp_path, hook)
    r = skhealth(host, "--test")
    assert r.returncode == 0, r.stderr
    (post,) = hook_posts(hook)
    assert post["headers"]["x-webhook-signature"] == expected_sig(post["body"])
    assert post["headers"]["content-type"] == "application/json"
    assert json.loads(post["body"])["events"][0]["kind"] == "test"


def test_signature_does_not_verify_with_another_secret(tmp_path, hook):
    host = make_host(tmp_path, hook)
    skhealth(host, "--test")
    (post,) = hook_posts(hook)
    assert post["headers"]["x-webhook-signature"] != expected_sig(post["body"], "another-secret")


def test_unset_secret_sends_neither_header(tmp_path, hook):
    host = make_host(tmp_path, hook, secret=None)
    skhealth(host, "--test")
    (post,) = hook_posts(hook)
    names = set(post["headers"])
    assert "x-webhook-signature" not in names and "x-request-id" not in names
    assert "x-hub-signature-256" not in names


def test_empty_secret_is_unset(tmp_path, hook):
    host = make_host(tmp_path, hook, secret="")
    skhealth(host, "--test")
    (post,) = hook_posts(hook)
    assert "x-webhook-signature" not in post["headers"]


def test_unsigned_body_is_byte_identical_to_the_signed_one(tmp_path, hook):
    """Signing adds headers only; the payload is the same bytes either way."""
    host_events = [{"kind": "problem", "key": "k", "message": "m"}]
    eng_secrets = {"SKHEALTH_WEBHOOK_URL": f"{hook.url}/hook/x"}
    eng.send({}, eng_secrets, "lab", "node-a", host_events)
    eng.send({}, eng_secrets | {"SKHEALTH_WEBHOOK_HMAC_SECRET": SECRET}, "lab", "node-a", host_events, basis=1.0)
    p1, p2 = hook_posts(hook)
    assert p1["body"] == p2["body"]
    assert "x-webhook-signature" not in p1["headers"] and p2["headers"]["x-webhook-signature"] == expected_sig(p2["body"])


def test_telegram_never_gets_the_signature(tmp_path, hook):
    host = make_host(tmp_path, hook)
    host.cfg["telegram_api_base"] = hook.url
    host.save()
    host.secrets.write_text(host.secrets.read_text() + "SKHEALTH_TELEGRAM_TOKEN='1:x'\nSKHEALTH_TELEGRAM_CHAT_ID='-1'\n")
    skhealth(host, "--test")
    tg = [p for p in hook.posts if p["path"].startswith("/bot")]
    assert len(tg) == 1 and "x-webhook-signature" not in tg[0]["headers"] and "x-request-id" not in tg[0]["headers"]


def test_retry_of_the_same_change_set_repeats_the_request_id(tmp_path, hook):
    host = make_host(tmp_path, hook)
    host.on("systemctl", ["--failed"], "x.service loaded failed failed X\n")
    skhealth(host, now=1000)
    hook.set_status(500)
    r = skhealth(host, now=1300)                          # alert, not delivered: state kept
    assert "alert NOT delivered" in r.stderr
    hook.set_status(200)
    skhealth(host, now=1600)                              # the retry
    first, retry = hook_posts(hook)
    assert json.loads(first["body"])["events"] == json.loads(retry["body"])["events"]
    assert first["headers"]["x-request-id"] == retry["headers"]["x-request-id"]
    assert retry["headers"]["x-webhook-signature"] == expected_sig(retry["body"])
    assert retry["headers"]["x-request-id"].startswith("skhealth-lab-node-a-")


def test_a_later_change_set_gets_a_new_request_id(tmp_path, hook):
    host = make_host(tmp_path, hook)
    host.on("systemctl", ["--failed"], "x.service loaded failed failed X\n")
    skhealth(host, now=1000)
    skhealth(host, now=1300)                              # PROBLEM
    host.clear("systemctl")
    skhealth(host, now=1600)                              # RECOVERED
    host.on("systemctl", ["--failed"], "x.service loaded failed failed X\n")
    skhealth(host, now=1900)
    skhealth(host, now=2200)                              # PROBLEM again, same key
    ids = [p["headers"]["x-request-id"] for p in hook_posts(hook)]
    kinds = [json.loads(p["body"])["events"][0]["kind"] for p in hook_posts(hook)]
    assert kinds == ["problem", "recovered", "problem"]
    assert len(set(ids)) == 3


def test_request_id_is_header_safe(tmp_path, hook):
    rid = eng.request_id("lab cluster/€", "node a", [{"kind": "problem", "key": "k\n"}], 12.5)
    assert rid.isascii() and "\n" not in rid and " " not in rid and len(rid) <= 128
    assert rid == eng.request_id("lab cluster/€", "node a", [{"kind": "problem", "key": "k\n"}], 12.5)
    assert rid != eng.request_id("lab cluster/€", "node a", [{"kind": "recovered", "key": "k\n"}], 12.5)


def test_status_shows_signing_as_a_boolean_only(tmp_path, hook):
    host = make_host(tmp_path, hook)
    st = json.loads(skhealth(host, "status").stdout)
    assert st["webhook_signed"] is True
    host2 = make_host(tmp_path / "u", hook, secret=None)
    assert json.loads(skhealth(host2, "status").stdout)["webhook_signed"] is False
