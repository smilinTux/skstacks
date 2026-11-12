"""skmon v2 agent play (agent.yml) and the host-container run script it installs."""
import re

import yaml

from image_pins import template_default
from skmon_support import (AGENT_PLAY, AGENT_RUN, AGENT_TARGETS, CFG, PLAYS, ROLE, cfg, load_tasks, mounts,
                           play_steps, play_tasks, render_run_script, run_script_args)

PINS = yaml.safe_load((CFG / "pins.yml").read_text())


def test_docker_run_flags():
    a = run_script_args()
    assert a["--network"] == "host" and a["--pid"] == "host" and a["--restart"] == "unless-stopped"
    assert a["--name"] == "skmon-prod-alloy"
    assert "--privileged" not in a and "--cap-add" not in a


def test_cgroup_namespace_is_the_hosts():
    # without it the cadvisor component sees only the agent's own cgroup
    assert run_script_args()["--cgroupns"] == "host"


def test_systemd_collector_reaches_systemd():
    # the system bus refuses AppArmor docker-default peers, so systemd's private socket is mounted;
    # without :ro, which would suggest a restriction a socket bind mount does not have (R31)
    assert "/run/systemd/private:/run/systemd/private" in mounts() and "-e" not in run_script_args()
    assert not any(m.startswith("/run/systemd/private:") and m.endswith(":ro") for m in mounts())
    assert "root-equivalent control channel" in " ".join(AGENT_RUN.read_text().replace("#", " ").split())


def test_systemd_private_socket_only_in_container_mode():
    # the mount lives only in the container run script, which the play writes and runs only in
    # container mode; the agent config (shared with systemd mode) mounts nothing
    tasks = play_tasks(AGENT_PLAY)
    for name in ("Write the agent run script", "Start, replace or remove the agent container"):
        assert next(t for t in tasks if t.get("name") == name)["when"] == "skmon_agent_mode == 'container'"
    assert "/run/systemd/private" not in AGENT_PLAY.read_text()
    assert "/run/systemd/private:" not in cfg(agent_mode="systemd") + cfg(agent_mode="container")


def test_resource_caps():
    a = run_script_args()
    assert (a["--memory"], a["--memory-reservation"], a["--cpus"]) == ("384m", "128m", "0.5")


def test_read_only_mounts():
    want = {"/proc:/host/proc:ro", "/sys:/host/sys:ro", "/:/rootfs:ro", "/var/lib/docker/containers:/var/lib/docker/containers:ro",
            "/var/log:/var/log:ro", "/run/log/journal:/run/log/journal:ro", "/var/log/journal:/var/log/journal:ro",
            "/var/run/docker.sock:/var/run/docker.sock:ro"}
    assert want <= set(mounts())


def test_config_and_state_on_local_disk():
    assert "/etc/skmon-prod/alloy:/etc/alloy:ro" in mounts() and "/var/lib/skmon-prod/alloy:/var/lib/alloy" in mounts()
    assert not any(m.startswith("/var/data") for m in mounts())


def test_no_published_ports_and_no_traefik():
    assert "-p" not in run_script_args() and "--publish" not in run_script_args()
    assert "traefik" not in render_run_script()


def test_alloy_command_line():
    a = run_script_args()
    assert a["--storage.path"] == "/var/lib/alloy"
    # the loki.write WAL block is experimental in Alloy v1.21
    assert a["--stability.level"] == "experimental"
    assert a.positional[-2:] == ["run", "/etc/alloy/config.alloy"]


def test_image_default_equals_pin():
    p = PINS["images"]["alloy"]
    assert template_default(AGENT_RUN.read_text(), "ALLOY_IMAGE") == f"{p['repo']}:{p['tag']}@{p['digest']}"
    assert run_script_args().positional[0] == f"{p['repo']}:{p['tag']}@{p['digest']}"


def test_parked_removes_the_container():
    s = render_run_script(skmon_parked=True)
    assert "docker rm -f skmon-prod-alloy" in s and "docker run" not in s


def test_run_is_idempotent_on_config_hash():
    s = render_run_script()
    assert "skmon.config-hash" in s  # label compared before replacing the container
    assert run_script_args()["--label"] == "skmon.config-hash=" + "0" * 64
    compare = s.index('"$have" = "$want"')
    assert compare < s.index("docker rm -f skmon-prod-alloy") < s.index("docker run")


def test_env_names_the_container_and_dirs():
    a = run_script_args(env="dev")
    assert a["--name"] == "skmon-dev-alloy" and "/etc/skmon-dev/alloy:/etc/alloy:ro" in mounts(env="dev")


def test_hosts_pattern():
    assert yaml.safe_load((ROLE / "agent.yml").read_text())[0]["hosts"] == "skmon_agent_hosts"


# --- the play ---

def _play():
    return yaml.safe_load(AGENT_PLAY.read_text())[0]


def test_play_loads_the_vault_and_imports_facts_only():
    pre = _play()["pre_tasks"]
    assert any(str(t.get("import_tasks", "")).endswith("shared/tasks/select_vault_file.yml") for t in pre)
    assert any(t.get("include_vars") == "{{ vault_file_path }}" for t in pre)
    imports = [t.get("import_tasks") or t.get("include_tasks") for t in pre]
    assert "tasks/facts.yml" in imports and not any("guards" in str(i) for i in imports)


