"""skcorpus (v1/ansible/optional/skcorpus/app/skcorpus.py) against a fake
Qdrant, a fake embeddings endpoint and a fake FalkorDB, no network:

* read-only guarantee: the only Qdrant calls are search and scroll, any
  other path raises before a request is sent; the only FalkorDB command is
  GRAPH.RO_QUERY;
* Cypher parameterization: user input reaches FalkorDB only as an encoded
  parameter, so an injection attempt stays inside a string literal;
* merge ordering across collections, hard limits, corpus scoping, auth;
* an empty corpus config serves an empty list.
"""
import importlib.util
import json
import pathlib
import re
import socket
import threading

import pytest

httpx = pytest.importorskip("httpx")
fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

APP = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skcorpus/app/skcorpus.py"
spec = importlib.util.spec_from_file_location("skcorpus_app", APP)
sk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sk)

TOKEN = "example-corpus-token-not-a-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
CORPORA = [
    {"name": "example-legal", "description": "Example corpus", "collections": ["col-a", "col-b"],
     "graph": "example-graph", "embed_model": "example-embed", "query_prefix": "query: "},
    {"name": "example-novec", "collections": ["col-c"], "embed_model": "example-embed"},
]


class FakeQdrant:
    def __init__(self, results=None, points=None):
        self.calls = []
        self.results = results or {}
        self.points = points or []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        self.calls.append((request.method, request.url.path, body))
        m = re.match(r"^/collections/([^/]+)/points/(search|scroll)$", request.url.path)
        if request.method != "POST" or not m:
            return httpx.Response(500, json={"status": "fake qdrant: unexpected call"})
        if m.group(2) == "search":
            return httpx.Response(200, json={"result": self.results.get(m.group(1), [])[: body["limit"]]})
        return httpx.Response(200, json={"result": {"points": self.points}})


class FakeEmbed:
    def __init__(self):
        self.calls = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.url.path, json.loads(request.content), request.headers.get("authorization")))
        return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2, 0.3]}]})


class FakeFalkorConn:
    def __init__(self, reply=None):
        self.commands = []
        self.reply = reply if reply is not None else [["name", "labels", "properties"],
                                                      [["Example Entity", ["Person"], ["role", "witness"]]],
                                                      ["Cached execution: 0"]]

    def command(self, *args):
        self.commands.append(args)
        return self.reply


def pt(title, idx, score):
    return {"id": f"{title}-{idx}", "score": score,
            "payload": {"text": f"text {title} {idx}", "title": title, "file_path": f"docs/{title}.md",
                        "chunk_index": idx, "category": "example"}}


def make(qdrant=None, falkor=None, corpora=CORPORA, embed=None):
    qdrant = qdrant or FakeQdrant()
    embed = embed or FakeEmbed()
    falkor = falkor or FakeFalkorConn()
    c = sk.Corpus(sk.load_corpora(corpora),
                  sk.QdrantReader("http://qdrant.invalid:6333", "ro-key", transport=httpx.MockTransport(qdrant.handler)),
                  sk.FalkorReader(falkor),
                  sk.Embedder("http://embed.invalid/v1", "embed-key", transport=httpx.MockTransport(embed.handler)))
    return TestClient(sk.create_app(c, token=TOKEN)), qdrant, falkor, embed


# --- read-only guarantee ----------------------------------------------------

def test_qdrant_reader_refuses_every_non_read_path_before_sending():
    q = FakeQdrant()
    reader = sk.QdrantReader("http://qdrant.invalid:6333", transport=httpx.MockTransport(q.handler))
    for path in ("/collections/col-a/points", "/collections/col-a/points/delete", "/collections/col-a",
                 "/collections/col-a/points/upsert", "/collections/col-a/index", "/snapshots",
                 "/collections/../points/search"):
        with pytest.raises(sk.ReadOnlyViolation):
            reader._post(path, {})
    assert q.calls == []


def test_reader_classes_expose_no_write_methods():
    public = {n for n in dir(sk.QdrantReader) if not n.startswith("_")}
    assert public == {"search", "scroll"}
    assert {n for n in dir(sk.FalkorReader) if not n.startswith("_")} == {"COMMAND", "ro_query"}
    assert sk.FalkorReader.COMMAND == "GRAPH.RO_QUERY"


def test_a_full_session_sends_only_search_scroll_and_ro_query():
    client, q, f, _ = make(FakeQdrant({"col-a": [pt("a", 0, 0.9)]}, [pt("a", 0, None)]))
    assert client.post("/search", headers=AUTH, json={"corpus": "example-legal", "query": "x"}).status_code == 200
    assert client.post("/chunks", headers=AUTH, json={"corpus": "example-legal", "collection": "col-a",
                                                      "file_path": "docs/a.md", "chunk_index": 0}).status_code == 200
    assert client.post("/graph/entity", headers=AUTH, json={"corpus": "example-legal", "name": "x"}).status_code == 200
    assert client.post("/graph/neighbors", headers=AUTH,
                       json={"corpus": "example-legal", "name": "x", "depth": 2}).status_code == 200
    assert {(m, p.rsplit("/", 1)[1]) for m, p, _ in q.calls} <= {("POST", "search"), ("POST", "scroll")}
    assert {c[0] for c in f.commands} == {"GRAPH.RO_QUERY"}


