"""skmon v2 deploy script and default probe discovery (Task 9)."""
import json
import re

import pytest
import yaml

import skmon_support as s

RETIRED = ("prometheus", "promtail", "cadvisor", "node-exporter", "jaeger")


def test_no_forced_update_when_parked():
    out = s.render_deploy(replicas=0)
    assert "service update --force" not in out
    assert "preexisted " not in out.replace("preexisted() {", "")
    assert "record_preexisting \"" not in out


def test_no_retired_services_in_deploy_script():
    for out in (s.render_deploy(), s.render_deploy(replicas=0), s.render_deploy(env="dev")):
        for n in RETIRED:
            assert f"_{n}" not in out, n


def test_stack_deploy_prunes_parked_or_not():
    for kw in ({}, {"replicas": 0}):
        assert "docker stack deploy" in s.render_deploy(**kw)
        assert re.search(r"docker stack deploy[^\n]*\\\n\s+-c[^\n]*\\\n\s+--prune", s.render_deploy(**kw))


def test_unparked_script_updates_and_waits_for_the_v2_services_only():
    out = s.render_deploy()
    recorded = re.search(r'^record_preexisting (.*)', out, re.M).group(1)
    assert set(re.findall(r'STACK_NAME\}_([a-z-]+)', recorded)) == set(s.V2_SERVICES)
    forced = re.findall(r'preexisted \$\{STACK_NAME\}_([a-z-]+) && .*service update --force', out)
    assert sorted(forced) == sorted(s.V2_SERVICES)
    waited = re.findall(r'--filter "name=\$\{STACK_NAME\}_([a-z-]+)"', out)
    assert sorted(waited) == sorted(s.WAITED_SERVICES)


def test_parked_script_skips_health_waits_and_says_so():
    out = s.render_deploy(replicas="0")
    assert "--filter \"name=" not in out and "skipping health waits" in out
    assert "sleep 30" not in out


@pytest.mark.parametrize("env,sfx", [("prod", ""), ("dev", "-dev")])
def test_url_summary_lists_grafana_vmui_alertmanager(env, sfx):
    out = s.render_deploy(env=env)
    for host in ("skmon", "vmui", "alertmanager"):
        assert f"https://{host}{sfx}.c1.example.test" in out
    assert "Prometheus" not in out and "9090" not in out


def test_deploy_script_has_no_dashes():
    assert "—" not in s.render_deploy() and "–" not in s.render_deploy()


# --- probe discovery -----------------------------------------------------------------

def test_probe_default_from_router_hosts():
    got = s.probes_from(s.FAKE_SERVICE_INSPECT)
    by_url = {p["url"]: p for p in got}
    assert set(by_url) == {"https://app.render.example.test", "https://www.render.example.test",
                           "https://render.example.test", "https://api.render.example.test"}
    assert by_url["https://app.render.example.test"]["forward_auth"] is True
    assert all(not p["forward_auth"] for u, p in by_url.items() if "app." not in u)
    for p in got:
        assert p["name"] == p["url"].removeprefix("https://")
        assert p["severity"] == "critical" and p["expect"] == "2xx|3xx"


def test_multi_argument_host_rules_yield_a_probe_per_host():
    got = s.probes_from([s._svc("multi", {
        "traefik.http.routers.m.rule": "Host(`a.render.example.test`, `b.render.example.test`) && PathPrefix(`/x`)",
        "traefik.http.routers.n.rule": "Host(`c.render.example.test`) || Host(`d.render.example.test`,`e.render.example.test`)"})])
    assert [p["name"] for p in got] == [f"{x}.render.example.test" for x in "abcde"]


def test_hostregexp_and_hostsni_are_not_probed():
    rules = ["HostRegexp(`^.+\\.render\\.example\\.test$`)", "HostSNI(`sni.render.example.test`)",
             "HostSNI(`*`)", "HostRegexp(`^[a-z]+\\.render\\.example\\.test$`)"]
    assert s.probes_from([s._svc(f"r{i}", {f"traefik.http.routers.r{i}.rule": r}) for i, r in enumerate(rules)]) == []
    mixed = s.probes_from([s._svc("x", {"traefik.http.routers.x.rule": "HostSNI(`s.example.test`) || Host(`h.example.test`)"})])
    assert [p["name"] for p in mixed] == ["h.example.test"]