def test_container_tasks_only_in_container_mode():
    tasks = play_tasks(AGENT_PLAY)
    names = ("Create the agent config and state directories", "Create the textfile collector directory",
             "Write the agent config",
             "Write the agent run script", "Start, replace or remove the agent container")
    for name in names:
        t = next(t for t in tasks if t.get("name") == name)
        when = t.get("when", "")
        assert "skmon_agent_mode == 'container'" in (when if isinstance(when, str) else " ".join(when)), name


def test_local_dirs_and_modes():
    tasks = play_tasks(AGENT_PLAY)
    dirs = next(t for t in tasks if t.get("name") == "Create the agent config and state directories")
    assert dirs["file"]["mode"] == "0750"
    assert set(dirs["loop"]) == {"/etc/skmon-{{ env }}/alloy", "/var/lib/skmon-{{ env }}/alloy"}
    assert next(t for t in tasks if t.get("name") == "Write the agent config")["copy"]["mode"] == "0640"
    run = next(t for t in tasks if t.get("name") == "Write the agent run script")["template"]
    assert run["dest"] == "/usr/local/sbin/skmon-{{ env }}-alloy-run" and run["mode"] == "0750"


def test_nothing_on_the_agent_path_touches_var_data():
    assert "/var/data" not in AGENT_PLAY.read_text()


def _layout(p):
    """The env play's steps after the header import and the select-manager play."""
    steps = play_steps(p)
    assert steps[0] == ("import", {"file": "../../shared/meta/skstack-header.yml", "vars": {}})
    return steps[2:]


def test_env_plays_import_the_agent_play_around_the_stack_deploy():
    # R80: prepare (facts, probes, agent group), agents pre, stack, agents post, Authentik last (R81)
    for p in PLAYS:
        env = re.search(r"deploy_skmon-(\w+)\.yml", p.name).group(1)
        prep, pre, stack, post, auth = _layout(p)
        assert prep[0] == "play" and "Set the skmon probe list" in prep[1]
        assert prep[1][-1] == "Put every skmon agent host in the agent play's group", p.name
        assert pre == ("import", {"file": "agent.yml", "vars": {"env": env, "skmon_agent_phase": "pre"}}), p.name
        assert stack[0] == "play" and "Deploy {{ app }} stack" in stack[1], p.name
        assert "Render the {{ app }} Swarm config contents" in stack[1]
        assert post == ("import", {"file": "agent.yml", "vars": {"env": env, "skmon_agent_phase": "post"}}), p.name
        assert auth[0] == "play" and "Create or update the skmon providers and applications in Authentik" in auth[1]


def test_nothing_the_agent_play_reads_is_made_after_the_pre_import():
    # the agents converge before the stack: the probe list and the agent group come first
    for p in PLAYS:
        prep, _, stack, _, _ = _layout(p)
        for name in ("Set the skmon probe list", "Put every skmon agent host in the agent play's group",
                     "Check the skmon vault and derive the data facts", "Derive the skmon agent scrape targets"):
            assert name in prep[1] and name not in stack[1], (p.name, name)


def _gate():
    return next(t for t in play_tasks(AGENT_PLAY) if t.get("meta") == "end_host")


def _agent_does_work(phase, parked):
    from skmon_support import ansible_environment
    when = _gate()["when"]
    ctx = {"skmon_parked": parked, **({"skmon_agent_phase": phase} if phase is not None else {})}
    return not ansible_environment().compile_expression(when.strip())(**ctx)


def test_agent_phase_gate():
    assert _agent_does_work("pre", False) and not _agent_does_work("pre", True)
    assert _agent_does_work("post", True) and not _agent_does_work("post", False)
    # agent.yml on its own (no phase) always does the work
    assert _agent_does_work(None, False) and _agent_does_work(None, True)


def test_agent_phase_gate_follows_the_derived_facts_and_precedes_the_work():
    # both phases derive the facts, the targets and this host's settings (DATA_NODE's address)
    names = [t.get("name") for t in play_tasks(AGENT_PLAY)]
    gate = names.index(_gate()["name"])
    for name in ("Derive the skmon facts", "Derive the skmon agent targets", "Find this host's agent target",
                 "Derive this host's agent settings", "Derive this host's profiling settings"):
        assert names.index(name) < gate, name
    assert names.index("Restrict the agent port (and DATA_NODE's published ports) to the estate addresses") == gate + 1
    assert all(not t.get("when") for t in _play()["pre_tasks"] if t.get("name") != "Load vault file")


def test_agent_phase_is_checked():
    t = next(t for t in play_tasks(AGENT_PLAY) if t.get("name") == "Check the agent phase")
    assert "['', 'pre', 'post']" in t["assert"]["that"][0]


def test_env_plays_put_every_agent_host_in_the_group():
    for p in PLAYS:
        t = next(t for t in play_tasks(p) if "add_host" in t)
        assert t["add_host"]["groups"] == "skmon_agent_hosts" and t["loop"] == "{{ skmon_agent_host_list }}"


def test_targets_derived_in_one_shared_file():
    assert any(t.get("name") == "Derive the skmon agent scrape targets" for t in load_tasks(AGENT_TARGETS))
    for p in [*PLAYS, AGENT_PLAY]:
        assert any(str(t.get("include_tasks", "")).endswith("tasks/agent_targets.yml") for t in play_tasks(p)), p.name
