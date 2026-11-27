"""Render one role's deploy playbook through REAL Ansible
(render/render_playbooks.py) with a given vault, for the skai, skcorpus and
skembed render tests. Same mechanism as test_skpayment_render.py."""
import shutil
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "render"))
import render_playbooks as rp  # noqa: E402


def example(service: str, kind: str = "example") -> dict:
    return yaml.safe_load((rp.VARS / f"{service}.{kind}.yml").read_text())[service]


def render(tmp: Path, service: str, vault: dict, env: str = "prod", extra: dict | None = None):
    """Returns (result, out_dir). `extra` maps other services to their vault
    namespace (staged as <svc>.with.<other>.yml, like the gate does)."""
    work = tmp / "work"
    if not (work / "ansible").exists():
        shutil.copytree(rp.ANSIBLE, work / "ansible")
        for task_file in (work / "ansible").glob("**/tasks/*.yml"):
            if task_file.name != "select_vault_file.yml":
                rp.rewrite_task_file(task_file)
    vars_dir = tmp / "vars"
    if vars_dir.exists():
        shutil.rmtree(vars_dir)
    vars_dir.mkdir()
    (vars_dir / f"{service}.example.yml").write_text(yaml.safe_dump({service: vault}))
    for other in sorted(set(rp_with(service)) | set(extra or {})):
        ns = (extra or {}).get(other)
        if ns is None:
            ns = yaml.safe_load((rp.VARS / f"{service}.with.{other}.yml").read_text())[other]
        (vars_dir / f"{service}.with.{other}.yml").write_text(yaml.safe_dump({other: ns}))
    run_dir = work / f"{service}-{env}-base"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    saved = rp.VARS
    rp.VARS = vars_dir
    try:
        pb = work / "ansible" / "optional" / service / f"deploy_{service}-{env}.yml"
        res = rp.run_one(work, "optional", service, env, pb, "base")
    finally:
        rp.VARS = saved
    log = run_dir / "ansible.log"
    res["log"] = log.read_text() if log.exists() else ""
    return res, run_dir / "out"


def rp_with(service: str) -> list[str]:
    return [p.name.split(".with.")[1][:-4] for p in rp.VARS.glob(f"{service}.with.*.yml")]


def ok(tmp, service, vault, env="prod", extra=None):
    res, out = render(tmp, service, vault, env, extra)
    assert res["status"] == "PASS", (res["findings"], res["log"][-3000:])
    return out


def fails(tmp, service, vault, env="prod", extra=None) -> str:
    res, _ = render(tmp, service, vault, env, extra)
    assert res["status"] != "PASS", "render passed but should have failed"
    return res["log"] + "\n".join(res.get("findings") or [])


def env_file(path: Path) -> dict:
    return dict(l.split("=", 1) for l in path.read_text().splitlines() if l and not l.startswith("#"))
