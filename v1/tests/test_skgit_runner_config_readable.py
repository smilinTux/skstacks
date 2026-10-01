"""skgit's runner-config.yml must stay readable by the Actions runner.

The runner compose project bind-mounts ``runner-config.yml`` read-only at
``/data/config.yaml`` in the forgejo-runner container, which runs as a
non-root user. The playbook rendered it with the env-file loop's ``0640``
root:root (the secret-file-mode change), so after a deploy the runner could
not open its config (``open /data/config.yaml: permission denied``), restart
looped and every Actions job queued. The file carries no secret (capacity,
timeouts, labels, the DinD address), so it is rendered ``0644``; the env
files in the same loop keep ``0640``.
"""
import pathlib

import jinja2
import yaml

SKGIT = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skgit"
PLAYBOOKS = [SKGIT / f"deploy_skgit-{env}.yml" for env in ("dev", "staging", "prod")]
RUNNER_CONFIG_TPL = SKGIT / "src/config/skgit/runner-config.yml.j2"


def _template_modes(playbook):
    """Map each templated dest basename to its effective mode string."""
    modes = {}
    for play in yaml.safe_load(playbook.read_text()):
        for task in play.get("tasks", []) or []:
            tpl = task.get("template")
            if not tpl or "loop" not in task:
                continue
            for item in task["loop"]:
                ctx = {"item": item, "app": "skgit", "env": "prod", "playbook_dir": "."}
                env = jinja2.Environment()
                dest = env.from_string(tpl["dest"]).render(**ctx)
                dest = env.from_string(dest).render(**ctx)  # item values hold {{ app }} too
                mode = env.from_string(str(tpl.get("mode"))).render(**ctx)
                modes[dest.rsplit("/", 1)[-1]] = mode
    return modes


def test_runner_config_is_world_readable():
    for pb in PLAYBOOKS:
        mode = int(_template_modes(pb)["runner-config.yml"], 8)
        assert mode & 0o004, f"{pb.name}: runner-config.yml mode {oct(mode)} not readable by the runner"


def test_env_files_in_the_same_loop_stay_private():
    for pb in PLAYBOOKS:
        modes = _template_modes(pb)
        for name in ("skgit.env", "postgres.env", "postgres-backup.env", ".env", "skgit-runners.yml"):
            assert int(modes[name], 8) & 0o007 == 0, f"{pb.name}: {name} is {modes[name]}"


def test_runner_config_template_carries_no_secret():
    text = RUNNER_CONFIG_TPL.read_text().upper()
    for marker in ("TOKEN", "PASSWORD", "SECRET", "_KEY"):
        assert marker not in text, marker
