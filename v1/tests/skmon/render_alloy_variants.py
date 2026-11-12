#!/usr/bin/env python3
"""Render agent.alloy variants for validate_configs.sh.

The render gate (render_playbooks.py) skips import_playbook plays, so agent.yml's
Alloy config is never in its output. This writes the variants the agent play
produces into <out_dir>/alloy-variants/<variant>/agent.alloy, rendered with the
same helpers the tests use (StrictUndefined, fake values only).

usage: render_alloy_variants.py <out_dir>
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from skmon_support import cfg  # noqa: E402

PROBES = [
    {"name": "grafana", "url": "https://skmon.c1.example.test", "severity": "critical"},
    {"name": "api", "url": "https://api.c1.example.test/health", "severity": "warning", "expect": 200},
]
VARIANTS = {
    "container-node": {},
    "container-data-node": {"agent_is_data_node": True, "probes": PROBES},
    "container-storage": {"agent_is_storage": True},
    "systemd-node": {"agent_mode": "systemd"},
}


def main(out: pathlib.Path) -> None:
    for name, ctx in VARIANTS.items():
        dest = out / "alloy-variants" / name / "agent.alloy"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(cfg(**ctx))
        print(f"rendered {dest}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__.strip().splitlines()[-1])
    main(pathlib.Path(sys.argv[1]))
