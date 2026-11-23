"""skai's deploy rendered through REAL Ansible: digest-pinned images (equal
in playbook and compose), secrets only in 0600 env files, the Open WebUI env
contract (models, web search, RAG, OIDC with group admin, tool server JSON),
the corpus presets, the routers (API router without error pages, the
outside password sign-in refused in oidc mode), voice opt-in, and the
fail-closed checks."""
import json
import re
import shutil
from pathlib import Path

import pytest
import yaml

import role_render_support as rs

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")

ROLE = Path(__file__).resolve().parents[1] / "ansible/optional/skai"
BASE = rs.example("skai")
SET = rs.example("skai", "set")
CORPUS = {"CORPUS_TOKEN": "example-corpus-token-not-a-secret-0123456789",
          "CORPORA": [{"name": "example-legal", "title": "Example Legal"}]}


def cfg(out, env="prod"):
    return out / f"var/data/config/skai-{env}"


def compose(out, env="prod"):
    return yaml.safe_load((cfg(out, env) / "skai.yml").read_text())


def labels(doc):
    return dict(l.split("=", 1) for l in doc["services"]["skai"]["deploy"]["labels"])


@pytest.fixture(scope="module")
def base_out(tmp_path_factory):
    return rs.ok(tmp_path_factory.mktemp("skai-base"), "skai", BASE)


@pytest.fixture(scope="module")
def corpus_out(tmp_path_factory):
    return rs.ok(tmp_path_factory.mktemp("skai-corpus"), "skai", {**BASE, **CORPUS})


def test_default_images_are_digest_pinned_and_equal_everywhere():
    for var, needle in (("ska_default_image", "ghcr.io/open-webui/open-webui:v"),
                        ("ska_default_db_image", "pgvector/pgvector:"),
                        ("ska_default_voice_image", "ghcr.io/speaches-ai/speaches:")):
        pins = set()
        for env in ("dev", "staging", "prod"):
            pins |= set(re.findall(var + r': "([^"]+)"', (ROLE / f"deploy_skai-{env}.yml").read_text()))
        tpl = (ROLE / "src/config/skai/skai.yml.j2").read_text()
        pins |= {p for p in re.findall(r"default\('([^']+@sha256:[0-9a-f]{64})'", tpl) if p.startswith(needle)}
        assert len(pins) == 1, (var, pins)
        pin = pins.pop()
        assert pin.startswith(needle) and re.search(r":[^:@]*[0-9][^:@]*@sha256:[0-9a-f]{64}$", pin)
    assert "-bookworm@" in re.findall(r'ska_default_db_image: "([^"]+)"', (ROLE / "deploy_skai-prod.yml").read_text())[0]


@pytest.mark.parametrize("env", ["dev", "staging"])
def test_other_envs_render(tmp_path, env):
    out = rs.ok(tmp_path, "skai", BASE, env)
    assert set(compose(out, env)["networks"]) == {f"skai-{env}", f"cloud-public-{env}", f"skpeek-{env}",
                                                   f"skembed-{env}", f"skcorpus-{env}"}


def test_env_contract_models_search_rag(base_out):
    e = rs.env_file(cfg(base_out) / "skai.env")
    assert e["ENABLE_PERSISTENT_CONFIG"] == "false"
    assert e["ENABLE_OLLAMA_API"] == "false"
    assert e["OPENAI_API_BASE_URL"] == BASE["GATEWAY_URL"] and e["OPENAI_API_KEY"] == BASE["GATEWAY_API_KEY"]
    assert e["DEFAULT_MODELS"] == "example-chat" and e["TASK_MODEL_EXTERNAL"] == "example-chat"
    assert e["ENABLE_WEB_SEARCH"] == "true" and e["WEB_SEARCH_ENGINE"] == "searxng"
    assert e["SEARXNG_QUERY_URL"] == "http://skpeek-prod_skpeek:8080/search?q=<query>"
    assert e["ENABLE_SEARCH_QUERY_GENERATION"] == "true" and e["WEB_SEARCH_RESULT_COUNT"] == "6"
    assert e["BYPASS_WEB_SEARCH_EMBEDDING_AND_RETRIEVAL"] == "false"
    assert e["ENABLE_RAG_HYBRID_SEARCH"] == "true"
    assert e["RAG_EMBEDDING_ENGINE"] == "openai"
    assert e["RAG_OPENAI_API_BASE_URL"] == "http://skembed-prod_skembed:8080/v1"
    assert e["RAG_EMBEDDING_MODEL"] == "example-embed"
    assert e["VECTOR_DB"] == "pgvector"
    assert e["DATABASE_URL"] == f"postgresql://skai:{BASE['DB_PASSWORD']}@db:5432/skai" == e["PGVECTOR_DB_URL"]
    assert "TOOL_SERVER_CONNECTIONS" not in e  # no CORPUS_TOKEN, no tool server
    assert "AUDIO_STT_ENGINE" not in e


