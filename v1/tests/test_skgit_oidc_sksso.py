"""skgit (Forgejo) sign-in through sksso (Authentik) over OIDC, as code.

sksso side: an opt-in blueprint (sksso.SKGIT_OIDC_ENABLED) renders the OAuth2
provider + application; the client secret reaches Authentik as an env var read
with !Env, never in a rendered non-secret file.

skgit side: skgit.AUTH_MODE: oidc makes the deploy create or update ONE Forgejo
OAuth2 auth source through `forgejo admin auth`, by name, idempotently, never
deleting. Unset (local) the render is unchanged.

The auth source script is exercised against a fake `docker` that emulates the
`forgejo admin auth` subcommands, so the add / update / refuse logic and the
"secret never in argv or output" rule are tested for real.
"""
import configparser
import json
import os
import pathlib
import re
import stat
import subprocess

import jinja2
import pytest
import yaml

OPT = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional"
SKSSO = OPT / "sksso"
SKGIT = OPT / "skgit"
SECRET = "skgit-oidc-client-secret-0123456789abcdef-0123"
ENVS = ("dev", "staging", "prod")


class _Loader(yaml.SafeLoader):
    pass


for _tag in ("Find", "KeyOf", "Env"):
    _Loader.add_constructor("!" + _tag, lambda loader, node: (node.tag, loader.construct_sequence(node, deep=True)
                                                              if isinstance(node, yaml.SequenceNode)
                                                              else loader.construct_scalar(node)))


def _jenv(strict=False):
    j = jinja2.Environment(undefined=jinja2.StrictUndefined if strict else jinja2.ChainableUndefined,
                           trim_blocks=True)
    j.filters["to_json"] = json.dumps
    j.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).strip().lower() in ("yes", "true", "1")
    return j


# ----------------------------------------------------------------- sksso side

def _blueprint(**over):
    ctx = dict(sksso_skgit_client_id="skgit", sksso_skgit_host="git.example.com",
               sksso_skgit_app_name="SKGit (Forgejo)", sksso_skgit_source_name_enc="SKStack%20SSO",
               sksso_skgit_authentication_flow="")
    ctx.update(over)
    text = (SKSSO / "src/config/sksso/blueprints/skgit-oidc.yaml.j2").read_text()
    return text, yaml.load(_jenv(True).from_string(text).render(**ctx), _Loader)


def _entries(bp):
    return {e["model"]: e for e in bp["entries"]}


def test_blueprint_describes_the_provider_and_application():
    text, bp = _blueprint()
    e = _entries(bp)
    prov = e["authentik_providers_oauth2.oauth2provider"]
    assert prov["identifiers"] == {"name": "skgit"}
    a = prov["attrs"]
    assert a["client_type"] == "confidential" and a["client_id"] == "skgit"
    assert a["sub_mode"] == "hashed_user_id" and a["issuer_mode"] == "per_provider"
    assert a["grant_types"] == ["authorization_code", "refresh_token"]
    app = e["authentik_core.application"]
    assert app["identifiers"] == {"slug": "skgit"}
    assert app["attrs"]["provider"][0] == "!KeyOf"
    assert app["attrs"]["meta_launch_url"] == "https://git.example.com/"
    # one Application: visibility is the portal tile's binding, not ours
    assert "authentik_policies.policybinding" not in e


def test_redirect_uri_is_strict_and_carries_the_encoded_source_name():
    _, bp = _blueprint()
    uris = _entries(bp)["authentik_providers_oauth2.oauth2provider"]["attrs"]["redirect_uris"]
    assert uris == [{"matching_mode": "strict",
                     "url": "https://git.example.com/user/oauth2/SKStack%20SSO/callback"}]


def _task(name_part, role="sksso", file="skgit_blueprint.yml"):
    tasks = yaml.safe_load((OPT / role / "tasks" / file).read_text())
    return next(t for t in tasks if name_part in t["name"])


