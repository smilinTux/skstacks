"""skpdf secrets must not live in Stirling-PDF's settings.yml.

The Stirling-PDF image (frooodle/s-pdf 2.14.3, scripts/init-without-ocr.sh
"Permissions" block) runs `chown -R stirlingpdfuser` + `chmod -R 755` over
/configs on EVERY container start. /configs is the bind mount of
/var/data/skpdf-<env>/extraConfigs, so the playbook's 0600 on settings.yml
never survives a start, and whatever settings.yml holds is readable by every
account on the shared /var/data.

Stirling binds its settings with Spring Boot relaxed binding and loads
settings.yml as `spring.config.additional-location`, so an OS environment
variable wins over the file (upstream settings.yml.template: "If you want to
override with environment parameter follow parameter naming
SECURITY_INITIALLOGIN_USERNAME"). So the secrets go to skpdf.env (the stack's
env_file, 0600 root:root under /var/data/config, which the image never
touches) and settings.yml carries none of them:

  security.initialLogin.password  -> SECURITY_INITIALLOGIN_PASSWORD
  security.oauth2.clientSecret    -> SECURITY_OAUTH2_CLIENTSECRET
  AutomaticallyGenerated.key      -> AUTOMATICALLYGENERATED_KEY

Stirling also keeps state of its own in /configs that has no env override
(the H2 user database, and AutomaticallyGenerated.key when the vault does not
set one: Stirling generates it and writes it back into settings.yml). The
image chmods those 755 too, so the host directory above the mount,
/var/data/skpdf-<env>, is kept 0750 root:root: the container never sees it
(bind mounts are resolved by dockerd as root), other host accounts cannot
traverse into extraConfigs.
"""
import pathlib
import re

import jinja2
import pytest
import yaml

SKPDF = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skpdf"
CONF = SKPDF / "src/config/skpdf"
PLAYBOOKS = sorted(SKPDF.glob("deploy_skpdf-*.yml"))

# Sentinels, not credentials: built at runtime so no scanner reads a literal.
ADMIN_SENTINEL = "-".join(["sentinel", "skpdf", "initial", "login"])
OAUTH_SENTINEL = "-".join(["sentinel", "skpdf", "oauth", "client"])
KEY_SENTINEL = "00000000-0000-4000-8000-000000000001"  # a (fake, low-entropy) UUID, as Stirling requires

ENV_NAMES = {
    ADMIN_SENTINEL: "SECURITY_INITIALLOGIN_PASSWORD",
    OAUTH_SENTINEL: "SECURITY_OAUTH2_CLIENTSECRET",
    KEY_SENTINEL: "AUTOMATICALLYGENERATED_KEY",
}


def render(name, **extra):
    skpdf = {
        "CLUSTERNAME": "render",
        "DOMAIN": "example.test",
        "SECURITY_ENABLELOGIN": "true",
        "SECURITY_INITIAL_USERNAME": "admin",
        "SECURITY_INITIAL_PASSWORD": ADMIN_SENTINEL,
        "OAUTH2_ENABLED": True,
        "OAUTH2_ISSUER": "https://sso.example.test/realms/x",
        "OAUTH2_CLIENT_ID": "skpdf",
        "OAUTH2_CLIENT_SECRET": OAUTH_SENTINEL,
        "AUTO_KEY": KEY_SENTINEL,
    }
    skpdf.update(extra)
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    return env.from_string((CONF / name).read_text()).render(skpdf=skpdf, env="dev")


def _dig(doc, *path):
    for key in path:
        if not isinstance(doc, dict) or key not in doc:
            return None
        doc = doc[key]
    return doc


def test_settings_yml_carries_no_secret_value():
    out = render("settings.yml.j2")
    for sentinel, env_name in ENV_NAMES.items():
        assert sentinel not in out, f"settings.yml renders the value that belongs in {env_name}"


def test_settings_yml_has_no_secret_keys_at_all():
    doc = yaml.safe_load(render("settings.yml.j2"))
    assert _dig(doc, "security", "initialLogin", "password") is None
    assert _dig(doc, "security", "oauth2", "clientSecret") is None
    assert _dig(doc, "AutomaticallyGenerated", "key") is None
    # the non-secret settings are still there
    assert _dig(doc, "security", "enableLogin") is True
    assert _dig(doc, "security", "oauth2", "clientId") == "skpdf"


def _env(text):
    return dict(line.split("=", 1) for line in text.splitlines() if line and not line.startswith("#") and "=" in line)


def test_env_file_carries_each_secret_under_stirlings_env_name():
    env = _env(render("skpdf.env.j2"))
    for sentinel, env_name in ENV_NAMES.items():
        assert env.get(env_name) == sentinel, f"{env_name} missing or wrong in skpdf.env"


def test_env_file_omits_the_key_when_the_vault_sets_none():
    """No AUTO_KEY: Stirling generates one itself. A non-UUID default would be
    discarded by Stirling (InitialSetup.initSecretKey) anyway."""
    env = _env(render("skpdf.env.j2", AUTO_KEY=None))
    assert "AUTOMATICALLYGENERATED_KEY" not in env
    assert "None" not in env.values()


def test_env_file_omits_login_and_oauth_secrets_when_off():
    env = _env(render("skpdf.env.j2", SECURITY_ENABLELOGIN="false", OAUTH2_ENABLED=False))
    assert "SECURITY_INITIALLOGIN_PASSWORD" not in env
    assert "SECURITY_OAUTH2_CLIENTSECRET" not in env


def _tasks(pb):
    out = []
    for play in yaml.safe_load(pb.read_text()):
        for section in ("pre_tasks", "tasks", "post_tasks"):
            out.extend(play.get(section) or [])
    return out


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_env_file_is_0600(pb):
    modes = [
        str(t["template"].get("mode"))
        for t in _tasks(pb)
        if isinstance(t.get("template"), dict)
        and any(str(i.get("src", "")) in ("{{ app }}.env.j2", "skpdf.env.j2") for i in t.get("loop", []) if isinstance(i, dict))
    ]
    assert modes == ["0600"], f"{pb.name}: skpdf.env template modes {modes}"


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_data_root_above_the_configs_mount_is_not_traversable_by_others(pb):
    """extraConfigs itself is re-moded 755 by the image; its parent is not."""
    hits = []
    for t in _tasks(pb):
        spec = t.get("file")
        if not isinstance(spec, dict) or spec.get("state") != "directory":
            continue
        paths = [spec.get("path")] if "loop" not in t else [i for i in t["loop"] if isinstance(i, str)]
        if "/var/data/{{ app }}-{{ env }}" in paths:
            hits.append(str(spec.get("mode")))
    assert hits, f"{pb.name}: no task creates /var/data/{{{{ app }}}}-{{{{ env }}}}"
    for mode in hits:
        assert re.fullmatch(r"0?7[0-5]0", mode), f"{pb.name}: /var/data/skpdf-<env> mode {mode} lets others traverse"


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_playbook_rejects_a_non_uuid_auto_key(pb):
    """Stirling silently replaces a non-UUID key with a random one and writes
    THAT into settings.yml (world-readable). Fail the deploy instead."""
    text = pb.read_text()
    assert re.search(r"AUTO_KEY.*is\s+match|AUTO_KEY.*regex", text), f"{pb.name}: no UUID check on skpdf.AUTO_KEY"
