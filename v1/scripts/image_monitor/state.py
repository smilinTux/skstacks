"""Last-seen-digest state, so a rerun reports each change exactly once.

State is a flat JSON file keyed by "<cluster>/<service>", valued with the
last-seen image reference. The first run for a key has nothing to compare
against, so it is recorded as a baseline, not reported as "changed" (there
is no meaningful "before" to show yet).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class StateStore:
    path: Path
    _data: dict

    @classmethod
    def load(cls, path: str | Path) -> "StateStore":
        p = Path(path).expanduser()
        try:
            data = json.loads(p.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            data = {}
        return cls(path=p, _data=data)

    def key(self, cluster: str, service: str) -> str:
        return f"{cluster}/{service}"

    def last_image(self, cluster: str, service: str) -> str | None:
        return self._data.get(self.key(cluster, service), {}).get("image")

    def is_first_seen(self, cluster: str, service: str) -> bool:
        return self.key(cluster, service) not in self._data

    def record(self, cluster: str, service: str, image: str) -> None:
        self._data[self.key(cluster, service)] = {"image": image}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True))
        tmp.replace(self.path)


def image_changed(store: StateStore, cluster: str, service: str, current_image: str) -> bool:
    """Whether current_image differs from what was last recorded for this
    service - False on first sight (nothing to compare against yet)."""
    if store.is_first_seen(cluster, service):
        return False
    return store.last_image(cluster, service) != current_image
