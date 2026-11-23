"""skcorpus's deploy rendered through REAL Ansible: the app file pinned by
sha256 (and the pin equal to the file), the corpora rendered as JSON the app
accepts (pgvector corpora too), the networks it joins, secrets (Postgres DSNs
included) only in the 0600 env file."""
import hashlib
import importlib.util
import json
import re
import shutil
from pathlib import Path

import pytest
import yaml

import role_render_support as rs

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")

ROLE = Path(__file__).resolve().parents[1] / "ansible/optional/skcorpus"
BASE = rs.example("skcorpus")
SET = rs.example("skcorpus", "set")


def compose(out, env="prod"):
    return yaml.safe_load((out / f"var/data/config/skcorpus-{env}/skcorpus.yml").read_text())


def test_app_pin_matches_the_file_in_every_env():
    digest = hashlib.sha256((ROLE / "app/skcorpus.py").read_bytes()).hexdigest()
    for env in ("dev", "staging", "prod"):
        pins = re.findall(r'skc_app_sha256: "([0-9a-f]{64})"', (ROLE / f"deploy_skcorpus-{env}.yml").read_text())
        assert pins == [digest], f"deploy_skcorpus-{env}.yml: update skc_app_sha256 to {digest}"


def test_default_image_is_digest_pinned_and_equal_everywhere():
    pins = set()
    for env in ("dev", "staging", "prod"):
        pins |= set(re.findall(r'skc_default_image: "([^"]+)"', (ROLE / f"deploy_skcorpus-{env}.yml").read_text()))
    pins |= set(re.findall(r"default\('([^']+@sha256:[0-9a-f]{64})'", (ROLE / "src/config/skcorpus/skcorpus.yml.j2").read_text()))
    assert len(pins) == 1 and re.search(r"@sha256:[0-9a-f]{64}$", pins.pop())


def test_requirements_pin_every_package():
    lines = [l for l in (ROLE / "image/requirements.txt").read_text().splitlines() if l and not l.startswith("#")]
    assert lines and all(re.match(r"^[A-Za-z0-9_.-]+==[0-9][0-9A-Za-z.]*$", l) for l in lines)
    assert {"fastapi", "uvicorn", "httpx"} <= {l.split("==")[0] for l in lines}
    assert re.search(r"^FROM python:[^@\s]+@sha256:[0-9a-f]{64}$", (ROLE / "image/Dockerfile").read_text(), re.M)


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_base_render(tmp_path, env):
    out = rs.ok(tmp_path, "skcorpus", BASE, env)
    doc = compose(out, env)
    svc = doc["services"]["skcorpus"]
    assert set(doc["networks"]) == {f"skcorpus-{env}", f"skembed-{env}", f"skvector-{env}", f"skgraph-{env}"}
    assert f"/var/data/skcorpus-{env}/app/skcorpus.py:/skcorpus.py:ro" in svc["volumes"]
    assert svc["read_only"] is True and "traefik.enable=true" not in svc["deploy"]["labels"]
    assert json.loads((out / f"var/data/config/skcorpus-{env}/corpora.json").read_text()) == []
    e = rs.env_file(out / f"var/data/config/skcorpus-{env}/skcorpus.env")
    assert e["QDRANT_URL"] == f"http://skvector-{env}_qdrant:6333"
    assert e["FALKORDB_HOST"] == f"skgraph-{env}_falkordb"
    assert e["EMBED_URL"] == f"http://skembed-{env}_skembed:8080/v1"
    assert e["GLOBAL_ROUTES"] == "false" and e["COHERENCE_THRESHOLD"] == "0.9"
    assert e["COHERENCE_SAMPLE"] == "32" and e["COHERENCE_INTERVAL_SECONDS"] == "3600"
    assert e["PG_STATEMENT_TIMEOUT_MS"] == "5000" and not [k for k in e if k.endswith("_DSN")]
    assert BASE["API_TOKEN"] not in (out / f"var/data/config/skcorpus-{env}/skcorpus.yml").read_text()


def test_set_render_corpora_accepted_by_the_app(tmp_path):
    out = rs.ok(tmp_path, "skcorpus", {**BASE, **SET})
    raw = json.loads((out / "var/data/config/skcorpus-prod/corpora.json").read_text())
    spec = importlib.util.spec_from_file_location("skc", ROLE / "app/skcorpus.py")
    if importlib.util.find_spec("fastapi") is None or importlib.util.find_spec("httpx") is None:
        pytest.skip("fastapi/httpx not installed")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert set(mod.load_corpora(raw)) == {"example-legal", "example-notes", "example-wiki"}
    e = rs.env_file(out / "var/data/config/skcorpus-prod/skcorpus.env")
    for key in ("QDRANT_API_KEY", "FALKORDB_PASSWORD", "EMBED_API_KEY"):
        assert e[key] == SET[key]
    assert set(compose(out)["networks"]) == {"skcorpus-prod", "skembed-prod", "skvector-prod", "example-db-net"}
    legal = mod.load_corpora(raw)["example-legal"]
    assert legal["embed_url"] == "http://router.example.test:8090/v1" and legal["allowed_groups"] == ["example-ai-users"]
    assert legal["coherence_threshold"] == 0.8 and legal["title"] == "Example Legal"
    assert e["GLOBAL_ROUTES"] == "true" and e["COHERENCE_MIN_COSINE"] == "0.98"
    wiki = mod.load_corpora(raw)["example-wiki"]
    assert wiki["backend"] == "pgvector" and wiki["collections"] == ["wiki", "wiki-raw"]
    # the DSN lives only in the 0600 env file, under the name the corpus gives
    dsn = SET["PG_DSNS"]["EXAMPLE_WIKI_DSN"]
    assert e["EXAMPLE_WIKI_DSN"] == dsn and e["PG_STATEMENT_TIMEOUT_MS"] == "3000"
    for f in ("corpora.json", "skcorpus.yml", "skcorpus.sh.env"):
        assert dsn not in (out / f"var/data/config/skcorpus-prod/{f}").read_text()


