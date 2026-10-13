"""sksec behind a tunnel or CDN, and two bugs found on a production cluster
whose public traffic arrives through a Cloudflare tunnel (cloudflared):

1. `CROWDSEC_AGENT_HOST` set to a bare service name (`crowdsec`, no port)
   made the bouncer dial the LAPI on port 80 while it listens on 8080. Every
   check failed, and the bouncer fails closed, so every router behind
   `crowdsec-bouncer@file` answered 403 and the bouncer never pulled once.
   The play now refuses a value without `:<port>`.
2. With skfenceha, the bouncer middleware file reached only the workers'
   dynamic dir. The ACME-master Traefik reads `dynamic_acme/`, so every
   router there whose chain names `crowdsec-bouncer@file` (the `default`
   chain once skfenceha has `CROWDSEC_ENABLED`) was disabled. The play now
   writes the same file to both.

Real client IP behind a tunnel needs no new knob, only the existing ones
set together (README "Behind a tunnel or CDN"): skfenceha `TRUSTED_PROXIES`
(the tunnel's source address, so Traefik keeps the edge's X-Forwarded-For
and logs it as ClientHost), sksec `BOUNCER_TRUSTED_PROXIES` (the overlays
plus that address, so the bouncer takes the first untrusted entry from the
right) and `ALLOWLIST_CIDRS` covering the tunnel and internal ranges, so
they can never be banned. Unset, nothing renders differently."""
import shutil
import sys
from pathlib import Path

import jinja2
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "render"))
import render_playbooks as rp  # noqa: E402
from test_sksec_bouncer_knobs import BASE, render  # noqa: E402

V1 = Path(__file__).resolve().parents[1]
SVC = V1 / "ansible/core/sksec"
SKFENCEHA = V1 / "ansible/core/skfenceha"
PLAYBOOKS = [SVC / f"deploy_sksec-{e}.yml" for e in ("dev", "staging", "prod")]

needs_ansible = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")


def _tasks(pb):
    out = []

    def walk(ts):
        for t in ts or []:
            out.append(t)
            for k in ("block", "rescue", "always"):
                walk(t.get(k))

    for play in yaml.safe_load(pb.read_text()):
        for k in ("pre_tasks", "tasks", "post_tasks"):
            walk(play.get(k))
    return out


def _middleware_task(pb):
    hits = [t for t in _tasks(pb)
            if "sksec-dynamic.yml.j2" in str(t.get("ansible.builtin.template", t.get("template", "")))]
    assert len(hits) == 1, [t.get("name") for t in hits]
    return hits[0]


def _dirs(task, traefik):
    """The Traefik dynamic dirs the middleware task writes for this edge."""
    expr = task.get("loop")
    assert isinstance(expr, str) and expr.strip().startswith("{{"), "the dir list must be a loop expression"
    j = jinja2.Environment(undefined=jinja2.StrictUndefined)
    return yaml.safe_load(j.from_string(expr).render(traefik_service_name=traefik))


# --- 1. CROWDSEC_AGENT_HOST must carry the LAPI port ---------------------

@needs_ansible
@pytest.mark.parametrize("value", ["crowdsec", "sksec-prod_crowdsec", "http://sksec-prod_crowdsec:8080", "sksec-prod_crowdsec:"])
def test_agent_host_without_a_port_stops_the_play(tmp_path, value):
    res, _ = render(tmp_path, BASE | {"CROWDSEC_AGENT_HOST": value})
    assert res["status"] == "FAIL"
    assert "CROWDSEC_AGENT_HOST must be <host>:<port>" in res["log"], res["log"][-2000:]


