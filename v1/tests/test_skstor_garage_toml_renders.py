"""skstor's garage.toml template must render valid TOML and default to a
db_engine that is safe on the shared NFS filesystem skstor's meta/data now
bind-mount onto (see docs/decisions/skstor-backend.md and skstor's README):
Garage's own docs say LMDB (its default) "is prone to database corruption
after an unclean shutdown", which a Swarm reschedule of this replicas=1
service is exactly an instance of, while Sqlite "does not have the issues
listed above for LMDB" (https://garagehq.deuxfleurs.fr/documentation/
reference-manual/configuration/#db_engine)."""
import pathlib
import tomllib

import jinja2
import pytest

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
GARAGE_TOML = ANSIBLE / "optional/skstor/src/config/skstor/garage.toml.j2"


def render(env_name="prod", **skstor_overrides):
    skstor = {"rpc_secret": "a" * 64}
    skstor.update(skstor_overrides)
    tpl_env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    out = tpl_env.from_string(GARAGE_TOML.read_text()).render(env=env_name, skstor=skstor)
    return tomllib.loads(out)


def test_renders_valid_toml_and_db_engine_defaults_to_sqlite():
    doc = render()
    assert doc["db_engine"] == "sqlite"


def test_db_engine_is_configurable():
    # "fjall", not "lmdb": garage.toml.j2 hardcoded db_engine = "lmdb" before
    # this change, so asserting "lmdb" here would pass even unconfigured.
    doc = render(db_engine="fjall")
    assert doc["db_engine"] == "fjall"


@pytest.mark.parametrize("key,toml_key,default", [
    ("meta_dir", "metadata_dir", "/var/lib/garage/meta"),
    ("data_dir", "data_dir", "/var/lib/garage/data"),
])
def test_meta_and_data_dir_default_to_the_bind_mount_paths(key, toml_key, default):
    doc = render()
    assert doc[toml_key] == default


def test_meta_dir_is_configurable_for_a_node_local_ssd_override():
    doc = render(meta_dir="/mnt/nvme/garage-meta")
    assert doc["metadata_dir"] == "/mnt/nvme/garage-meta"


def test_data_dir_is_configurable():
    doc = render(data_dir="/mnt/nvme/garage-data")
    assert doc["data_dir"] == "/mnt/nvme/garage-data"
