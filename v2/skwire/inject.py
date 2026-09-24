"""
skwire inject — push a secret/config value into a target. RecordingInjector is the
default (records calls; great for dry-run + tests). Real injectors implement the
Injector contract: env-override, config-file write, or app-API POST.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


@dataclass(frozen=True)
class InjectCall:
    target: str
    key: str
    value: str


class RecordingInjector:
    """Default injector — records what would be injected (dry-run / test / audit)."""
    method = "record"

    def __init__(self):
        self.calls: list[InjectCall] = []
        self.written: dict[str, str] = {}

    def inject(self, target: str, key: str, value: str) -> bool:
        self.calls.append(InjectCall(target, key, value))
        self.written[f"{target}:{key}"] = value
        return True


class EnvFileInjector:
    """Write `KEY=value` into `{dir}/{target}.env` (idempotent — updates in place)."""
    method = "env"

    def __init__(self, directory: str = "."):
        self._dir = Path(directory).expanduser()

    def inject(self, target: str, key: str, value: str) -> bool:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"{target}.env"
        lines = path.read_text().splitlines() if path.exists() else []
        out, found = [], False
        for ln in lines:
            if ln.split("=", 1)[0].strip() == key:
                out.append(f"{key}={value}"); found = True
            else:
                out.append(ln)
        if not found:
            out.append(f"{key}={value}")
        path.write_text("\n".join(out) + "\n")
        return True


class JsonConfigInjector:
    """Merge `{key: value}` into a JSON config at `{dir}/{target}.json`."""
    method = "file"

    def __init__(self, directory: str = "."):
        self._dir = Path(directory).expanduser()

    def inject(self, target: str, key: str, value: str) -> bool:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"{target}.json"
        data = json.loads(path.read_text()) if path.exists() else {}
        data[key] = value
        path.write_text(json.dumps(data, indent=2))
        return True


def _urllib_post(url: str, payload: dict, headers: dict) -> bool:
    import urllib.request
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", **headers}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return 200 <= r.status < 300


class ApiPostInjector:
    """POST `{key: value}` to a target's config API: url_template uses `{target}`.
    `post` is injectable (DI) for testing / custom transport; default uses urllib."""
    method = "api"

    def __init__(self, url_template: str, headers: Optional[dict] = None,
                 post: Optional[Callable[[str, dict, dict], bool]] = None):
        self._tpl = url_template
        self._headers = headers or {}
        self._post = post or _urllib_post

    def inject(self, target: str, key: str, value: str) -> bool:
        url = self._tpl.format(target=target)
        try:
            return bool(self._post(url, {key: value}, self._headers))
        except Exception:
            return False
