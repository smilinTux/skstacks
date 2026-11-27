"""While a skmesh service has no running task, Swarm drops its Traefik labels
and the skmesh host fell through to the fence's priority-1 catch-all, which
answers 403. A NetBird client maps a 403 on GetServerKey to PermissionDenied
and stops retrying for good (NeedsLogin until a manual `netbird up`).

The skmesh playbooks now render a file-provider router for the skmesh host at
priority 2 (above the catch-alls, below every skmesh label router, whose
priority is its rule length) to a load balancer with an explicit empty server
list, so Traefik answers 503 (checked against traefik v3.7: `servers: []`
gives 503 "no available server"; `loadBalancer: {}` is rejected and makes the
file provider drop its whole config).
"""
import pathlib
import re

import jinja2
import pytest
import yaml

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
SKMESH = ANSIBLE / "optional/skmesh"
SRC = SKMESH / "src/config/skmesh"
FENCE_ROUTERS = [ANSIBLE / "core/skfence/src/config/skfence/routers.yml.j2",
                 ANSIBLE / "core/skfenceha/src/config/skfenceha/routers.yml.j2"]
SKMESH_BASE = {"CLUSTERNAME": "demo", "DOMAIN": "example.test", "POSTGRES_PASSWORD": "pw"}


def _render(name, envname="prod", **over):
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    return env.from_string((SRC / name).read_text()).render(
        env=envname, skmesh={**SKMESH_BASE, **over}, inventory_hostname="n1")


def _fallback(envname="prod", **over):
    return yaml.safe_load(_render("traefik-unavailable.yml.j2", envname, **over))["http"]


def _label_routers(envname="prod", **over):
    compose = yaml.safe_load(_render("skmesh.yml.j2", envname, **over))
    routers = {}
    for svc in compose["services"].values():
        for label in svc.get("deploy", {}).get("labels", []):
            m = re.fullmatch(r"traefik\.http\.routers\.([^.]+)\.(rule|priority)=(.*)", label)
            if m:
                routers.setdefault(m.group(1), {})[m.group(2)] = m.group(3)
    return routers


@pytest.mark.parametrize("envname,host", [("prod", "skmesh.demo.example.test"),
                                          ("dev", "skmesh-dev.demo.example.test")])
def test_fallback_router_covers_the_skmesh_host_with_an_empty_load_balancer(envname, host):
    http = _fallback(envname)
    (name, router), = http["routers"].items()
    assert router["rule"] == f"Host(`{host}`)"
    assert router["entryPoints"] == ["websecure"]
    assert router["tls"] == {}
    svc = http["services"][router["service"]]
    # an explicit empty list: that is what makes Traefik answer 503
    assert svc == {"loadBalancer": {"servers": []}}


def test_fallback_host_follows_cloudflared_like_the_label_routers():
    rule = next(iter(_fallback(CLOUDFLARED=True)["routers"].values()))["rule"]
    assert rule == "Host(`skmesh.example.test`)"
    assert "Host(`skmesh.example.test`)" in _label_routers(CLOUDFLARED=True)["skmesh-mgmt-grpc"]["rule"]


@pytest.mark.parametrize("envname", ["dev", "prod"])
@pytest.mark.parametrize("mode", ["standalone", "authentik"])
def test_fallback_sits_between_the_catch_alls_and_every_skmesh_label_router(envname, mode):
    prio = next(iter(_fallback(envname)["routers"].values()))["priority"]
    assert isinstance(prio, int)
    routers = _label_routers(envname, AUTH_MODE=mode, SSO_HOST_IP="192.0.2.5")
    assert {"skmesh-mgmt-grpc", "skmesh-mgmt-api", "skmesh-signal", "skmesh-relay", "skmesh-dash"} <= set(routers)
    for name, r in routers.items():
        # no explicit priority: Traefik uses the rule length
        assert int(r.get("priority", len(r["rule"]))) > prio, name
    for path in FENCE_ROUTERS:
        catch_alls = yaml.safe_load(jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
                                    .from_string(path.read_text()).render(env=envname, domain="example.test",
                                                                          cluster_name="demo"))["http"]["routers"]
        for name in ("wildcard-domain", "wildcard-cluster", "catch-all"):
            assert catch_alls[name]["priority"] < prio, (path, name)