def test_source_name_is_encoded_once_from_the_single_knob():
    facts = _task("public host and the URL-encoded")["set_fact"]
    enc = jinja2.Environment().from_string(facts["sksso_skgit_source_name_enc"])
    assert enc.render(sksso_skgit_source_name="SKStack SSO") == "SKStack%20SSO"
    assert enc.render(sksso_skgit_source_name="Team_SSO-2.0") == "Team_SSO-2.0"
    # the one knob, with the documented default
    name = _task("Resolve the skgit OIDC settings")["set_fact"]["sksso_skgit_source_name"]
    assert "sksso.SKGIT_OIDC_SOURCE_NAME" in name and "'SKStack SSO'" in name


def _assert_exprs(task):
    return [str(x) for x in task["assert"]["that"]]


@pytest.mark.parametrize("bad", ["a/b", "x%20y", "name?x", "", " lead", "a\"b", "a\nb"])
def test_source_name_with_unsafe_characters_is_refused(bad):
    expr = next(x for x in _assert_exprs(_task("Check the skgit OIDC vault keys")) if "source_name" in x)
    j = jinja2.Environment()
    j.tests["match"] = lambda value, pattern: re.match(pattern, value) is not None
    assert j.compile_expression(expr)(sksso_skgit_source_name=bad) is False
    assert j.compile_expression(expr)(sksso_skgit_source_name="SKStack SSO") is True


def test_secret_must_be_32_chars_before_anything_renders():
    exprs = _assert_exprs(_task("Check the skgit OIDC vault keys"))
    assert any("SKGIT_OIDC_CLIENT_SECRET" in x and ">= 32" in x for x in exprs)


def test_authentication_flow_unset_is_the_brand_default_and_capauth_binds():
    _, off = _blueprint()
    a = _entries(off)["authentik_providers_oauth2.oauth2provider"]["attrs"]
    assert a["authentication_flow"] is None
    _, on = _blueprint(sksso_skgit_authentication_flow="capauth-authentication")
    a = _entries(on)["authentik_providers_oauth2.oauth2provider"]["attrs"]
    assert a["authentication_flow"] == ("!Find", ["authentik_flows.flow", ["slug", "capauth-authentication"]])


def test_blueprint_never_carries_the_secret_only_an_env_reference():
    text, bp = _blueprint()
    prov = _entries(bp)["authentik_providers_oauth2.oauth2provider"]
    assert prov["attrs"]["client_secret"] == ("!Env", "SKGIT_OIDC_CLIENT_SECRET")
    assert SECRET not in text


SKSSO_VAULT = {"CLUSTERNAME": "cluster1", "DOMAIN": "example.com"}


def _compose(**sksso):
    out = _jenv().from_string((SKSSO / "src/config/sksso/sksso.yml.j2").read_text()).render(
        env="prod", app="sksso", sksso={**SKSSO_VAULT, **sksso})
    return yaml.safe_load(out)["services"]


def _env_file(**sksso):
    base = {"postgres_user": "u", "postgres_password": "p", "authentik_secret_key": "k",
            "CLUSTERNAME": "c", "DOMAIN": "example.com"}
    return _jenv().from_string((SKSSO / "src/config/sksso/sksso.env.j2").read_text()).render(
        env="prod", sksso={**base, **sksso})


def test_sksso_off_renders_nothing_about_skgit():
    for svc in ("server", "worker"):
        assert not any("/blueprints/" in v for v in _compose()[svc]["volumes"])
    assert "SKGIT" not in _env_file()
    # a secret alone, or the flag off, changes nothing either
    assert _env_file(SKGIT_OIDC_CLIENT_SECRET=SECRET) == _env_file()
    assert _env_file(SKGIT_OIDC_ENABLED=False, SKGIT_OIDC_CLIENT_SECRET=SECRET) == _env_file()
    assert _compose(SKGIT_OIDC_ENABLED=False, SKGIT_OIDC_CLIENT_SECRET=SECRET) == _compose()