def test_oidc_with_group_admin(base_out):
    e = rs.env_file(cfg(base_out) / "skai.env")
    assert e["OAUTH_CLIENT_ID"] == "skai" and e["OAUTH_CLIENT_SECRET"] == BASE["OIDC_CLIENT_SECRET"]
    assert e["OPENID_PROVIDER_URL"] == "https://sso.render.example.test/application/o/skai/.well-known/openid-configuration"
    assert e["OPENID_REDIRECT_URI"] == "https://skai.render.example.test/oauth/oidc/login/callback"
    assert e["ENABLE_OAUTH_ROLE_MANAGEMENT"] == "true" and e["OAUTH_ROLES_CLAIM"] == "groups"
    assert e["OAUTH_ADMIN_ROLES"] == "skadmin" and e["OAUTH_ALLOWED_ROLES"] == "skai-users,skadmin"
    assert e["OAUTH_MERGE_ACCOUNTS_BY_EMAIL"] == "false"
    assert e["ENABLE_LOGIN_FORM"] == "false" and e["ENABLE_SIGNUP"] == "false"
    assert e["WEBUI_ADMIN_EMAIL"] == "skai-provisioner@example.test"


def test_secrets_stay_out_of_the_compose_file(corpus_out):
    text = (cfg(corpus_out) / "skai.yml").read_text()
    for key in ("SECRET_KEY", "DB_PASSWORD", "PROVISIONER_PASSWORD", "GATEWAY_API_KEY", "OIDC_CLIENT_SECRET"):
        assert BASE[key] not in text, key
    assert CORPUS["CORPUS_TOKEN"] not in text
    presets = (corpus_out / "var/data/skai-prod/provision/presets.json").read_text()
    assert CORPUS["CORPUS_TOKEN"] not in presets and BASE["PROVISIONER_PASSWORD"] not in presets


def test_one_tool_server_per_corpus_admin_only_in_env(corpus_out):
    e = rs.env_file(cfg(corpus_out) / "skai.env")
    conns = json.loads(e["TOOL_SERVER_CONNECTIONS"])
    assert conns == [{
        "url": "http://skcorpus-prod_skcorpus:8000/c/example-legal", "path": "openapi.json", "type": "openapi",
        "auth_type": "bearer", "key": CORPUS["CORPUS_TOKEN"],
        "config": {"enable": True, "access_grants": []},
        "info": {"id": "skcorpus-example-legal", "name": "Example Legal search",
                 "description": "Read-only search over the Example Legal corpus (vectors and graph)."}}]
    assert e["ENABLE_TOOL_SERVERS"] == "true"


def test_corpus_preset_scoped_to_its_tool_and_groups(corpus_out):
    data = json.loads((corpus_out / "var/data/skai-prod/provision/presets.json").read_text())
    assert data["tool_servers"] == {"skcorpus-example-legal": ["skai-users"]}
    presets = data["presets"]
    assert len(presets) == 1
    p = presets[0]
    assert p["id"] == "corpus-example-legal" and p["name"] == "Example Legal" and p["base_model_id"] == "example-chat"
    assert p["meta"]["toolIds"] == ["server:skcorpus-example-legal"]
    assert p["params"]["function_calling"] == "native"
    s = p["params"]["system"]
    for needle in ('corpus "example-legal"', "graph_entity", "graph_neighbors", "corpus_search", "corpus_get",
                   "title, file_path, chunk_index", "only when the user asks for a quote", "warning"):
        assert needle in s, needle
    assert "{corpus}" not in s and "{title}" not in s
    assert p["allowed_groups"] == ["skai-users"] and "access_grants" not in p