def test_existing_embed_endpoint_means_no_skembed_network(tmp_path):
    out = rs.ok(tmp_path, "skcorpus", {**BASE, "EMBED_URL": "http://embed.example.test:8090/v1"})
    assert set(compose(out)["networks"]) == {"skcorpus-prod", "skvector-prod", "skgraph-prod"}
    assert rs.env_file(out / "var/data/config/skcorpus-prod/skcorpus.env")["EMBED_URL"] == "http://embed.example.test:8090/v1"


PG_WIKI = {"name": "example-wiki", "backend": "pgvector", "pg_dsn_env": "EXAMPLE_WIKI_DSN",
           "pg_filter_values": ["wiki"], "embed_model": "m"}
PG_OK = {"CORPORA": [PG_WIKI], "PG_DSNS": {"EXAMPLE_WIKI_DSN": "postgresql://db.example.test/examplewiki"},
         "IMAGE": "registry.example.test/skcorpus:1@sha256:" + "0" * 64}


def test_pgvector_corpus_without_qdrant_collections_renders(tmp_path):
    out = rs.ok(tmp_path, "skcorpus", {**BASE, **PG_OK})
    raw = json.loads((out / "var/data/config/skcorpus-prod/corpora.json").read_text())
    assert raw == [PG_WIKI]
    e = rs.env_file(out / "var/data/config/skcorpus-prod/skcorpus.env")
    assert e["EXAMPLE_WIKI_DSN"] == PG_OK["PG_DSNS"]["EXAMPLE_WIKI_DSN"]


@pytest.mark.parametrize("vault,needle", [
    ({"API_TOKEN": "short"}, "API_TOKEN (32+ chars"),
    ({"CORPORA": [{"name": "x", "collections": [], "embed_model": "m"}]}, "CORPORA must be a list"),
    ({"CORPORA": [{"name": "x y", "collections": ["a"], "embed_model": "m"}]}, "CORPORA must be a list"),
    ({"CORPORA": [{"name": "x", "collections": ["a"]}]}, "CORPORA must be a list"),
    ({"CORPORA": [{"name": "x", "collections": ["a"], "embed_model": "m"}] * 2}, "CORPORA must be a list"),
    ({"IMAGE": "python:3.12"}, "pinned by digest"),
    ({"CORPORA": [{"name": "x", "collections": ["a"], "embed_model": "m", "embed_url": "ftp://x"}]}, "CORPORA must be a list"),
    ({"CORPORA": [{"name": "x", "collections": ["a"], "embed_model": "m", "allowed_groups": "g"}]}, "CORPORA must be a list"),
    ({"COHERENCE_THRESHOLD": 1.5}, "COHERENCE_THRESHOLD numbers"),
    ({"CORPORA": [{"name": "x", "backend": "chroma", "collections": ["a"], "embed_model": "m"}]}, "CORPORA must be a list"),
    ({"CORPORA": [{"name": "x", "backend": "qdrant", "embed_model": "m"}]}, "CORPORA must be a list"),
    ({**PG_OK, "PG_DSNS": {}}, "backend pgvector corpus needs pg_dsn_env"),
    ({**PG_OK, "PG_DSNS": {"API_TOKEN": "postgresql://db.example.test/x"}}, "backend pgvector corpus needs pg_dsn_env"),
    ({**PG_OK, "PG_DSNS": {"EXAMPLE_WIKI_DSN": "postgresql://db.example.test/x\nAPI_TOKEN=x"}},
     "backend pgvector corpus needs pg_dsn_env"),
    ({**PG_OK, "PG_DSNS": {"EXAMPLE_WIKI_DSN": ""}}, "backend pgvector corpus needs pg_dsn_env"),
    ({**PG_OK, "CORPORA": [{**PG_WIKI, "pg_filter_values": []}]}, "backend pgvector corpus needs pg_dsn_env"),
    ({**PG_OK, "CORPORA": [{k: v for k, v in PG_WIKI.items() if k != "pg_filter_values"}]},
     "backend pgvector corpus needs pg_dsn_env"),
    ({**PG_OK, "CORPORA": [{**PG_WIKI, "graph": "g"}]}, "backend pgvector corpus needs pg_dsn_env"),
    ({**PG_OK, "CORPORA": [{**PG_WIKI, "collections": ["a"]}]}, "backend pgvector corpus needs pg_dsn_env"),
    ({**PG_OK, "PG_NETWORKS": "example-db-net"}, "backend pgvector corpus needs pg_dsn_env"),
    ({k: v for k, v in PG_OK.items() if k != "IMAGE"}, "the default image lacks"),
])
def test_bad_vaults_fail_closed(tmp_path, vault, needle):
    assert needle in rs.fails(tmp_path, "skcorpus", {**BASE, **vault})


def test_changed_app_without_new_pin_stops_the_deploy(tmp_path):
    res, _ = rs.render(tmp_path, "skcorpus", BASE)
    assert res["status"] == "PASS"
    app = tmp_path / "work/ansible/optional/skcorpus/app/skcorpus.py"
    app.write_text(app.read_text() + "\n# tampered\n")
    assert "does not hash to skc_app_sha256" in rs.fails(tmp_path, "skcorpus", BASE)
