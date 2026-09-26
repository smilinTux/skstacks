"""Deploy playbooks must not pip-install into the system Python: Ubuntu 23.04+
and Debian 12+ mark it externally managed (PEP 668), so `pip: name=docker`
fails ("externally-managed-environment"). Use the distro package
(python3-docker) or a virtualenv. Found by the skstack06 v2.18.0 skstor stage."""
import pathlib

import yaml

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"


def _tasks(items):
    for t in items or []:
        yield t
        for key in ("block", "rescue", "always"):
            yield from _tasks(t.get(key))


def test_no_system_pip_installs():
    hits = []
    for pb in ANSIBLE.glob("*/*/deploy_*.yml"):
        for play in yaml.safe_load(pb.read_text()) or []:
            for key in ("pre_tasks", "tasks", "post_tasks", "handlers"):
                for t in _tasks(play.get(key)):
                    mod = t.get("pip") or t.get("ansible.builtin.pip")
                    if isinstance(mod, dict) and not mod.get("virtualenv"):
                        hits.append(f"{pb.relative_to(ANSIBLE)}: {t.get('name')}")
    assert not hits, "\n".join(hits)