def test_sksso_on_mounts_the_blueprints_and_writes_the_secret_to_the_env_file_only():
    for svc in ("server", "worker"):
        vols = _compose(SKGIT_OIDC_ENABLED=True, SKGIT_OIDC_CLIENT_SECRET=SECRET)[svc]["volumes"]
        assert "/var/data/config/sksso-prod/blueprints:/blueprints/custom:ro" in vols
    on = _env_file(SKGIT_OIDC_ENABLED=True, SKGIT_OIDC_CLIENT_SECRET=SECRET)
    assert f"\nSKGIT_OIDC_CLIENT_SECRET={SECRET}\n" in on
    # the compose file (0644) never holds it
    out = _jenv().from_string((SKSSO / "src/config/sksso/sksso.yml.j2").read_text()).render(
        env="prod", app="sksso", sksso={**SKSSO_VAULT, "SKGIT_OIDC_ENABLED": True, "SKGIT_OIDC_CLIENT_SECRET": SECRET})
    assert SECRET not in out


@pytest.mark.parametrize("env", ENVS)
def test_every_sksso_playbook_includes_the_blueprint_task_only_when_enabled(env):
    doc = yaml.safe_load((SKSSO / f"deploy_sksso-{env}.yml").read_text())
    tasks = [t for play in doc if isinstance(play, dict) for t in play.get("tasks", [])]
    inc = next(t for t in tasks if "skgit (Forgejo) OAuth2 blueprint" in t.get("name", ""))
    assert inc["include_tasks"]["file"] == "tasks/skgit_blueprint.yml"
    assert inc["when"] == "sksso.SKGIT_OIDC_ENABLED | default(false) | bool"
    assert set(inc["tags"]) == {"templates", "deploy"}


def test_the_rendered_blueprint_file_is_not_secret_bearing():
    # the template task writes a 0644 file: it must not interpolate the secret
    tasks = yaml.safe_load((SKSSO / "tasks/skgit_blueprint.yml").read_text())
    tpl = next(t for t in tasks if "template" in t)["template"]
    assert tpl["mode"] == "0644"
    src = (SKSSO / "src/config/sksso/blueprints/skgit-oidc.yaml.j2").read_text()
    assert "sksso.SKGIT_OIDC_CLIENT_SECRET" not in re.sub(r"(?m)^\s*#.*$", "", src)


# ----------------------------------------------------------------- skgit side

SKGIT_VAULT = {"CLUSTERNAME": "skstack01", "DOMAIN": "example.com", "POSTGRES_PASSWORD": "pw",
               "SECRET_KEY": "secret", "INTERNAL_TOKEN": "internal"}


def _app_ini(**extra):
    return _jenv().from_string((SKGIT / "src/config/skgit/app.ini.j2").read_text()).render(
        app="skgit", env="prod", skgit={**SKGIT_VAULT, **extra})


def _parse(text):
    cfg = configparser.RawConfigParser(strict=True, interpolation=None)
    cfg.optionxform = str
    cfg.read_string("[__root__]\n" + text)
    return cfg


def test_local_mode_renders_exactly_as_unset():
    base = _app_ini()
    assert "oauth2_client" not in base
    assert _app_ini(AUTH_MODE="local") == base
    assert _app_ini(AUTH_MODE="") == base
    # the oidc-only knobs do nothing while the mode is local
    assert _app_ini(OIDC_AUTO_REGISTRATION=True, OIDC_ACCOUNT_LINKING_BY_EMAIL=True) == base


def test_oidc_mode_is_conservative_by_default():
    cfg = _parse(_app_ini(AUTH_MODE="oidc"))
    assert cfg.get("oauth2_client", "ENABLE_AUTO_REGISTRATION") == "false"
    assert cfg.get("oauth2_client", "ACCOUNT_LINKING") == "login"
    assert not cfg.has_option("oauth2_client", "USERNAME")


def test_oidc_knobs_turn_on_auto_registration_and_email_linking():
    cfg = _parse(_app_ini(AUTH_MODE="OIDC", OIDC_AUTO_REGISTRATION=True, OIDC_ACCOUNT_LINKING_BY_EMAIL=True))
    assert cfg.get("oauth2_client", "ENABLE_AUTO_REGISTRATION") == "true"
    assert cfg.get("oauth2_client", "ACCOUNT_LINKING") == "auto"
    assert cfg.get("oauth2_client", "USERNAME") == "preferred_username"
    cfg = _parse(_app_ini(AUTH_MODE="oidc", OIDC_AUTO_REGISTRATION=True, OIDC_USERNAME_CLAIM="email"))
    assert cfg.get("oauth2_client", "USERNAME") == "email"


