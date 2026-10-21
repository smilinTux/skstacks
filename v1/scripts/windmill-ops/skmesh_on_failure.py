"""Windmill script f/ops/skmesh_on_failure: schedule failure handler for f/ops/skmesh_pending_approvals.

Posts at most one failure notice per hour (last post time in plain variable f/ops/skmesh_failure_last). Carries the
error text trimmed to 300 characters, never job arguments or tokens.
"""
import json
import os
import time
import urllib.request

LAST_VAR = 'f/ops/skmesh_failure_last'


def _wm(method, path, body=None):
    base = os.environ.get('BASE_INTERNAL_URL', 'http://localhost:8000').rstrip('/')
    req = urllib.request.Request(
        '%s/api/w/%s%s' % (base, os.environ['WM_WORKSPACE'], path),
        data=json.dumps(body).encode() if body is not None else None, method=method,
        headers={'Authorization': 'Bearer ' + os.environ['WM_TOKEN'], 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or 'null')


def main(error: dict = {}, schedule_path: str = '', path: str = '', failed_times: int = 1):
    now = time.time()
    try:
        last = float(_wm('GET', '/variables/get_value/' + LAST_VAR) or 0)
    except Exception:
        last = 0.0
    if now - last < 3600:
        return {'posted': False, 'reason': 'rate limited, one failure post per hour'}
    err = error if isinstance(error, dict) else {'message': str(error)}
    detail = str(err.get('message') or err.get('name') or 'unknown error')[:300]
    from f.ops.notify import main as notify
    notify(title='skmesh pending-approval check failing', severity='error', room='NAM Admin',
           summary='%s failed (%s in a row): %s' % (path or 'f/ops/skmesh_pending_approvals', failed_times, detail))
    _wm('POST', '/variables/update/' + LAST_VAR, {'value': str(now)})
    return {'posted': True}
