"""/var/data is shared by every node, so a deploy must run on ONE manager.
Every v1 deploy playbook selects one via select_manager_node.yml and does its
work in a play on `selected_manager_group`; a play aimed at a whole manager
group races itself on the shared storage (skstack06 caught skreg doing it)."""
import pathlib

import yaml

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"


def test_deploy_playbooks_run_on_one_selected_manager():
    problems = []
    for p in sorted(ANSIBLE.glob("*/*/deploy_*.yml")):
        plays = yaml.safe_load(p.read_text())
        text = p.read_text()
        work = [pl for pl in plays if isinstance(pl, dict) and (pl.get("tasks") or pl.get("pre_tasks"))
                and pl.get("hosts") not in ("localhost", "all")]
        if "select_manager_node.yml" not in text:
            problems.append(f"{p.relative_to(ANSIBLE)}: does not select a manager")
        for pl in work:
            if pl.get("hosts") != "selected_manager_group":
                problems.append(f"{p.relative_to(ANSIBLE)}: play on {pl.get('hosts')!r}")
    assert not problems, "\n".join(problems)