def test_the_client_secret_is_never_rendered_into_app_ini():
    assert SECRET not in _app_ini(AUTH_MODE="oidc", OIDC_CLIENT_SECRET=SECRET)


def _skgit_play(env):
    doc = yaml.safe_load((SKGIT / f"deploy_skgit-{env}.yml").read_text())
    return next(p for p in doc if isinstance(p, dict) and p.get("hosts") == "selected_manager_group")


def _norm(task, env):
    return json.loads(json.dumps(task).replace(f"-{env}", "-ENV").replace(f": {env}", ": ENV"))


@pytest.mark.parametrize("env", ENVS)
def test_playbook_resolves_validates_and_runs_the_auth_source_task(env):
    play = _skgit_play(env)
    pre = {t["name"]: t for t in play["pre_tasks"]}
    mode = pre["Resolve the sign-in mode (skgit.AUTH_MODE, local by default or oidc)"]["set_fact"]
    assert "'local', true" in mode["skgit_auth_mode"]
    assert "'SKStack SSO'" in mode["skgit_oidc_source_name"]
    assert "AUTHENTIK_TILE_SLUG" in mode["skgit_oidc_client_id"]
    disc = pre["Resolve the OIDC discovery URL"]["set_fact"]["skgit_oidc_discovery_url"]
    assert "/application/o/' ~ skgit_oidc_client_id ~ '/.well-known/openid-configuration" in disc
    assert pre["Refuse an unknown AUTH_MODE"]["assert"]["that"] == ["skgit_auth_mode in ['local', 'oidc']"]
    guard = pre["Refuse AUTH_MODE oidc without a usable client secret, source name and issuer"]
    assert guard["when"] == "skgit_auth_mode == 'oidc'"
    assert any("OIDC_CLIENT_SECRET" in x and ">= 32" in x for x in guard["assert"]["that"])

    task = next(t for t in play["tasks"] if t["name"].startswith("Create or update the Forgejo OIDC auth source"))
    assert task["script"].endswith("/src/{{ app }}/oidc-auth-source.sh")
    assert task["no_log"] is True
    assert task["delegate_to"] == "{{ forgejo_node.stdout | trim }}"
    assert "skgit_auth_mode == 'oidc'" in task["when"]
    # the secret is only ever in this task's environment
    env_block = task["environment"]
    assert env_block["OIDC_CLIENT_SECRET"] == "{{ skgit.OIDC_CLIENT_SECRET }}"
    flat = yaml.safe_dump(play)
    assert flat.count("skgit.OIDC_CLIENT_SECRET") == 2  # the guard and this environment
    # the task needs the node and the API wait, which run for oidc too
    wait = next(t for t in play["tasks"] if t["name"].startswith("Wait for the Forgejo API"))
    assert "skgit_auth_mode == 'oidc'" in " ".join(wait["when"])
    assert "oidc" in wait["tags"]


def test_the_three_envs_carry_identical_oidc_tasks():
    def pick(env):
        play = _skgit_play(env)
        names = ("Resolve the sign-in mode", "Resolve the OIDC discovery", "Refuse an unknown AUTH_MODE",
                 "Refuse AUTH_MODE oidc", "Create or update the Forgejo OIDC", "Fail if the Forgejo OIDC")
        return [_norm(t, env) for t in play["pre_tasks"] + play["tasks"] if t["name"].startswith(names)]

    ref = pick("prod")
    assert len(ref) == 6
    assert pick("dev") == ref and pick("staging") == ref


def test_deleting_an_auth_source_is_never_attempted():
    text = (SKGIT / "src/skgit/oidc-auth-source.sh").read_text()
    code = re.sub(r"(?m)^\s*#.*$", "", text)
    assert "delete" not in code and " rm " not in code and "remove" not in code