def test_provision_service_only_with_corpora(base_out, corpus_out):
    assert "provision" not in compose(base_out)["services"]
    assert not (cfg(base_out) / "provision.env").exists()
    svc = compose(corpus_out)["services"]["provision"]
    assert svc["image"] == compose(corpus_out)["services"]["skai"]["image"]
    assert svc["entrypoint"] == ["python3", "-I", "/provision/provision_presets.py", "--loop", "300",
                                 "/provision/presets.json"]
    assert svc["volumes"] == ["/var/data/skai-prod/provision:/provision:ro"]
    assert svc["networks"] == ["skai-prod"]
    pe = rs.env_file(cfg(corpus_out) / "provision.env")
    assert pe == {"SKAI_PROVISION_URL": "http://skai:8080", "WEBUI_ADMIN_EMAIL": "skai-provisioner@example.test",
                  "WEBUI_ADMIN_PASSWORD": BASE["PROVISIONER_PASSWORD"]}
    bodies = [p.read_text() for p in (corpus_out / "_shell").glob("*.sh")]
    wait = [b for b in bodies if "_provision" in b and "RECONCILED" in b]
    assert len(wait) == 1 and "docker service logs --raw" in wait[0]
    assert not any("docker exec" in b for b in bodies)
    assert not any(BASE["PROVISIONER_PASSWORD"] in b for b in bodies)


def test_oauth_group_management_and_users_group(base_out):
    e = rs.env_file(cfg(base_out) / "skai.env")
    assert e["ENABLE_OAUTH_GROUP_MANAGEMENT"] == "true" and e["ENABLE_OAUTH_GROUP_CREATION"] == "false"
    assert e["OAUTH_GROUPS_CLAIM"] == "groups"


def test_existing_embed_endpoint_means_no_skembed_network(tmp_path):
    out = rs.ok(tmp_path, "skai", {**BASE, "EMBED_URL": "http://embed.example.test:8090/v1"})
    assert f"skembed-prod" not in compose(out)["networks"]
    assert rs.env_file(cfg(out) / "skai.env")["RAG_OPENAI_API_BASE_URL"] == "http://embed.example.test:8090/v1"


def test_routers(base_out):
    lb = labels(compose(base_out))
    assert lb["traefik.http.routers.skai.rule"] == "Host(`skai.render.example.test`)"
    assert lb["traefik.http.routers.skai.middlewares"] == "skai-chain@file"
    assert lb["traefik.http.routers.skai-api.middlewares"] == "skai-headers@file,gzip@file"
    assert "PathPrefix(`/api/`)" in lb["traefik.http.routers.skai-api.rule"]
    assert lb["traefik.http.routers.skai-signin.priority"] == "1000"
    assert "Path(`/api/v1/auths/signin`)" in lb["traefik.http.routers.skai-signin.rule"]
    assert lb["traefik.http.middlewares.skai-no-signin.ipallowlist.sourcerange"] == "127.0.0.1/32"
    assert lb["traefik.http.services.skai.loadbalancer.server.port"] == "8080"
    assert "traefik.http.routers.skai.tls.options" not in lb


def test_local_mode_has_no_signin_block_and_shows_the_form(tmp_path):
    out = rs.ok(tmp_path, "skai", {**BASE, "AUTH_MODE": "local", "OIDC_CLIENT_SECRET": ""})
    assert not any("signin" in k for k in labels(compose(out)))
    e = rs.env_file(cfg(out) / "skai.env")
    assert e["ENABLE_LOGIN_FORM"] == "true" and "OAUTH_CLIENT_ID" not in e


