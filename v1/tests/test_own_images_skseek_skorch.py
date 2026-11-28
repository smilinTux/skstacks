"""skseek and skorch's dump sidecar run images we build and publish ourselves
(smilintux/skseek-runtime, smilintux/postgres-backup-local) from image/ in each
role, because the upstream images are stale and carry fixable CVEs. Same shape
as skcorpus: the Dockerfile bases are digest pinned, the shipped default is a
dated tag pinned by digest, and an instance can pin something else."""
import pathlib
import re

import jinja2
import pytest
import yaml

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional"
SEEK = ANSIBLE / "skseek"
ORCH = ANSIBLE / "skorch"
SEEK_TPL = SEEK / "src/config/skseek/skseek.yml.j2"
ORCH_TPL = ORCH / "src/config/skorch/skorch.yml.j2"
DIGEST = re.compile(r"^smilintux/(?P<repo>[a-z-]+):(?P<tag>[\w.]+-\d{8})@sha256:[0-9a-f]{64}$")


def _env():
    return jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)


def _seek(**extra):
    vars_ = {"PORT": "3000", "CLUSTERNAME": "c1", "DOMAIN": "example.test", "SEARXNG_API_URL": "http://x:8080",
             "NEXT_PUBLIC_API_URL": "https://a/api", "NEXT_PUBLIC_WS_URL": "wss://a/ws"}
    skseek = {**vars_, **{k: v for k, v in extra.items() if k != "skseek_image"}}
    kw = {"skseek_image": extra["skseek_image"]} if "skseek_image" in extra else {}
    out = _env().from_string(SEEK_TPL.read_text()).render(
        app="skseek", env="prod", cluster_name="c1", domain="example.test", skseek=skseek, **kw)
    return yaml.safe_load(out)["services"]["skseek"]["image"]


def _orch(**extra):
    out = _env().from_string(ORCH_TPL.read_text()).render(
        app="skorch", env="prod", cluster_name="c1", domain="example.test",
        skorch_cfg={"POSTGRES_USER": "u", "POSTGRES_DB": "d", **extra})
    return yaml.safe_load(out)["services"]


def test_skseek_default_is_our_dated_digest_pinned_image():
    m = DIGEST.match(_seek())
    assert m and m["repo"] == "skseek-runtime", _seek()


def test_skseek_image_overrides():
    pin = "registry.example.test/skseek:1@sha256:" + "a" * 64
    assert _seek(IMAGE=pin) == pin
    assert _seek(skseek_image=pin) == pin            # the older play-level variable still works
    assert _seek(IMAGE=pin, skseek_image="other") == pin


def test_skorch_backup_default_is_our_dated_digest_pinned_image_of_the_server_major():
    img = _orch()["postgres-backup"]["image"]
    m = DIGEST.match(img)
    assert m and m["repo"] == "postgres-backup-local" and m["tag"].startswith("18-"), img
    assert re.search(r"pgvector/pgvector:pg18|postgres:18", ORCH.joinpath("deploy_skorch-prod.yml").read_text())


def test_skorch_backup_image_overrides():
    pin = "smilintux/postgres-backup-local:16-20261128@sha256:" + "b" * 64
    assert _orch(POSTGRES_BACKUP_IMAGE=pin)["postgres-backup"]["image"] == pin


@pytest.mark.parametrize("role", ["skseek", "skorch"])
def test_dockerfile_bases_are_digest_pinned(role):
    text = (ANSIBLE / role / "image/Dockerfile").read_text()
    froms = re.findall(r"^FROM\s+(\S+)", text, re.M)
    args = dict(re.findall(r"^ARG (\w+)=(\S+)", text, re.M))
    assert froms
    for ref in froms:
        ref = re.sub(r"\$\{(\w+)\}", lambda m: args.get(m[1], m[0]), ref)
        assert re.search(r"@sha256:[0-9a-f]{64}$", ref), f"{role}: FROM {ref} is not pinned by digest"


def test_skseek_source_commit_is_pinned():
    text = (SEEK / "image/Dockerfile").read_text()
    assert re.search(r"^ARG PERPLEXICA_COMMIT=[0-9a-f]{40}$", text, re.M)
    assert re.search(r"^ARG NEXT_VERSION=\d+\.\d+\.\d+$", text, re.M)


def test_skorch_image_keeps_the_upstream_contract():
    d = ORCH / "image"
    text = (d / "Dockerfile").read_text()
    for f in ("backup.sh", "env.sh", "init.sh", "hooks/00-webhook", "UPSTREAM.md"):
        assert (d / f).exists(), f
    assert 'CMD ["/init.sh"]' in text and "HEALTHCHECK" in text and "VOLUME /backups" in text
    # every env var the role's compose sets or the env file renders is one the image reads
    env = (ORCH / "src/config/skorch/postgres-backup.env.j2").read_text() + ORCH_TPL.read_text()
    for var in ("POSTGRES_HOST", "POSTGRES_EXTRA_OPTS", "SCHEDULE", "BACKUP_KEEP_DAYS", "HEALTHCHECK_PORT", "POSTGRES_PASSWORD"):
        assert var in env
        assert var in text or var in (d / "env.sh").read_text() or var in (d / "backup.sh").read_text(), var


@pytest.mark.parametrize("role", ["skseek", "skorch"])
def test_readme_documents_the_image(role):
    text = (ANSIBLE / role / "README.md").read_text()
    assert re.search(r"^## Image", text, re.M) and "docker build" in text and "docker push" in text
