"""skhub.env.j2 must render exactly one KEY=value per line under Ansible's
template module settings (trim_blocks=True, keep_trailing_newline=True).

A parity run against live prod found two newline-eating bugs:

* `{%- set _custom_apps ... -%}` after DEFAULT_PHONE_REGION stripped the
  newlines on both sides, so the env file read
  `DEFAULT_PHONE_REGION=USNEXTCLOUD_CUSTOM_APPS=previewgenerator recognize`:
  a wrong phone region, and NEXTCLOUD_CUSTOM_APPS never set at all.
* the inline `{% endif %}` closing the non-prod bucket suffix swallowed the
  line's newline under trim_blocks, giving
  `OBJECTSTORE_S3_BUCKET=skhubOBJECTSTORE_S3_HOST=...` in s3 and skstor mode.

Docker's env_file parser takes each line verbatim, so either bug silently
misconfigures Nextcloud with no error anywhere."""
import pathlib
import re

import jinja2
import pytest

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
SKHUB_ENV = ANSIBLE / "optional/skhub/src/config/skhub/skhub.env.j2"

BASE_VARS = {
    "CLUSTERNAME": "skstack01",
    "DOMAIN": "example.com",
    "nextcloud_admin_user": "admin",
    "nextcloud_admin_password": "x",
    "mysql_root_password": "x",
    "mysql_user": "nextcloud",
    "mysql_password": "x",
    "s3_host": "minio.example.com",
    "s3_access_key": "k",
    "s3_secret_key": "s",
}

KEY_LINE = re.compile(r"^[A-Z0-9_]+=")


def render_env(env_name, drop=(), **overrides):
    # Ansible's template module: trim_blocks=True, keep_trailing_newline=True.
    tpl_env = jinja2.Environment(
        undefined=jinja2.ChainableUndefined, trim_blocks=True, keep_trailing_newline=True
    )
    skhub = {k: v for k, v in dict(BASE_VARS, **overrides).items() if k not in drop}
    return tpl_env.from_string(SKHUB_ENV.read_text()).render(env=env_name, skhub=skhub)


def env_lines(text):
    return [l for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]


def values(text):
    out = {}
    for line in env_lines(text):
        key, _, val = line.partition("=")
        out.setdefault(key, []).append(val)
    return out


CASES = [
    (env_name, backend)
    for env_name in ("prod", "dev")
    for backend in ("local", "s3", "skstor")
]


@pytest.mark.parametrize("env_name,backend", CASES)
def test_every_line_is_one_assignment(env_name, backend):
    text = render_env(env_name, storage_backend=backend)
    for line in env_lines(text):
        assert KEY_LINE.match(line), f"not a KEY=value line: {line!r}"
        assert not re.search(r"=.*[A-Z][A-Z0-9_]{2,}=", line), f"two assignments joined: {line!r}"


@pytest.mark.parametrize("env_name,backend", CASES)
def test_phone_region_and_custom_apps_on_their_own_lines(env_name, backend):
    vals = values(render_env(env_name, storage_backend=backend, default_phone_region="DE"))
    assert vals.get("DEFAULT_PHONE_REGION") == ["DE"]
    assert vals.get("NEXTCLOUD_CUSTOM_APPS") == ["previewgenerator recognize"]


def test_custom_apps_includes_spreed_with_talk_hpb():
    vals = values(render_env("prod", enable_talk_hpb=True))
    assert vals.get("DEFAULT_PHONE_REGION") == ["US"]
    assert vals.get("NEXTCLOUD_CUSTOM_APPS") == ["previewgenerator recognize spreed"]


@pytest.mark.parametrize("env_name", ["prod", "dev"])
@pytest.mark.parametrize("backend,host", [("s3", "minio.example.com"), ("skstor", None)])
def test_objectstore_bucket_and_host_on_their_own_lines(env_name, backend, host):
    drop = ()
    if host is None:  # skstor default: the in-cluster Garage service
        drop = ("s3_host",)
        host = f"skstor-{env_name}-garage"
    vals = values(render_env(env_name, drop=drop, storage_backend=backend))
    bucket = "skhub" if env_name == "prod" else f"skhub-{env_name}"
    assert vals.get("OBJECTSTORE_S3_BUCKET") == [bucket]
    assert vals.get("OBJECTSTORE_S3_HOST") == [host]


@pytest.mark.parametrize("env_name", ["prod", "dev"])
def test_local_backend_has_no_objectstore(env_name):
    assert "OBJECTSTORE" not in render_env(env_name, storage_backend="local")
