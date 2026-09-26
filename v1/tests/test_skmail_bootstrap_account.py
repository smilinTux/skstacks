"""docker-mailserver 16 refuses to start with no mail account ("You need at
least one mail account to start Dovecot ... Shutting down", exit 1, restart
loop). On a fresh skmail instance nothing created one: the only tool,
add_user, needs a running container, and it ran `docker exec
skmail-<env>_skmail`, which is the Swarm SERVICE name, not a container name,
so it failed even when the service was up.

The deploy playbooks now bootstrap a first account when
skmail.BOOTSTRAP_ACCOUNT and skmail.BOOTSTRAP_PASSWORD are both set and the
DMS account database (config dir, mounted at /tmp/docker-mailserver) does
not exist yet. The password is hashed on the target with `openssl passwd -6
-stdin` (stdin, never argv) and every task that sees it is no_log. An existing
postfix-accounts.cf is never touched. add_user now resolves the task's
container (`name=skmail-<env>_skmail.`) and feeds the password over stdin.
DMS reads postfix-accounts.cf as root at startup and writes its own dovecot
userdb from it, so root:root 0640 is enough."""
import pathlib

import jinja2
import pytest
import yaml

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
SKMAIL = ANSIBLE / "optional/skmail"
ADD_USER = SKMAIL / "src/skmail/add_user.j2"
ENVS = ("dev", "staging", "prod")
ACCOUNTS_DB = "/var/data/{{ app }}-{{ env }}/docker-data/dms/config/postfix-accounts.cf"
SECRET_MARKERS = ("BOOTSTRAP_PASSWORD", "skmail_bootstrap_hash")


def _tasks(env_name):
    plays = yaml.safe_load((SKMAIL / f"deploy_skmail-{env_name}.yml").read_text())
    for play in plays:
        yield from play.get("tasks", []) or []


def _leaves(tasks, inherited_when=()):
    for task in tasks:
        when = task.get("when", [])
        when = [when] if isinstance(when, str) else list(when)
        if "block" in task:
            yield from _leaves(task["block"], tuple(inherited_when) + tuple(when))
        else:
            yield task, list(inherited_when) + when


def _bootstrap_block(env_name):
    blocks = [t for t in _tasks(env_name) if "block" in t and "BOOTSTRAP_ACCOUNT" in yaml.safe_dump(t)]
    assert len(blocks) == 1, f"{env_name}: expected one bootstrap block, found {len(blocks)}"
    return blocks[0]


@pytest.mark.parametrize("env_name", ENVS)
def test_bootstrap_guarded_by_both_vars(env_name):
    block = _bootstrap_block(env_name)
    when = " ".join(block["when"] if isinstance(block["when"], list) else [block["when"]])
    assert "skmail.BOOTSTRAP_ACCOUNT" in when
    assert "skmail.BOOTSTRAP_PASSWORD" in when


@pytest.mark.parametrize("env_name", ENVS)
def test_bootstrap_guarded_by_accounts_db_absence(env_name):
    leaves = [t for t, _ in _leaves(_bootstrap_block(env_name)["block"])]
    stats = [t for t in leaves if "stat" in t]
    assert stats and stats[0]["stat"]["path"] == ACCOUNTS_DB
    reg = stats[0]["register"]
    for task in leaves:
        if task is stats[0]:
            continue
        when = task.get("when", [])
        when = " ".join([when] if isinstance(when, str) else when)
        assert f"{reg}.stat.exists" in when and "not" in when, task.get("name")


@pytest.mark.parametrize("env_name", ENVS)
def test_bootstrap_hash_uses_openssl_stdin(env_name):
    leaves = [t for t, _ in _leaves(_bootstrap_block(env_name)["block"])]
    hashers = [t for t in leaves if "openssl passwd -6 -stdin" in str(t.get("command", t.get("shell", "")))]
    assert len(hashers) == 1
    task = hashers[0]
    assert "BOOTSTRAP_PASSWORD" not in str(task.get("command", task.get("shell")))
    assert "skmail.BOOTSTRAP_PASSWORD" in task["args"]["stdin"]
    assert task["register"] == "skmail_bootstrap_hash"


@pytest.mark.parametrize("env_name", ENVS)
def test_every_task_touching_password_is_no_log(env_name):
    touched = 0
    for task, _ in _leaves(_tasks(env_name)):
        body = {k: v for k, v in task.items() if k != "when"}
        if any(m in yaml.safe_dump(body) for m in SECRET_MARKERS):
            touched += 1
            assert task.get("no_log") is True, task.get("name")
    assert touched >= 2


@pytest.mark.parametrize("env_name", ENVS)
def test_bootstrap_writes_accounts_db_0640_without_overwrite(env_name):
    leaves = [t for t, _ in _leaves(_bootstrap_block(env_name)["block"])]
    writers = [t for t in leaves if "copy" in t]
    assert len(writers) == 1
    copy = writers[0]["copy"]
    assert copy["dest"] == ACCOUNTS_DB
    assert str(copy["mode"]) == "0640"
    assert copy["force"] is False
    assert copy["owner"] == "root"
    # Ansible keeps a templated value's trailing newline; plain Jinja drops it.
    line = jinja2.Environment(keep_trailing_newline=True).from_string(copy["content"]).render(
        skmail={"BOOTSTRAP_ACCOUNT": "postmaster@example.test"},
        skmail_bootstrap_hash={"stdout": "$6$fakesalt$fakehash"})
    assert line == "postmaster@example.test|{SHA512-CRYPT}$6$fakesalt$fakehash\n"


@pytest.mark.parametrize("env_name", ENVS)
def test_bootstrap_runs_before_stack_deploy(env_name):
    names = [t.get("name", "") for t in _tasks(env_name)]
    boot = names.index(_bootstrap_block(env_name)["name"])
    deploy = names.index("Deploy {{ app }} stack")
    assert boot < deploy


@pytest.mark.parametrize("env_name", ENVS)
def test_add_user_execs_into_task_container_not_service(env_name):
    out = jinja2.Environment().from_string(ADD_USER.read_text()).render(env=env_name)
    service = f"skmail-{env_name}_skmail"
    assert f"name={service}." in out or ("name=${SERVICE_NAME}." in out and f"SERVICE_NAME={service}" in out)
    assert f"docker exec {service} " not in out
    assert "docker exec -i" in out
    assert 'setup email add "${EMAIL}"' in out
    assert 'setup email add "${EMAIL}" "${PASSWORD}"' not in out