# ----------------------------------------------- the auth source script (fake docker)

FAKE_DOCKER = r'''#!/bin/bash
# Fake docker: emulates `docker ps` and `docker exec <cid> forgejo admin auth ...`.
# State file: tab separated rows "id name type enabled". Every call is logged.
STATE="$FAKE_DIR/state"
LOG="$FAKE_DIR/calls.log"
echo "docker $*" >> "$LOG"
case "$1" in
  ps) echo cid123; exit 0 ;;
  exec) shift ;;
  *) exit 2 ;;
esac
while [ "$1" = "-e" ]; do
  echo "env-pass $2" >> "$LOG"
  shift 2
done
shift  # container id
if [ "$1" = forgejo ]; then
  # forgejo admin auth list
  [ -n "${FAKE_LIST_FAIL:-}" ] && { echo "list exploded"; exit 1; }
  if [ "${FAKE_LIST_SPACES:-}" = 1 ]; then
    printf 'ID  Name         Type    Enabled\n'
    while IFS=$'\t' read -r id name type en; do printf '%-3s %-12s %-7s %s\n' "$id" "$name" "$type" "$en"; done < "$STATE"
  else
    printf 'ID\tName\tType\tEnabled\n'
    cat "$STATE"
  fi
  exit 0
fi
# sh -c SCRIPT sh <subcommand> args...  (the secret comes from the environment)
shift 3   # sh -c SCRIPT
shift     # sh
sub=$1; shift
echo "forgejo-auth $sub $*" >> "$LOG"
echo "secret-seen-in-container-env=${OIDC_CLIENT_SECRET:+yes}" >> "$LOG"
if [ -n "${FAKE_FAIL:-}" ]; then echo "boom: client secret ${OIDC_CLIENT_SECRET} rejected"; exit 1; fi
name=; id=
while [ $# -gt 0 ]; do
  case "$1" in --name) name=$2; shift 2 ;; --id) id=$2; shift 2 ;; *) shift ;; esac
done
case "$sub" in
  add-oauth)
    next=$(awk -F'\t' 'BEGIN{m=0} $1>m{m=$1} END{print m+1}' "$STATE")
    printf '%s\t%s\tOAuth2\ttrue\n' "$next" "$name" >> "$STATE"
    echo "Authentication source added" ;;
  update-oauth)
    grep -q "^$id	" "$STATE" || { echo "no such source"; exit 1; }
    echo "Authentication source updated" ;;
  *) echo "unexpected $sub"; exit 3 ;;
esac
'''


