"""sksync mount layout parity: live mounts 3 dirs (sync-data -> a subdirectory
of the container home, syncthing_config, and a third syncthing_data ->
/var/syncthing/data) while the framework mounted 2 (sync-data -> the whole
container home, syncthing_config). Verified live via
`docker service inspect sksync-prod_syncthing`. SYNC_DATA_MOUNT and
SYNC_DATA_EXTRA_MOUNT let an instance express live's layout without moving
any data (only the mount TARGET changes, not the host-side directory)."""
import pathlib

import jinja2
import yaml

SKSYNC = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/sksync/src/config/sksync/sksync.yml.j2"


def render(**sksync):
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    out = env.from_string(SKSYNC.read_text()).render(env="prod", app="sksync", sksync=sksync)
    return yaml.safe_load(out)["services"]["syncthing"]["volumes"]


def test_default_layout_is_two_mounts_unchanged():
    vols = [str(v) for v in render()]
    assert "/var/data/sksync-prod/sync-data:/var/syncthing" in vols
    assert "/var/data/sksync-prod/syncthing_config:/var/syncthing/config" in vols
    assert not any("syncthing_data" in v for v in vols)


def test_instance_can_reproduce_the_live_three_mount_layout():
    vols = [str(v) for v in render(
        SYNC_DATA_MOUNT="/var/syncthing/sync-data",
        SYNC_DATA_EXTRA_MOUNT="/var/syncthing/data",
    )]
    assert "/var/data/sksync-prod/sync-data:/var/syncthing/sync-data" in vols
    assert "/var/data/sksync-prod/syncthing_config:/var/syncthing/config" in vols
    assert "/var/data/sksync-prod/syncthing_data:/var/syncthing/data" in vols


def test_extra_mount_is_source_data_unchanged_only_target_moves():
    # The parity fix must be expressible by changing vault vars alone - the
    # host-side source directories (sync-data, syncthing_config,
    # syncthing_data) never change, only the container-side target.
    vols = [str(v) for v in render(SYNC_DATA_EXTRA_MOUNT="/var/syncthing/data")]
    sources = {v.split(":")[0] for v in vols if v.startswith("/var/data/")}
    assert sources == {
        "/var/data/sksync-prod/sync-data",
        "/var/data/sksync-prod/syncthing_config",
        "/var/data/sksync-prod/syncthing_data",
    }