@needs_ansible
@pytest.mark.parametrize("value,rendered", [
    (None, "sksec-{APP_ENV}_crowdsec:8080"),
    ("", "sksec-{APP_ENV}_crowdsec:8080"),
    ("sksec-prod_crowdsec:8080", "sksec-prod_crowdsec:8080"),
    ("crowdsec:8080", "crowdsec:8080"),
])
def test_agent_host_with_a_port_or_unset_renders(tmp_path, value, rendered):
    """Unset or empty: the stack's own LAPI service on 8080 (an empty value
    used to render an empty host)."""
    rendered = rendered.format(APP_ENV=BASE["APP_ENV"])
    vault = dict(BASE)
    if value is not None:
        vault["CROWDSEC_AGENT_HOST"] = value
    res, out = render(tmp_path, vault)
    assert res["status"] == "PASS", (res["findings"], res["log"][-2000:])
    assert f"CROWDSEC_AGENT_HOST={rendered}" in (out / "sksec.env").read_text().splitlines()


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_agent_host_check_runs_before_anything_is_written(pb):
    names = [t.get("name", "") for t in _tasks(pb)]
    check = next(i for i, n in enumerate(names) if "CROWDSEC_AGENT_HOST" in n)
    first_write = next(i for i, t in enumerate(_tasks(pb))
                       if any(k in t for k in ("template", "ansible.builtin.template", "copy", "ansible.builtin.copy")))
    assert check < first_write


# --- 2. the bouncer middleware reaches the ACME-master Traefik too ---------

@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_skfenceha_gets_the_middleware_in_both_dynamic_dirs(pb):
    task = _middleware_task(pb)
    assert _dirs(task, "skfenceha") == ["dynamic", "dynamic_acme"]
    assert "{{ item }}/sksec.yml" in str(task.get("ansible.builtin.template", task.get("template"))["dest"])


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_skfence_keeps_only_its_one_dynamic_dir(pb):
    assert _dirs(_middleware_task(pb), "skfence") == ["dynamic"]


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_every_dir_written_is_created_first(pb):
    tasks = _tasks(pb)
    mw = tasks.index(_middleware_task(pb))
    mkdirs = [t for t in tasks[:mw]
              if "state" in str(t.get("ansible.builtin.file", t.get("file", "")))
              and "traefik_service_name" in str(t.get("ansible.builtin.file", t.get("file", "")))
              and "{{ item }}" in str(t.get("ansible.builtin.file", t.get("file", "")))]
    assert mkdirs, "the dynamic dirs must be created before the middleware is written"
    assert mkdirs[-1].get("loop") == _middleware_task(pb).get("loop")


def test_the_acme_traefik_mounts_the_dir_sksec_writes():
    compose = (SKFENCEHA / "src/config/skfenceha/skfenceha.yml.j2").read_text()
    assert "/dynamic_acme:/etc/traefik/dynamic" in compose
    assert "/dynamic:/etc/traefik/dynamic" in compose


@needs_ansible
def test_render_writes_identical_files_to_both_dirs(tmp_path):
    res, out = render(tmp_path, BASE)
    assert res["status"] == "PASS", (res["findings"], res["log"][-2000:])
    fence = out.parent / "skfenceha-prod"
    worker = fence / "dynamic" / "sksec.yml"
    acme = fence / "dynamic_acme" / "sksec.yml"
    assert worker.exists() and acme.exists(), sorted(p.name for p in fence.rglob("*"))
    assert worker.read_text() == acme.read_text()
    doc = yaml.safe_load(acme.read_text())
    assert "crowdsec-bouncer" in doc["http"]["middlewares"]


# --- 3. the tunnel recipe is documented and uses only existing knobs -------

def test_readme_documents_the_tunnel_recipe():
    readme = (SVC / "README.md").read_text()
    section = readme.split("### Behind a tunnel or CDN", 1)
    assert len(section) == 2, "README needs a 'Behind a tunnel or CDN' section"
    body = section[1].split("\n## ", 1)[0]
    for needle in ("skfenceha", "TRUSTED_PROXIES", "BOUNCER_TRUSTED_PROXIES", "ALLOWLIST_CIDRS",
                   "ClientHost", "X-Forwarded-Host", "CF-Connecting-IP"):
        assert needle in body, needle
    assert 'CROWDSEC_AGENT_HOST: "sksec-prod_crowdsec:8080"' in readme


def test_skfenceha_trusted_proxies_reach_both_public_entrypoints():
    src = (SKFENCEHA / "src/config/skfenceha/traefik-worker.yml.j2").read_text()
    head = src.split("entryPoints:", 1)[1]
    web, rest = head.split("  websecure:", 1)
    websecure = rest.split("\n  ssh:", 1)[0] if "\n  ssh:" in rest else rest
    for block in (web, websecure):
        assert "skfenceha.TRUSTED_PROXIES" in block and "trustedIPs:" in block