@pytest.mark.parametrize("envname", ["dev", "prod"])
def test_playbook_renders_the_fallback_into_each_present_fence_dynamic_dir(envname):
    play = yaml.safe_load((SKMESH / f"deploy_skmesh-{envname}.yml").read_text())
    tasks = [t for p in play if isinstance(p, dict) for t in p.get("tasks", [])]
    stat = next(t for t in tasks if "stat" in t and "dynamic" in t["stat"]["path"])
    assert stat["stat"]["path"] == "/var/data/config/{{ item }}-{{ env }}/dynamic"
    assert stat["loop"] == ["skfence", "skfenceha"]
    tpl = next(t for t in tasks if "template" in t and "traefik-unavailable" in t["template"]["src"])
    # top level of dynamic/, never a subdirectory (the fences refuse those)
    assert tpl["template"]["dest"] == "/var/data/config/{{ item.item }}-{{ env }}/dynamic/{{ app }}-{{ env }}.yml"
    assert stat["register"] in tpl["loop"] and "stat.exists" in tpl["loop"]



@pytest.mark.parametrize("envname", ["dev", "prod"])
def test_skfenceha_fallback_is_touched_on_every_manager(envname):
    # skfenceha's dynamic/ is shared NFS: a write on one manager wakes only that
    # manager's file watch, so each manager must see a local touch
    play = yaml.safe_load((SKMESH / f"deploy_skmesh-{envname}.yml").read_text())
    tasks = [t for p in play if isinstance(p, dict) for t in p.get("tasks", [])]
    tpl = next(t for t in tasks if "template" in t and "traefik-unavailable" in t["template"]["src"])
    touch = next(t for t in tasks if t.get("file", {}).get("state") == "touch"
                 and "{{ app }}-{{ env }}.yml" in t["file"]["path"])
    assert tasks.index(tpl) < tasks.index(touch)
    assert touch["file"]["path"] == "/var/data/config/skfenceha-{{ env }}/dynamic/{{ app }}-{{ env }}.yml"
    assert touch["delegate_to"] == "{{ item }}"
    assert touch["loop"] == "{{ groups[target_manager_group | default('swarm_managers')] | default([]) }}"
    # only when the skfenceha copy changed this run
    assert tpl["register"] in touch["when"] and "'skfenceha'" in touch["when"]


def _touch_when(results):
    play = yaml.safe_load((SKMESH / "deploy_skmesh-prod.yml").read_text())
    tasks = [t for p in play if isinstance(p, dict) for t in p.get("tasks", [])]
    when = next(t for t in tasks if t.get("file", {}).get("state") == "touch")["when"]
    env = jinja2.Environment()
    env.filters["default"] = lambda v, d=None: d if v is None else v
    return env.compile_expression(when.strip())(skmesh_fallback_render={"results": results})


def test_touch_condition_fires_only_for_a_changed_skfenceha_copy():
    assert _touch_when([{"changed": True, "item": {"item": "skfenceha"}}])
    assert not _touch_when([{"changed": False, "item": {"item": "skfenceha"}}])
    assert not _touch_when([{"changed": True, "item": {"item": "skfence"}}])
    assert not _touch_when([])


@pytest.mark.parametrize("fence", ["skfence", "skfenceha"])
@pytest.mark.parametrize("envname", ["dev", "staging", "prod"])
def test_fallback_file_name_is_reserved_against_extra_dynamic_files(fence, envname):
    play = yaml.safe_load((ANSIBLE / f"core/{fence}/deploy_{fence}-{envname}.yml").read_text())
    tasks = [t for p in play if isinstance(p, dict) for t in p.get("tasks", []) + p.get("pre_tasks", [])]
    extra = next(t for t in tasks if "extra_dynamic_reserved" in t.get("vars", {}))
    assert "skmesh-{{ env }}.yml" in extra["vars"]["extra_dynamic_reserved"]
