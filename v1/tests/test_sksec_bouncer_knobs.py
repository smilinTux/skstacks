"""sksec bouncer knobs: BOUNCER_REPLICAS, BOUNCER_HEALTHCHECK (+ its
BOUNCER_HEALTH_CHECK_* timings) and BOUNCER_TRUSTED_PROXIES. The bouncer is
the fail-closed forwardAuth every protected router depends on, so these
make it redundant, health-managed, and stop it from taking a forged
leftmost X-Forwarded-For as the client address. Unset, sksec renders
byte-identical to the baselines rendered from the templates before the
knobs (v1/tests/fixtures/sksec_bouncer_baseline). Rendered through REAL
Ansible (render/render_playbooks.py)."""
import shutil
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "render"))
import render_playbooks as rp  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")

BASELINE = Path(__file__).resolve().parent / "fixtures" / "sksec_bouncer_baseline"
BASE = rp.load_vars(rp.VARS / "sksec.example.yml")["sksec"]
SET = rp.load_vars(rp.VARS / "sksec.set.yml")["sksec"]


def render(tmp: Path, vault: dict, env: str = "prod") -> tuple[dict, Path]:
    work = tmp / "work"
    if not (work / "ansible").exists():
        shutil.copytree(rp.ANSIBLE, work / "ansible")
        for task_file in (work / "ansible").glob("**/tasks/*.yml"):
            if task_file.name != "select_vault_file.yml":
                rp.rewrite_task_file(task_file)
    vars_dir = tmp / "vars"
    vars_dir.mkdir(exist_ok=True)
    (vars_dir / "sksec.example.yml").write_text(yaml.safe_dump({"sksec": vault}))
    run_dir = work / f"sksec-{env}-base"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    saved = rp.VARS
    rp.VARS = vars_dir
    try:
        pb = work / "ansible" / "core" / "sksec" / f"deploy_sksec-{env}.yml"
        res = rp.run_one(work, "core", "sksec", env, pb, "base")
    finally:
        rp.VARS = saved
    res["log"] = (run_dir / "ansible.log").read_text() if (run_dir / "ansible.log").exists() else ""
    return res, run_dir / "out" / "var/data/config" / f"sksec-{env}"


def ok(tmp, vault, env="prod"):
    res, out = render(tmp, vault, env)
    assert res["status"] == "PASS", (res["findings"], res["log"][-3000:])
    return out


def bouncer(out):
    return yaml.safe_load((out / "sksec.yml").read_text())["services"]["bouncer-traefik"]


def env_lines(out):
    return (out / "sksec.env").read_text().splitlines()


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
@pytest.mark.parametrize("pas", ["base", "set"])
def test_unset_knobs_render_byte_identical(tmp_path, env, pas):
    vault = BASE if pas == "base" else rp.deep_merge(BASE, SET)
    out = ok(tmp_path, vault, env)
    for f in ("sksec.yml", "sksec.env"):
        assert (out / f).read_text() == (BASELINE / f"{env}-{pas}-{f}").read_text(), f


def test_explicit_defaults_render_byte_identical(tmp_path):
    out = ok(tmp_path, BASE | {"BOUNCER_REPLICAS": 1, "BOUNCER_HEALTHCHECK": False, "BOUNCER_TRUSTED_PROXIES": []})
    for f in ("sksec.yml", "sksec.env"):
        assert (out / f).read_text() == (BASELINE / f"prod-base-{f}").read_text(), f


def test_replicas_spread_one_per_node(tmp_path):
    b = bouncer(ok(tmp_path, BASE | {"BOUNCER_REPLICAS": 2}))
    assert b["deploy"]["replicas"] == 2
    assert b["deploy"]["placement"]["max_replicas_per_node"] == 1
    assert b["deploy"]["placement"]["preferences"] == [{"spread": "node.labels.zone"}]


def test_healthcheck_defaults_and_overrides(tmp_path):
    b = bouncer(ok(tmp_path, BASE | {"BOUNCER_HEALTHCHECK": True}))
    assert b["healthcheck"] == {"test": ["CMD", "/healthchecker"], "interval": "10s", "timeout": "5s",
                                "retries": 3, "start_period": "20s"}
    b = bouncer(ok(tmp_path, BASE | {"BOUNCER_HEALTHCHECK": True, "BOUNCER_HEALTH_CHECK_INTERVAL": "30s",
                                     "BOUNCER_HEALTH_CHECK_RETRIES": 5, "BOUNCER_HEALTH_CHECK_START_PERIOD": "1m"}))
    assert b["healthcheck"]["interval"] == "30s" and b["healthcheck"]["retries"] == 5
    assert b["healthcheck"]["start_period"] == "1m"
    off = bouncer(ok(tmp_path, BASE))
    assert "healthcheck" not in off


def test_trusted_proxies_render_into_the_bouncer_env(tmp_path):
    out = ok(tmp_path, BASE | {"BOUNCER_TRUSTED_PROXIES": ["172.16.200.0/24", "192.0.2.0/24", "2001:db8::/32"]})
    lines = env_lines(out)
    assert "TRUSTED_PROXIES=172.16.200.0/24,192.0.2.0/24,2001:db8::/32" in lines
    assert bouncer(out)["env_file"] == "/var/data/config/sksec-prod/sksec.env"


@pytest.mark.parametrize("over,needle", [
    ({"BOUNCER_REPLICAS": 0}, "BOUNCER_REPLICAS must be"),
    ({"BOUNCER_REPLICAS": "two"}, "BOUNCER_REPLICAS must be"),
    ({"BOUNCER_TRUSTED_PROXIES": "192.0.2.0/24"}, "a YAML list"),
    ({"BOUNCER_TRUSTED_PROXIES": ["0.0.0.0/0"]}, "not 0.0.0.0/0"),
    ({"BOUNCER_TRUSTED_PROXIES": ["::/0"]}, "not 0.0.0.0/0"),
    ({"BOUNCER_TRUSTED_PROXIES": ["proxy.example.test"]}, "a YAML list"),
])
def test_invalid_knobs_stop_the_play(tmp_path, over, needle):
    res, _ = render(tmp_path, BASE | over)
    assert res["status"] == "FAIL"
    assert needle in res["log"], res["log"][-2000:]
