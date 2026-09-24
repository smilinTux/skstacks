"""
skrender CLI — render an app.yaml descriptor to a deployable manifest.

    skrender <C>/<service>            # auto-detect; prints the swarm stack
    skrender cloud/skfence --platform k8s
    skrender cloud/skfence --platform swarm -o platform/swarm/stacks/skfence/docker-compose.yml
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .render import load_descriptor, render_swarm, render_k8s


def _dump(obj, many=False) -> str:
    import yaml
    if many:
        return "---\n".join(yaml.safe_dump(m, sort_keys=False) for m in obj)
    return yaml.safe_dump(obj, sort_keys=False)


def _resolve(path_arg: str) -> Path:
    p = Path(path_arg)
    if p.is_dir():
        p = p / "app.yaml"
    if p.name != "app.yaml" and p.suffix not in (".yaml", ".yml"):
        p = p / "app.yaml"
    return p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="skrender", description="Render app.yaml → Swarm/K8s.")
    ap.add_argument("service", help="path to a service dir or its app.yaml")
    ap.add_argument("--platform", choices=["swarm", "k8s"], default="swarm")
    ap.add_argument("-o", "--out", help="write to file instead of stdout")
    args = ap.parse_args(argv)

    path = _resolve(args.service)
    if not path.exists():
        print(f"no descriptor at {path}", file=sys.stderr)
        return 1
    desc = load_descriptor(path)
    if not desc.get("deploy"):
        print(f"{path} has no `deploy:` block yet — can't render it.", file=sys.stderr)
        return 2

    if args.platform == "swarm":
        text = _dump(render_swarm(desc))
    else:
        text = _dump(render_k8s(desc), many=True)

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text)
        print(f"✓ wrote {args.out}")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
