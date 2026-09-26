"""skhub defaults to local storage on the shared NFS filesystem
(/var/data/skhub-<env>), matching this estate's production deploy.
`skhub.storage_backend: skstor` is an opt-in shortcut that points
Nextcloud's S3 objectstore at this cluster's own in-cluster skstor Garage
service instead, and joins the skstor-<env> overlay network to reach it --
both `local` (the default) and the existing free-form `s3` mode must
render unchanged."""
import pathlib

import jinja2
import pytest
import yaml

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
SKHUB_COMPOSE = ANSIBLE / "optional/skhub/src/config/skhub/skhub.yml.j2"
SKHUB_ENV = ANSIBLE / "optional/skhub/src/config/skhub/skhub.env.j2"

BASE_VARS = {
    "CLUSTERNAME": "skstack01",
    "DOMAIN": "example.com",
    "nextcloud_admin_user": "admin",
    "nextcloud_admin_password": "x",
    "mysql_root_password": "x",
    "mysql_user": "nextcloud",
    "mysql_password": "x",
}


def _tpl_env():
    return jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)


def render_compose(env_name="prod", **skhub_overrides):
    skhub = dict(BASE_VARS, **skhub_overrides)
    out = _tpl_env().from_string(SKHUB_COMPOSE.read_text()).render(env=env_name, skhub=skhub)
    return yaml.safe_load(out)


def render_env(env_name="prod", **skhub_overrides):
    skhub = dict(BASE_VARS, **skhub_overrides)
    return _tpl_env().from_string(SKHUB_ENV.read_text()).render(env=env_name, skhub=skhub)


def test_default_compose_has_no_skstor_network():
    doc = render_compose()
    assert "skstor-prod" not in doc["services"]["nextcloud"]["networks"]
    assert "skstor-prod" not in doc["networks"]


def test_default_env_has_no_objectstore_lines():
    text = render_env()
    assert "OBJECTSTORE" not in text


def test_s3_mode_env_is_unchanged():
    text = render_env(storage_backend="s3", s3_host="minio.example.com",
                       s3_access_key="k", s3_secret_key="s")
    assert "OBJECTSTORE_S3_HOST=minio.example.com" in text
    assert "OBJECTSTORE_S3_PORT=443" in text
    assert "OBJECTSTORE_S3_SSL=true" in text
    assert "OBJECTSTORE_S3_REGION" not in text  # unchanged: s3 mode never set this


def test_s3_mode_compose_does_not_join_skstor_network():
    doc = render_compose(storage_backend="s3")
    assert "skstor-prod" not in doc["services"]["nextcloud"]["networks"]


@pytest.mark.parametrize("env_name", ["dev", "staging", "prod"])
def test_skstor_mode_compose_joins_the_in_cluster_network(env_name):
    doc = render_compose(env_name, storage_backend="skstor")
    assert f"skstor-{env_name}" in doc["services"]["nextcloud"]["networks"]
    assert f"skstor-{env_name}" in doc["networks"]
    assert doc["networks"][f"skstor-{env_name}"]["external"] is True


def test_skstor_mode_env_points_at_the_in_cluster_garage():
    text = render_env(storage_backend="skstor", s3_access_key="k", s3_secret_key="s")
    assert "OBJECTSTORE_S3_HOST=skstor-prod-garage" in text
    assert "OBJECTSTORE_S3_PORT=3900" in text
    assert "OBJECTSTORE_S3_SSL=false" in text
    assert "OBJECTSTORE_S3_USEPATH_STYLE=true" in text
    assert "OBJECTSTORE_S3_REGION=garage" in text
    assert "OBJECTSTORE_S3_KEY=k" in text
    assert "OBJECTSTORE_S3_SECRET=s" in text


def test_skstor_mode_env_host_is_overridable():
    text = render_env(storage_backend="skstor", s3_host="garage.other.example.com")
    assert "OBJECTSTORE_S3_HOST=garage.other.example.com" in text
