"""Unit tests for the real-Ansible render gate's playbook rewriter and output
checks (v1/tests/render/render_playbooks.py). The gate itself runs as its own
CI job; these keep its transform honest without needing ansible-playbook."""
import pathlib
import sys

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "render"))
import render_playbooks as rp  # noqa: E402


def test_template_renders_via_ansible_lookup_in_the_tasks_own_context():
    tasks = rp.Rewriter().tasks([{
        "name": "render env",
        "template": {"src": "{{ playbook_dir }}/a.j2", "dest": "/var/data/config/{{ app }}/a.env",
                     "owner": "root", "mode": "0640", "lstrip_blocks": False},
        "loop": [1, 2], "when": "x is defined", "vars": {"extra": 1},
        "become": True, "delegate_to": "{{ item }}", "notify": "restart",
    }])
    assert len(tasks) == 1
    t = tasks[0]
    assert "template" not in t and "lookup('ansible.builtin.template', _render_src" in t["set_fact"]["_render_files"]
    assert "lstrip_blocks=False" in t["set_fact"]["_render_files"]
    assert t["vars"] == {"extra": 1, "_render_dest": "/var/data/config/{{ app }}/a.env",
                         "_render_src": "{{ playbook_dir }}/a.j2"}
    assert t["loop"] == [1, 2] and t["when"] == "x is defined"
    for key in ("become", "delegate_to", "notify"):
        assert key not in t


def test_unpack_writes_files_and_refuses_to_escape(tmp_path):
    (tmp_path / "_render_files.json").write_text('{"/var/data/a.env": "A=1\\n", "_shell/main-001.sh": "true\\n"}')
    rp.unpack(tmp_path)
    assert (tmp_path / "var/data/a.env").read_text() == "A=1\n"
    assert (tmp_path / "_shell/main-001.sh").exists()
    (tmp_path / "_render_files.json").write_text('{"/../../escape": "x"}')
    try:
        rp.unpack(tmp_path)
    except ValueError:
        pass
    else:
        raise AssertionError("unpack wrote outside the output dir")


def test_shell_is_never_run_but_its_body_is_rendered_and_its_register_stubbed():
    tasks = rp.Rewriter().tasks([{
        "name": "deploy", "shell": "cd /var/data/{{ app }} && ./deploy", "register": "deploy_output",
    }])
    modules = [rp.module_of(t) for t in tasks]
    assert "shell" not in modules and "command" not in modules
    body, stub = tasks
    assert body["vars"]["_render_body"].startswith("cd /var/data/{{ app }}")
    assert body["vars"]["_render_dest"].startswith("_shell/")
    assert stub["set_fact"]["deploy_output"]["rc"] == 0


def test_looped_register_gets_one_fake_result_per_item():
    tasks = rp.Rewriter().tasks([{
        "name": "hostnames", "command": "hostname", "register": "skfenceha_logrotate_nodes",
        "loop": "{{ groups['managers'] }}",
    }])
    acc, final = tasks[-2:]
    assert acc["loop"] == "{{ groups['managers'] }}"
    assert '"stdout": "render-node-1"' in acc["set_fact"]["_render_acc_skfenceha_logrotate_nodes"]
    assert "results" in final["set_fact"]["skfenceha_logrotate_nodes"]


def test_unknown_modules_are_dropped_and_blocks_recursed():
    tasks = rp.Rewriter().tasks([
        {"name": "pkgs", "apt": {"name": "x"}},
        {"block": [{"name": "a", "set_fact": {"x": 1}}, {"name": "b", "docker_network": {"name": "n"}}]},
    ])
    assert tasks == [{"block": [{"name": "a", "set_fact": {"x": 1}}]}]


def test_check_file_flags_single_quoted_php_newline(tmp_path):
    bad = tmp_path / "x.sh"
    bad.write_text("docker exec c php -r \"file_put_contents(\\$f, '<?php\\n\\$CONFIG = 1;');\"\n")
    assert any("single-quoted PHP" in f for f in rp.check_file(bad, "_shell/x.sh"))
    good = tmp_path / "y.sh"
    good.write_text("docker exec c php -r \"file_put_contents(\\$f, '<?php' . PHP_EOL);\"\n")
    assert rp.check_file(good, "_shell/y.sh") == []


def test_check_file_validates_compose_and_leftover_jinja(tmp_path):
    ok = tmp_path / "ok.yml"
    ok.write_text(yaml.safe_dump({"services": {"a": {"image": "x:1", "networks": ["n"]}},
                                  "networks": {"n": {"external": True}}}))
    assert rp.check_file(ok, "ok.yml") == []
    bad = tmp_path / "bad.yml"
    bad.write_text("services:\n  a:\n    image: x:1\n    networks: [missing]\n")
    assert any("not declared" in f for f in rp.check_file(bad, "bad.yml"))
    left = tmp_path / "left.env"
    left.write_text("A={{ foo }}\n")
    assert any("unrendered Jinja" in f for f in rp.check_file(left, "left.env"))


def test_unsafe_vault_values_survive_the_merge(tmp_path):
    base = tmp_path / "b.yml"
    base.write_text("svc:\n  A: 1\n")
    over = tmp_path / "o.yml"
    over.write_text("svc:\n  S: !unsafe |\n    cmd: '{{QUERY}}'\n")
    merged = rp.deep_merge(rp.load_vars(base), rp.load_vars(over))
    dumped = yaml.dump(merged, Dumper=rp.VarsDumper)
    assert "!unsafe" in dumped and "{{QUERY}}" in dumped
    assert yaml.load(dumped, Loader=rp.VarsLoader) == {"svc": {"A": 1, "S": "cmd: '{{QUERY}}'\n"}}