def test_set_pass_voice_rerank_and_knobs(tmp_path):
    out = rs.ok(tmp_path, "skai", {**BASE, **SET})
    doc = compose(out)
    assert set(doc["services"]) == {"skai", "db", "db-backup", "voice"}
    assert doc["services"]["voice"]["image"] == SET["VOICE_IMAGE"]
    assert rs.env_file(cfg(out) / "voice.env")["API_KEY"] == SET["VOICE_API_KEY"]
    e = rs.env_file(cfg(out) / "skai.env")
    assert e["AUDIO_STT_OPENAI_API_BASE_URL"] == "http://voice:8000/v1" and e["AUDIO_TTS_MODEL"] == "example-org/example-tts"
    assert e["RAG_RERANKING_ENGINE"] == "external" and e["RAG_EXTERNAL_RERANKER_URL"] == SET["RERANK_URL"]
    assert e["OAUTH_ALLOWED_ROLES"] == "example-ai-users,example-ai-admins"
    assert e["TASK_MODEL_EXTERNAL"] == "example-fast"
    lb = labels(doc)
    assert lb["traefik.http.routers.skai.rule"] == "Host(`ai.example.test`)"
    assert lb["traefik.http.routers.skai.tls.options"] == "modern@file"
    data = json.loads((out / "var/data/skai-prod/provision/presets.json").read_text())
    presets = data["presets"]
    assert [p["base_model_id"] for p in presets] == ["example-chat", "example-big"]
    assert data["tool_servers"] == {"skcorpus-example-legal": ["example-ai-users"],
                                    "skcorpus-example-notes": ["example-ai-admins", "*"]}
    assert doc["services"]["provision"]["entrypoint"][4] == "600"
    assert presets[1]["params"]["system"] == "Answer from corpus example-notes (example-notes) only, cite title, file_path and chunk_index."


def test_db_and_backup(base_out):
    doc = compose(base_out)
    db = doc["services"]["db"]
    assert db["deploy"]["update_config"]["order"] == "stop-first"
    assert db["volumes"] == ["/var/data/skai-prod/postgres:/var/lib/postgresql/data"]
    assert "pg_dumpall -h db" in "\n".join(doc["services"]["db-backup"]["entrypoint"])
    pg = rs.env_file(cfg(base_out) / "postgres.env")
    assert pg["POSTGRES_PASSWORD"] == BASE["DB_PASSWORD"] and pg["POSTGRES_USER"] == "skai"


@pytest.mark.parametrize("vault,needle", [
    ({"SECRET_KEY": "short"}, "SECRET_KEY (32+"),
    ({"DB_PASSWORD": "has a space in it!!"}, "DB_PASSWORD (16+"),
    ({"GATEWAY_URL": ""}, "GATEWAY_URL"),
    ({"PROVISIONER_PASSWORD": ""}, "PROVISIONER_PASSWORD (32+"),
    ({"EMBED_MODEL": ""}, "EMBED_MODEL set"),
    ({"OIDC_CLIENT_SECRET": ""}, "oidc needs"),
    ({"AUTH_MODE": "saml"}, "must be oidc"),
    ({"CORPORA": [{"name": "x"}]}, "CORPORA"),
    ({"IMAGE": "ghcr.io/open-webui/open-webui:main"}, "pinned by digest"),
    ({"VOICE": True}, "VOICE_API_KEY"),
    ({"DB_PATH": "/srv/skai/pg"}, "no node.hostname"),
    ({"SEARXNG_URL": "http://search.example.test/search"}, "<query>"),
    ({**CORPUS, "CORPORA": [{"name": "x", "allowed_groups": "g"}]}, "allowed_groups (when set)"),
    ({**CORPUS, "CORPORA": [{"name": "x", "allowed_groups": ["a,b"]}]}, "allowed_groups (when set)"),
    ({"PROVISION_INTERVAL": 5}, "PROVISION_INTERVAL"),
])
def test_bad_vaults_fail_closed(tmp_path, vault, needle):
    assert needle in rs.fails(tmp_path, "skai", {**BASE, **vault})
