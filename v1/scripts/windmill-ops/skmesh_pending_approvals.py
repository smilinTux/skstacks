"""Windmill script f/ops/skmesh_pending_approvals (workspace fwe).

Lists NetBird users through the skmesh REST API and posts to Nextcloud Talk (via f/ops/notify) once per newly pending
user, with one reminder after 24 hours. State is a JSON Windmill variable, cleared per user once they are approved,
blocked or deleted. Variables under f/ops/: skmesh_api_token (secret PAT of service user svc-pending-approvals, role
auditor), skmesh_pending_state (plain JSON). Nothing here prints or returns the token.

Args: dry_run=true formats and returns without posting or saving state. test=true posts one fake-user test message
and touches no state.
"""
import json
import os
import time
import urllib.request

BASE_VAR = 'f/ops/skmesh_base_url'  # plain variable, e.g. https://skmesh.example.org (no trailing slash needed)
TOKEN_VAR = 'f/ops/skmesh_api_token'
STATE_VAR = 'f/ops/skmesh_pending_state'
REMIND_AFTER = 24 * 3600


def _wm(method, path, body=None):
    base = os.environ.get('BASE_INTERNAL_URL', 'http://localhost:8000').rstrip('/')
    req = urllib.request.Request(
        '%s/api/w/%s%s' % (base, os.environ['WM_WORKSPACE'], path),
        data=json.dumps(body).encode() if body is not None else None, method=method,
        headers={'Authorization': 'Bearer ' + os.environ['WM_TOKEN'], 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or 'null')


def _base():
    return str(_wm('GET', '/variables/get_value/' + BASE_VAR)).strip().strip('"').rstrip('/')


def _list_users(token):
    req = urllib.request.Request(_base() + '/api/users', headers={'Authorization': 'Token ' + token, 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def pending_users(users):
    return [u for u in users if u.get('pending_approval') and not u.get('is_service_user') and not u.get('is_blocked')]


def message(name, email, approve_url, reminder=False):
    text = 'skmesh: %s (%s) is waiting for approval. Approve at %s' % (name or 'unknown', email or 'no email', approve_url)
    return ('Reminder, still pending after 24h. ' + text) if reminder else text


def _post(title, text):
    from f.ops.notify import main as notify
    return notify(title=title, summary=text, severity='warn')


def plan(pending, state, now):
    """Return (messages, new_state). messages is a list of (user, reminder)."""
    new, out = {}, []
    for u in pending:
        rec = state.get(u['id'])
        if rec is None:
            out.append((u, False))
            rec = {'first': now, 'reminded': False}
        elif not rec.get('reminded') and now - rec['first'] >= REMIND_AFTER:
            out.append((u, True))
            rec = dict(rec, reminded=True)
        new[u['id']] = rec
    return out, new


def main(dry_run: bool = False, test: bool = False):
    if test:
        text = message('Test User', 'test@example.invalid', _base() + '/team/users')
        if dry_run:
            return {'dry_run': True, 'would_post': text}
        _post('skmesh approval alert test, ignore', text)
        return {'test_posted': True, 'text': text}

    users = _list_users(_wm('GET', '/variables/get_value/' + TOKEN_VAR))
    pending = pending_users(users)
    raw = _wm('GET', '/variables/get_value/' + STATE_VAR)
    try:
        state = json.loads(raw) if raw else {}
    except ValueError:
        state = {}
    msgs, new_state = plan(pending, state, time.time())
    approve_url = _base() + '/team/users'
    texts = [message(u.get('name'), u.get('email'), approve_url, rem) for u, rem in msgs]
    if dry_run:
        return {'dry_run': True, 'users': len(users), 'pending': len(pending), 'would_post': texts}
    posted = 0
    for (u, rem), text in zip(msgs, texts):
        _post('skmesh user waiting for approval', text)
        posted += 1
    if new_state != state:
        _wm('POST', '/variables/update/' + STATE_VAR, {'value': json.dumps(new_state)})
    return {'users': len(users), 'pending': len(pending), 'posted': posted}