def test_falkor_queries_contain_no_write_clause():
    for q in (sk.ENTITY_QUERY, sk.NEIGHBORS_1, sk.NEIGHBORS_2):
        assert not re.search(r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|CALL)\b", q, re.I)


# --- Cypher parameterization --------------------------------------------------

def test_injection_attempt_stays_inside_a_string_literal():
    client, _, f, _ = make()
    evil = 'x") DETACH DELETE n //\\" MATCH (m) SET m.pwned = 1 RETURN m'
    r = client.post("/graph/entity", headers=AUTH, json={"corpus": "example-legal", "name": evil})
    assert r.status_code == 200
    cmd, graph, query = f.commands[-1]
    assert (cmd, graph) == ("GRAPH.RO_QUERY", "example-graph")
    # The Cypher text after the parameter header is exactly the constant.
    assert query.endswith(sk.ENTITY_QUERY.format(prop="name"))
    header = query[: -len(sk.ENTITY_QUERY.format(prop="name"))]
    assert header == f"CYPHER name={sk.cypher_literal(evil)} limit={sk.MAX_GRAPH_ROWS} "
    # Decoding the literal gives back the input unchanged: nothing escaped it.
    lit = sk.cypher_literal(evil)
    assert json.loads(lit) == evil
    assert evil not in query  # the raw text (with its bare quote) never appears


def test_cypher_literal_encoding():
    assert sk.cypher_literal('a"b\\c') == '"a\\"b\\\\c"'
    assert sk.cypher_literal("line\nbreak\x00") == '"line\\nbreak "'
    assert sk.cypher_literal(5) == "5" and sk.cypher_literal(True) == "true" and sk.cypher_literal(None) == "null"
    with pytest.raises(TypeError):
        sk.cypher_literal({"a": 1})
    with pytest.raises(ValueError):
        sk.parameterized("RETURN 1", {"bad name": 1})


def test_name_property_comes_from_config_and_is_validated():
    with pytest.raises(sk.ConfigError):
        sk.load_corpora([{"name": "c", "collections": ["a"], "embed_model": "m", "name_property": "n}) DELETE n"}])


# --- search, merge, limits ----------------------------------------------------

def test_search_embeds_with_prefix_and_merges_by_score():
    q = FakeQdrant({"col-a": [pt("a", 0, 0.91), pt("a", 1, 0.5)], "col-b": [pt("b", 7, 0.95), pt("b", 8, 0.5)]})
    client, q, _, e = make(q)
    r = client.post("/search", headers=AUTH, json={"corpus": "example-legal", "query": "find me", "k": 3})
    assert r.status_code == 200, r.text
    hits = r.json()
    assert [(h["collection"], h["chunk_index"]) for h in hits] == [("col-b", 7), ("col-a", 0), ("col-a", 1)]
    assert set(hits[0]) >= {"text", "title", "file_path", "chunk_index", "collection", "score"}
    assert e.calls[0][1] == {"model": "example-embed", "input": ["query: find me"]}
    assert e.calls[0][2] == "Bearer embed-key"


def test_merge_ties_keep_collection_then_rank_order():
    merged = sk.merge_hits([("c1", [pt("x", 1, 0.5), pt("x", 2, 0.5)]), ("c2", [pt("y", 1, 0.5)])], 10)
    assert [(h["collection"], h["chunk_index"]) for h in merged] == [("c1", 1), ("c1", 2), ("c2", 1)]


def test_k_is_capped():
    q = FakeQdrant({"col-a": [pt("a", i, 1 - i / 1000) for i in range(500)]})
    client, q, _, _ = make(q)
    r = client.post("/search", headers=AUTH, json={"corpus": "example-legal", "query": "x", "k": 999})
    assert len(r.json()) == sk.MAX_K
    assert all(body["limit"] == sk.MAX_K for _, _, body in q.calls)


def test_long_text_is_clipped():
    p = pt("a", 0, 0.9)
    p["payload"]["text"] = "z" * (sk.MAX_TEXT_CHARS + 50)
    client, _, _, _ = make(FakeQdrant({"col-a": [p]}))
    text = client.post("/search", headers=AUTH, json={"corpus": "example-legal", "query": "x"}).json()[0]["text"]
    assert len(text) < sk.MAX_TEXT_CHARS + 10 and text.endswith("[...]")


