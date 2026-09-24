"""
skstacks bridge — load the real SKStacks v2 `app.yaml` descriptors as a skwire Pack,
so skwire can plan/wire the actual sk* stack. Reads provider/secrets/depends_on/ha
off each descriptor and converts `depends_on` → skwire `needs` (paired with the
provider's first secret). Needs PyYAML (optional — only when you use this bridge;
the skwire core stays zero-dep).

    from skwire.packs.skstacks_bridge import load_skstacks
    skwire.register_pack(load_skstacks())          # defaults to this repo's v2/
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from skwire import Pack


def load_skstacks(v2_dir: Optional[str] = None) -> Pack:
    import yaml  # optional dep — only the bridge needs it

    base = Path(v2_dir) if v2_dir else Path(__file__).resolve().parents[2]
    descs: dict[str, dict] = {}
    for f in sorted(base.glob("*/*/app.yaml")):
        try:
            d = yaml.safe_load(f.read_text())
        except Exception:
            continue
        if d and d.get("name"):
            descs[d["name"]] = d

    nodes = []
    for name, d in descs.items():
        prov = (str(d.get("provider") or "service").split() or ["service"])[0].lower()
        node = {"name": name, "provides": {"url": f"http://{name}", "api_kind": prov}}
        secs = [s for s in (d.get("secrets") or []) if isinstance(s, dict) and s.get("key")]
        if secs:
            node["secrets"] = [
                {"key": s["key"], **({"rotation_days": s["rotation_days"]} if s.get("rotation_days") else {})}
                for s in secs
            ]
        if d.get("ha"):
            node["critical"] = True
        needs = []
        for dep in (d.get("depends_on") or []):
            if dep not in descs:
                continue                       # don't wire to a service we don't have
            dep_secs = [s for s in (descs[dep].get("secrets") or []) if isinstance(s, dict) and s.get("key")]
            secret = dep_secs[0]["key"] if dep_secs else f"{dep}_token"
            needs.append({"service": dep, "secret": secret})
        if needs:
            node["needs"] = needs
        nodes.append(node)

    return Pack(name="skstacks", nodes=nodes)
