"""skbackup's compose template must render to valid YAML, mount /var/data
read-only into the container by default, and never leave its Traefik
router without TLS -- the cert resolver is only attached when this
instance has ACME enabled, mirroring the `tls: {}` vs `tls: {certResolver:
main}` house idiom (skfence/skfenceha's routers.yml.j2), translated to the
docker-label provider every other optional service uses."""
import pathlib

import jinja2
import pytest
import yaml

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
SKBACKUP = ANSIBLE / "optional/skbackup/src/config/skbackup/skbackup.yml.j2"


def render(env_name, **skbackup_overrides):
    skbackup = {"CLUSTERNAME": "skstack01", "DOMAIN": "example.com"}
    skbackup.update(skbackup_overrides)
    tpl_env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)  # ansible's template default
    out = tpl_env.from_string(SKBACKUP.read_text()).render(app="skbackup", env=env_name, skbackup=skbackup)
    return yaml.safe_load(out)


@pytest.mark.parametrize("env_name", ["dev", "staging", "prod"])
def test_compose_is_valid_yaml_for_every_env(env_name):
    doc = render(env_name)
    svc = doc["services"]["duplicati"]
    assert "deploy" in svc
    assert f"skbackup-{env_name}" in svc["networks"]
    assert f"cloud-public-{env_name}" in svc["networks"]


def test_image_is_pinned_by_tag_and_digest_and_not_latest():
    doc = render("prod")
    image = doc["services"]["duplicati"]["image"]
    assert image.startswith("lscr.io/linuxserver/duplicati:")
    assert "@sha256:" in image
    assert ":latest" not in image


def test_source_is_bind_mounted_read_only_by_default():
    doc = render("prod")
    volumes = doc["services"]["duplicati"]["volumes"]
    assert "/var/data:/source:ro" in volumes


def test_source_read_only_can_be_disabled():
    doc = render("prod", source_read_only=False)
    volumes = doc["services"]["duplicati"]["volumes"]
    assert "/var/data:/source:rw" in volumes


def test_source_path_is_instance_configurable():
    doc = render("prod", source_path="/mnt/estate-data")
    volumes = doc["services"]["duplicati"]["volumes"]
    assert "/mnt/estate-data:/source:ro" in volumes


def test_config_and_backups_are_bind_mounts_on_the_shared_filesystem():
    # Same requirement as skstor: /var/data, not a node-local named Docker
    # volume that would strand data on whichever manager this replicas=1
    # service last ran on after a Swarm reschedule.
    doc = render("prod")
    volumes = doc["services"]["duplicati"]["volumes"]
    assert "/var/data/skbackup-prod/config:/config" in volumes
    assert "/var/data/skbackup-prod/backups:/backups" in volumes
    assert "volumes" not in doc  # no top-level named-volume declarations


def test_exclude_filter_is_mounted_into_config():
    doc = render("prod")
    volumes = doc["services"]["duplicati"]["volumes"]
    assert "/var/data/config/skbackup-prod/exclude.filter:/config/exclude.filter:ro" in volumes


def test_single_replica():
    doc = render("prod")
    assert doc["services"]["duplicati"]["deploy"]["replicas"] == 1
    assert doc["services"]["duplicati"]["deploy"]["mode"] == "replicated"


def test_router_tls_is_always_on_but_certresolver_only_with_acme_enabled():
    off = render("prod")["services"]["duplicati"]["deploy"]["labels"]
    on = render("prod", ACME_ENABLED=True)["services"]["duplicati"]["deploy"]["labels"]

    def has(labels, suffix):
        return any(l.endswith(suffix) for l in labels)

    assert has(off, ".tls=true")
    assert not any(".tls.certresolver=" in l for l in off)

    assert has(on, ".tls=true")
    assert any(l.endswith(".tls.certresolver=main") for l in on)


def test_no_secret_var_has_a_literal_default_in_this_template():
    text = SKBACKUP.read_text()
    assert "settings_encryption_key" not in text  # only skbackup.env.j2 needs it
    assert "ui_password" not in text