def test_corpus_get_window_is_capped_sorted_and_scoped():
    q = FakeQdrant(points=[pt("a", 6, None), pt("a", 4, None), pt("a", 5, None)])
    client, q, _, _ = make(q)
    r = client.post("/chunks", headers=AUTH, json={"corpus": "example-legal", "collection": "col-a",
                                                  "file_path": "docs/a.md", "chunk_index": 5, "window": 99})
    assert [h["chunk_index"] for h in r.json()] == [4, 5, 6]
    body = q.calls[-1][2]
    rng = body["filter"]["must"][1]["range"]
    assert (rng["gte"], rng["lte"], body["limit"]) == (5 - sk.MAX_WINDOW, 5 + sk.MAX_WINDOW, 2 * sk.MAX_WINDOW + 1)
    # A collection outside the corpus is refused before any call.
    n = len(q.calls)
    r = client.post("/chunks", headers=AUTH, json={"corpus": "example-legal", "collection": "other",
                                                  "file_path": "x", "chunk_index": 0})
    assert r.status_code == 404 and len(q.calls) == n


def test_graph_depth_limit_and_missing_graph():
    client, _, f, _ = make()
    assert client.post("/graph/neighbors", headers=AUTH,
                       json={"corpus": "example-legal", "name": "x", "depth": 3}).status_code == 422
    r = client.post("/graph/neighbors", headers=AUTH, json={"corpus": "example-legal", "name": "x", "depth": 2})
    assert f.commands[-1][2].endswith(sk.NEIGHBORS_2.format(prop="name"))
    assert client.post("/graph/entity", headers=AUTH, json={"corpus": "example-novec", "name": "x"}).status_code == 404


def test_graph_rows_are_capped_and_properties_mapped():
    many = [["n", ["L"], ["k", "v"]]] * (sk.MAX_GRAPH_ROWS + 20)
    client, _, _, _ = make(falkor=FakeFalkorConn([["name", "labels", "properties"], many, []]))
    rows = client.post("/graph/entity", headers=AUTH, json={"corpus": "example-legal", "name": "n"}).json()
    assert len(rows) == sk.MAX_GRAPH_ROWS and rows[0]["properties"] == {"k": "v"}


# --- config, auth -------------------------------------------------------------

def test_empty_corpus_config_is_allowed():
    client, _, _, _ = make(corpora=[])
    r = client.get("/corpora", headers=AUTH)
    assert r.status_code == 200 and r.json() == []
    assert client.post("/search", headers=AUTH, json={"corpus": "example-legal", "query": "x"}).status_code == 404
    assert sk.load_corpora(None) == {}


@pytest.mark.parametrize("bad", [
    {"collections": ["a"], "embed_model": "m"},
    {"name": "c", "collections": [], "embed_model": "m"},
    {"name": "c", "collections": ["a b"], "embed_model": "m"},
    {"name": "c", "collections": ["a"]},
    {"name": "c", "collections": ["a"], "embed_model": "m", "graph": "g h"},
])
def test_bad_corpus_config_is_refused(bad):
    with pytest.raises(sk.ConfigError):
        sk.load_corpora([bad])


def test_duplicate_corpus_is_refused():
    with pytest.raises(sk.ConfigError):
        sk.load_corpora([CORPORA[1], CORPORA[1]])


def test_unknown_corpus_404_and_auth_required():
    client, _, _, _ = make()
    assert client.get("/corpora").status_code == 401
    assert client.get("/corpora", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/search", headers=AUTH, json={"corpus": "nope", "query": "x"}).status_code == 404
    assert client.get("/health").status_code == 200


def test_app_refuses_to_start_without_a_token():
    with pytest.raises(sk.ConfigError):
        sk.create_app(sk.Corpus({}, None, None, None), token="")


def test_openapi_exposes_the_tool_operations():
    client, _, _, _ = make()
    spec = client.get("/openapi.json").json()
    ops = {op["operationId"] for path in spec["paths"].values() for op in path.values()}
    assert ops == {"corpus_list", "corpus_search", "corpus_get", "graph_entity", "graph_neighbors"}


def test_resp_client_against_a_local_fake_server():
    """RespConnection speaks RESP to a socket server on loopback (no external
    network): AUTH first, then the command, reply parsed into lists."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    seen = []

    def serve():
        conn, _ = srv.accept()
        f = conn.makefile("rwb")
        for reply in (b"+OK\r\n", b"*3\r\n*1\r\n$4\r\nname\r\n*1\r\n*1\r\n$1\r\nx\r\n*0\r\n"):
            n = int(f.readline()[1:-2])
            args = []
            for _ in range(n):
                ln = int(f.readline()[1:-2])
                args.append(f.read(ln + 2)[:-2].decode())
            seen.append(args)
            f.write(reply)
            f.flush()
        conn.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    conn = sk.RespConnection("127.0.0.1", srv.getsockname()[1], password="pw")
    reply = sk.FalkorReader(conn).ro_query("example-graph", "RETURN $name AS name", {"name": 'a"b'})
    t.join(5)
    srv.close()
    assert seen[0] == ["AUTH", "pw"]
    assert seen[1] == ["GRAPH.RO_QUERY", "example-graph", 'CYPHER name="a\\"b" RETURN $name AS name']
    assert reply == [{"name": "x"}]