def test_probe_names_are_unique_and_one_per_host():
    twice = s.FAKE_SERVICE_INSPECT + [s._svc("app_alias", {
        "traefik.http.routers.alias.rule": "Host(`app.render.example.test`)"})]
    got = s.probes_from(twice)
    assert len({p["name"] for p in got}) == len(got) == 4
    # A host on two routers is behind forward auth when either router is.
    assert next(p for p in got if p["name"] == "app.render.example.test")["forward_auth"] is True


def test_forward_auth_follows_the_knob():
    assert not next(p for p in s.probes_from(s.FAKE_SERVICE_INSPECT, "other@file") if p["name"] == "app.render.example.test")["forward_auth"]


def _routed(name: str, host: str, mode: dict | None) -> dict:
    svc = s._svc(name, {f"traefik.http.routers.{name}.rule": f"Host(`{host}`)"})
    if mode is not None:
        svc["Spec"]["Mode"] = mode
    return svc


def test_zero_replica_service_yields_no_probe():
    # spec 7.4: a parked service (scaled to 0, labels intact) alerts on nothing (R83)
    for replicas in (0, "0"):
        assert s.probes_from([_routed("parked", "parked.render.example.test", {"Replicated": {"Replicas": replicas}})]) == []


def test_one_replica_and_global_services_are_probed():
    got = s.probes_from([_routed("one", "one.render.example.test", {"Replicated": {"Replicas": 1}}),
                         _routed("glob", "glob.render.example.test", {"Global": {}}),
                         _routed("nomode", "nomode.render.example.test", None)])
    assert [p["name"] for p in got] == ["glob.render.example.test", "nomode.render.example.test", "one.render.example.test"]


def test_host_kept_while_any_live_service_serves_it():
    shared = "shared.render.example.test"
    got = s.probes_from([_routed("old", shared, {"Replicated": {"Replicas": 0}}),
                         _routed("new", shared, {"Replicated": {"Replicas": 2}})])
    assert [p["name"] for p in got] == [shared]


def test_discovery_ignores_other_labels_and_empty_input():
    assert s.probes_from([]) == [] and s.probes_from(None) == []
    assert s.probes_from([{"Spec": {}}, {"ID": "x"}]) == []
    assert s.probes_from([s._svc("x", {"traefik.http.routers.r.rule": "PathPrefix(`/x`)"})]) == []


def _play_probes(inspect, **extra):
    ctx = {**s.guard_facts(), "skmon_service_inspect": {"stdout": json.dumps(inspect)},
           "skmon_authentik_host": extra.pop("host", ""), **extra}
    return s.eval_play_fact("Set the skmon probe list", "skmon_probes", **ctx)


def test_play_sets_probes_from_the_inspect_output_and_adds_authentik():
    got = _play_probes(s.FAKE_SERVICE_INSPECT, host="sso.c1.example.test")
    assert got[-1] == {"name": "authentik", "url": "https://sso.c1.example.test/-/health/live/",
                       "severity": "critical", "expect": 200, "forward_auth": False}
    assert len(got) == 5 and got[0]["url"].startswith("https://api.")


def test_play_probes_vault_list_wins_over_discovery():
    mine = [{"name": "mine", "url": "https://mine.example.test", "severity": "warning"}]
    ctx = s.guard_facts(PROBES=mine)
    got = s.eval_play_fact("Set the skmon probe list", "skmon_probes", **{
        **ctx, "skmon_service_inspect": {"stdout": json.dumps(s.FAKE_SERVICE_INSPECT)}, "skmon_authentik_host": ""})
    assert got == mine


def test_play_without_inspect_output_and_authentik_host_gives_no_probes():
    # A skipped inspect task (nothing to inspect, or parked) leaves no stdout: no probes, no failure.
    got = s.eval_play_fact("Set the skmon probe list", "skmon_probes", **{**s.guard_facts(), "skmon_service_inspect": {"skipped": True}, "skmon_authentik_host": ""})
    assert got == []


def test_operator_authentik_entry_is_kept():
    mine = [{"name": "authentik", "url": "https://x.example.test/ok", "expect": 200}]
    got = s.eval_play_fact("Set the skmon probe list", "skmon_probes", **{
        **s.guard_facts(PROBES=mine), "skmon_authentik_host": "sso.c1.example.test"})
    assert got == mine


def _host(env="prod", vault=None, **skmon):
    ctx = {**s.guard_facts(env=env, **skmon), "env": env}
    if vault is not None:
        ctx["skmon_sksso_vault"] = {"sksso": vault}
    return s.eval_play_fact("Derive the Authentik host for the health probe", "skmon_authentik_host", **ctx).strip()


