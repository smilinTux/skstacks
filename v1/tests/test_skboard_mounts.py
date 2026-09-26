"""skboard (Vikunja) bind-mounted its data dir onto /app/vikunja, the image's
own install dir, hiding the binary: every task failed with `exec:
"/app/vikunja/vikunja": stat ... no such file or directory` on a fresh
cluster (skstack06 v2.19.0 rc4). A pre-existing deployment only works if a copy
of the binary sits in its data dir, which an image upgrade never refreshes.

Keep the image's /app/vikunja visible: mount the data dir at /db (sqlite at
/db/vikunja.db, the same file an existing data dir already holds) and
data/files at /app/vikunja/files, so existing instances keep their data with
no file moves."""
import pathlib

import jinja2
import yaml

CFG = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skboard/src/config/skboard"
PLAYBOOKS = sorted((CFG.parents[2]).glob("deploy_skboard-*.yml"))


def _compose(env="dev"):
    j = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    out = j.from_string((CFG / "skboard.yml.j2").read_text()).render(env=env, app="skboard", skboard={})
    return yaml.safe_load(out)


def _env_file(env="dev"):
    j = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    out = j.from_string((CFG / "skboard.env.j2").read_text()).render(env=env, app="skboard", skboard={})
    return dict(l.split("=", 1) for l in out.splitlines() if "=" in l and not l.startswith("#"))


def _targets():
    vols = _compose()["services"]["vikunja"]["volumes"]
    return {v.split(":")[1]: v.split(":")[0] for v in vols}


def test_nothing_is_mounted_over_the_image_install_dir():
    assert "/app/vikunja" not in _targets()


def test_db_and_files_live_on_the_existing_data_dir_layout():
    t = _targets()
    assert t["/db"] == "/var/data/skboard-dev/data"
    assert t["/app/vikunja/files"] == "/var/data/skboard-dev/data/files"
    env = _env_file()
    assert env["VIKUNJA_DATABASE_PATH"] == "/db/vikunja.db"
    assert env["VIKUNJA_FILES_BASEPATH"] == "/app/vikunja/files"


def test_deploy_creates_the_files_dir_in_every_env():
    assert len(PLAYBOOKS) == 3
    for pb in PLAYBOOKS:
        assert "/var/data/{{ app }}-{{ env }}/data/files" in pb.read_text(), pb.name
