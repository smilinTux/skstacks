"""skstor's compose template must render to valid YAML and point at a
pinned, still-pullable Garage image (MinIO's own images disappearing from
Docker Hub is exactly the failure check_images.sh now guards against; this
locks the specific pin down at the template level too)."""
import pathlib

import jinja2
import pytest
import yaml

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
SKSTOR = ANSIBLE / "optional/skstor/src/config/skstor/skstor.yml.j2"


def render(env_name, **skstor_overrides):
    skstor = {"CLUSTERNAME": "skstack01", "DOMAIN": "example.com"}
    skstor.update(skstor_overrides)
    tpl_env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)  # ansible's template default
    out = tpl_env.from_string(SKSTOR.read_text()).render(app="skstor", env=env_name, skstor=skstor)
    return yaml.safe_load(out)


@pytest.mark.parametrize("env_name", ["dev", "staging", "prod"])
def test_compose_is_valid_yaml_for_every_env(env_name):
    doc = render(env_name)
    svc = doc["services"]["garage"]
    assert "deploy" in svc
    assert f"skstor-{env_name}" in svc["networks"]
    assert f"cloud-public-{env_name}" in svc["networks"]


def test_image_is_pinned_by_digest_and_not_minio():
    doc = render("prod")
    image = doc["services"]["garage"]["image"]
    assert image.startswith("dxflrs/garage:")
    assert "@sha256:" in image
    assert "minio" not in image.lower()


def test_no_web_console_router_since_garage_ships_no_admin_ui():
    doc = render("prod")
    labels = doc["services"]["garage"]["deploy"]["labels"]
    assert not any("console" in label for label in labels)


def test_rpc_secret_has_no_literal_default():
    # test_no_default_secrets.py already scans the whole tree for this, but
    # a template-level assertion catches a regression here before that
    # broader scan does.
    text = SKSTOR.read_text()
    assert "rpc_secret" not in text  # the compose template itself never
    # needs the RPC secret; garage.toml.j2 is the one file that renders it,
    # and it must come from the vault with no default (asserted elsewhere).


def test_meta_and_data_are_bind_mounts_on_the_shared_filesystem():
    # Chef's requirement: skstor stores on /var/data/skstor-<env>, the same
    # shared (NFS) filesystem every other service binds onto, matching this
    # estate's production deploy -- not a node-local named Docker volume
    # that would strand data on whichever manager this replicas=1 service
    # last ran on.
    doc = render("prod")
    volumes = doc["services"]["garage"]["volumes"]
    assert "/var/data/skstor-prod/meta:/var/lib/garage/meta" in volumes
    assert "/var/data/skstor-prod/data:/var/lib/garage/data" in volumes


def test_no_named_volumes_top_level_block():
    doc = render("prod")
    assert "volumes" not in doc  # no top-level named-volume declarations