def test_authentik_host_knob_wins():
    assert _host(AUTHENTIK_HOST="sso.knob.example.test", vault={"CLUSTERNAME": "c1", "DOMAIN": "example.test"}) == "sso.knob.example.test"


def test_authentik_host_from_sksso_vault_like_sksso_routes():
    v = {"CLUSTERNAME": "c1", "DOMAIN": "example.test"}
    assert _host(vault=v, AUTHENTIK_HOST="") == "sso.c1.example.test"
    assert _host(env="dev", vault=v, AUTHENTIK_HOST="") == "sso-dev.c1.example.test"
    assert _host(vault={**v, "CLOUDFLARED": True}, AUTHENTIK_HOST="") == "sso.example.test"


def test_authentik_host_empty_without_knob_or_vault():
    assert _host(AUTHENTIK_HOST="") == ""


def test_authentik_probe_tasks_never_fail_the_deploy():
    for play in s.PLAYS:
        names = [t["name"] for t in s.play_tasks(play)]
        assert "Say that the Authentik health probe is left out" in names
        load = s.task_named(play, "Load sksso's vault when it exists")
        assert load.get("no_log") is True and "fileglob" in str(load["when"])


def test_discovery_commands_are_read_only_and_skip_when_parked():
    for play in s.PLAYS:
        for name in ("List the Swarm service IDs", "Inspect every service for its Traefik router labels"):
            t = s.task_named(play, name)
            assert t["changed_when"] is False and "not skmon_parked" in str(t["when"])
    t = s.task_named(s.PLAYS[0], "Inspect every service for its Traefik router labels")
    assert "docker" in str(t["command"]) and "inspect" in str(t["command"])


# --- the probes reach VictoriaMetrics as labelled blackbox targets (end to end) ------

def _blackbox_job(probes):
    cfg = yaml.safe_load(s.render(s.CFG / "vm-scrape.yml.j2", extra_ctx={
        "skmon_agent_targets": s.agent_targets(), "skmon_probes": probes}))
    return {j["job_name"]: j for j in cfg["scrape_configs"]}.get("alloy-blackbox")


def _target(job, probe):
    return next(g for g in job["static_configs"] if g["labels"]["probe"] == probe)


def test_probe_series_labels_reach_the_blackbox_job():
    probes = _play_probes(s.FAKE_SERVICE_INSPECT, host="sso.c1.example.test")
    job = _blackbox_job(probes)
    app = _target(job, "app.render.example.test")
    assert app["targets"] == ["https://app.render.example.test"]
    assert app["labels"]["severity"] == "critical" and app["labels"]["forward_auth"] == "true"
    assert app["labels"]["__tmp_module"] == "http_2xx_3xx"
    auth = _target(job, "authentik")
    assert auth["targets"] == ["https://sso.c1.example.test/-/health/live/"]
    assert auth["labels"]["forward_auth"] == "false" and auth["labels"]["__tmp_module"] == "http_200"
    assert len(job["static_configs"]) == len(probes) == 5


def test_blackbox_job_passes_target_and_module_to_the_agent_exporter():
    job = _blackbox_job(_play_probes(s.FAKE_SERVICE_INSPECT))
    assert job["metrics_path"].endswith("prometheus.exporter.blackbox.probes/metrics")
    rel = {(r.get("target_label"), tuple(r.get("source_labels", []))): r for r in job["relabel_configs"]}
    assert ("__param_target", ("__address__",)) in rel and ("instance", ("__address__",)) in rel
    assert ("__param_module", ("__tmp_module",)) in rel
    assert rel[("__address__", ())]["replacement"] == "192.0.2.10:12345"  # the DATA_NODE agent


def test_vault_probe_without_forward_auth_or_severity_gets_defaults():
    job = _blackbox_job([{"name": "mine", "url": "https://mine.example.test"}])
    assert _target(job, "mine")["labels"]["forward_auth"] == "false" and _target(job, "mine")["labels"]["severity"] == "warning"


def test_no_blackbox_job_without_probes():
    assert _blackbox_job([]) is None


def test_agent_play_reads_the_probe_fact_from_the_selected_manager():
    t = s.task_named(s.AGENT_PLAY, "Derive this host's agent settings")
    assert "selected_manager_group" in t["set_fact"]["skmon_agent_probes"]
    assert "skmon_probes" in t["set_fact"]["skmon_agent_probes"]


def test_probes_are_computed_before_the_configs_are_rendered():
    for play in s.PLAYS:
        names = [t["name"] for t in s.play_tasks(play)]
        assert names.index("Set the skmon probe list") < names.index("Render the {{ app }} Swarm config contents")
