"""skmon parking (replicas 0, spec 14): everything scaled to 0, nothing destroyed, no wasted forced updates."""
import skmon_support as s


def test_parking_keeps_data_path():
    for p in s.PLAYS:
        assert not any(t.get("file", {}).get("state") == "absent" and "skmon_data_path" in str(t)
                       for t in s.play_tasks(p))


def test_parking_keeps_data_path_in_the_agent_play():
    tasks = s.play_tasks(s.AGENT_PLAY)
    assert not any(t.get("file", {}).get("state") == "absent" and "DATA_PATH" in str(t) for t in tasks)


def test_parked_render_gives_zero_everywhere():
    for value in (0, "0"):
        assert {v["deploy"]["replicas"] for v in s.services(replicas=value).values()} == {0}


def test_unparked_render_gives_one_everywhere_except_the_optional_pve_exporter():
    assert {n: v["deploy"]["replicas"] for n, v in s.services().items()} == {
        **{n: 1 for n in s.services()}, "pve-exporter": 0, "pyroscope": 0}


def test_parked_agent_runs_no_container():
    assert "docker run" not in s.render_run_script(skmon_parked=True)


def test_parked_deploy_script_does_not_force_update():
    assert "service update --force" not in s.render_deploy(replicas=0)
    assert "service update --force" in s.render_deploy()


def test_parked_deploy_script_still_deploys_so_replicas_reach_zero():
    out = s.render_deploy(replicas=0)
    assert "docker stack deploy" in out and "--prune" in out


def _work_order(play, parked):
    """Which of agents/stack do work, in playbook order, for one parking state (R80)."""
    import test_skmon_agent_play as ap
    order = []
    for kind, detail in s.play_steps(play):
        if kind == "import" and detail["file"] == "agent.yml" and ap._agent_does_work(detail["vars"]["skmon_agent_phase"], parked):
            order.append("agents")
        elif kind == "play" and "Deploy {{ app }} stack" in detail:
            order.append("stack")
    return order


def test_unparked_agents_converge_before_the_stack():
    for p in s.PLAYS:
        assert _work_order(p, parked=False) == ["agents", "stack"], p.name


def test_parking_stops_the_stack_before_the_agents():
    for p in s.PLAYS:
        assert _work_order(p, parked=True) == ["stack", "agents"], p.name
