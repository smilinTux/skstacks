"""Talk notification and GTD capture for the fleet image monitor.

Talk: reuses the Windmill script f/ops/notify and the NAM Admin / NAM Urgent
rooms wired up in skstacks PR #215 (v1/scripts/windmill-ops/notify.py,
ALERT-POLICY.md). No new room, bot or alert path is created here - this
calls the one script externally over Windmill's own job-run API, the same
way the Talk bot's own webhook calls back into Windmill (see
v1/ansible/optional/skflow/README.md, "Talk bot").

GTD: upserts each actionable finding into the unified GTD via
skos.gtd_ingest.upsert, source "image-monitor", keyed by
"<cluster>/<service>/<image>" so a rerun with nothing new patches the
existing item instead of duplicating it.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class NotConfigured(Exception):
    """Raised when the Talk notify credentials file is missing or incomplete.

    Callers treat this as "skip the Talk post, log a warning, keep going" -
    never as a reason to fail the whole run.
    """


@dataclass
class NotifyCredentials:
    base_url: str
    workspace: str
    token: str


def load_credentials(path: str | Path) -> NotifyCredentials:
    """Read WM_NOTIFY_BASE_URL / WM_NOTIFY_WORKSPACE / WM_NOTIFY_TOKEN from a
    simple KEY=VALUE env file. Raises NotConfigured if the file is absent or
    any key is missing; never logs the token value."""
    p = Path(path).expanduser()
    if not p.is_file():
        raise NotConfigured(f"{p} does not exist yet; see README.md 'Config'")
    values: dict[str, str] = {}
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    missing = [k for k in ("WM_NOTIFY_BASE_URL", "WM_NOTIFY_WORKSPACE", "WM_NOTIFY_TOKEN") if not values.get(k)]
    if missing:
        raise NotConfigured(f"{p} is missing: {', '.join(missing)}")
    return NotifyCredentials(base_url=values["WM_NOTIFY_BASE_URL"].rstrip("/"),
                             workspace=values["WM_NOTIFY_WORKSPACE"], token=values["WM_NOTIFY_TOKEN"])


def post_talk(creds: NotifyCredentials, *, title: str, summary: str, severity: str, room: str,
             ref_id: str = "", timeout: float = 30, urlopen: Callable = urllib.request.urlopen) -> bool:
    """Call f/ops/notify via Windmill's run_wait_result API. Returns whether
    the script reported `posted: true`. Raises on a transport/HTTP error
    (the caller decides whether that should fail the run)."""
    url = f"{creds.base_url}/api/w/{creds.workspace}/jobs/run_wait_result/p/f/ops/notify"
    body = json.dumps({"title": title, "summary": summary, "severity": severity, "room": room,
                      "ref_id": ref_id}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {creds.token}",
        "Content-Type": "application/json",
    })
    with urlopen(req, timeout=timeout) as resp:
        result = json.loads(resp.read() or b"{}")
    return bool(result.get("posted"))


def gtd_upsert_finding(*, cluster: str, service: str, image: str, text: str, status: str = "next",
                       context: str = "@fleet", meta: dict | None = None,
                       upsert_fn: Callable | None = None) -> tuple[str, str]:
    """Upsert one finding into the unified GTD.

    `upsert_fn` is injectable for tests; in production it is
    `skos.gtd_ingest.upsert` with a `GtdCapture`, imported lazily so this
    module (and its tests) don't need `skos` installed to exercise the
    non-GTD parts of the tool.
    """
    source_ref = f"{cluster}/{service}/{image}"
    if upsert_fn is None:
        from skos.gtd_ingest import GtdCapture, upsert as _upsert

        def upsert_fn(**kwargs):
            return _upsert(GtdCapture(**kwargs))

    return upsert_fn(text=text, source="image-monitor", source_ref=source_ref, context=context,
                     status=status, meta=meta or {})