@pytest.fixture
def fake(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    d = bindir / "docker"
    d.write_text(FAKE_DOCKER)
    d.chmod(d.stat().st_mode | stat.S_IEXEC)
    work = tmp_path / "work"
    work.mkdir()
    (work / "state").write_text("")

    def run(rows=(), name="SKStack SSO", extra_env=None):
        (work / "state").write_text("".join("\t".join(r) + "\n" for r in rows))
        (work / "calls.log").write_text("")
        env = {"PATH": f"{bindir}:{os.environ['PATH']}", "FAKE_DIR": str(work),
               "FORGEJO_SERVICE": "skgit-prod_forgejo", "SOURCE_NAME": name, "CLIENT_ID": "skgit",
               "OIDC_CLIENT_SECRET": SECRET,
               "DISCOVERY_URL": "https://sso.example.com/application/o/skgit/.well-known/openid-configuration"}
        env.update(extra_env or {})
        p = subprocess.run(["bash", str(SKGIT / "src/skgit/oidc-auth-source.sh")], env=env, capture_output=True,
                           text=True, timeout=60)
        calls = (work / "calls.log").read_text()
        state = [l.split("\t") for l in (work / "state").read_text().splitlines()]
        return p, calls, state

    return run


def _auth_calls(calls):
    return [l.split(" ", 1)[1] for l in calls.splitlines() if l.startswith("forgejo-auth ")]


@pytest.mark.parametrize("spaces", ["", "1"])
def test_absent_source_is_created_once_then_updated_in_place(fake, spaces):
    env = {"FAKE_LIST_SPACES": spaces}
    p, calls, state = fake(rows=[], extra_env=env)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "OIDC_SOURCE_CREATED" in p.stdout
    assert [c.split()[0] for c in _auth_calls(calls)] == ["add-oauth"]
    add = _auth_calls(calls)[0]
    for part in ("--name SKStack SSO", "--provider openidConnect", "--key skgit",
                 "--auto-discover-url https://sso.example.com/application/o/skgit/.well-known/openid-configuration",
                 "--scopes openid", "--scopes email", "--scopes profile"):
        assert part in add
    assert state == [["1", "SKStack SSO", "OAuth2", "true"]]

    # second deploy: the source exists, so it is updated by id, not added again
    p, calls, state2 = fake(rows=state, extra_env=env)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "OIDC_SOURCE_UPDATED id=1" in p.stdout
    assert [c.split()[0] for c in _auth_calls(calls)] == ["update-oauth"]
    assert "--id 1" in _auth_calls(calls)[0]
    assert state2 == state


def test_other_sources_are_never_touched_and_a_longer_name_is_not_a_match(fake):
    rows = [["1", "Corp LDAP", "LDAP", "true"], ["2", "SKStack SSO 2", "OAuth2", "true"]]
    p, calls, state = fake(rows=rows)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "OIDC_SOURCE_CREATED" in p.stdout
    assert [c.split()[0] for c in _auth_calls(calls)] == ["add-oauth"]
    assert state[:2] == rows and state[2][1] == "SKStack SSO"
    # and an existing one with the exact name among several is the one updated
    p, calls, state = fake(rows=rows + [["7", "SKStack SSO", "OAuth2", "true"]])
    assert "OIDC_SOURCE_UPDATED id=7" in p.stdout
    assert "--id 7" in _auth_calls(calls)[0]
    assert not any(" 1 " in c or "--id 2" in c for c in _auth_calls(calls))


def test_a_same_named_source_of_another_type_is_refused_not_overwritten(fake):
    p, calls, state = fake(rows=[["3", "SKStack SSO", "LDAP", "true"]])
    assert p.returncode == 1
    assert "LDAP" in p.stdout and "refusing" in p.stdout
    assert _auth_calls(calls) == []
    assert state == [["3", "SKStack SSO", "LDAP", "true"]]


def test_the_secret_is_never_in_argv_or_output_and_a_failure_is_redacted(fake):
    p, calls, _ = fake(rows=[])
    assert SECRET not in calls and SECRET not in p.stdout + p.stderr
    # handed to the container by name only
    assert "env-pass OIDC_CLIENT_SECRET" in calls
    assert "secret-seen-in-container-env=yes" in calls
    p, calls, _ = fake(rows=[], extra_env={"FAKE_FAIL": "1"})
    assert p.returncode == 1
    assert "add-oauth failed" in p.stdout and "<redacted>" in p.stdout
    assert SECRET not in p.stdout + p.stderr + calls
    p, calls, _ = fake(rows=[["1", "SKStack SSO", "OAuth2", "true"]], extra_env={"FAKE_FAIL": "1"})
    assert p.returncode == 1 and "update-oauth failed" in p.stdout
    assert SECRET not in p.stdout + p.stderr + calls


def test_it_stops_when_the_list_fails_or_an_input_is_missing(fake):
    p, calls, _ = fake(rows=[], extra_env={"FAKE_LIST_FAIL": "1"})
    assert p.returncode == 1 and "auth list failed" in p.stdout and _auth_calls(calls) == []
    p, calls, _ = fake(rows=[], extra_env={"DISCOVERY_URL": ""})
    assert p.returncode == 1 and "DISCOVERY_URL is empty" in p.stdout and calls == ""


def test_no_call_ever_deletes_or_removes(fake):
    for rows in ([], [["1", "SKStack SSO", "OAuth2", "true"]], [["1", "Other", "OAuth2", "true"]]):
        _, calls, _ = fake(rows=rows)
        assert not re.search(r"delete|remove", calls, re.I)
