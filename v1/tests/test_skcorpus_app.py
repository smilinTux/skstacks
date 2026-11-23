"""skcorpus (v1/ansible/optional/skcorpus/app/skcorpus.py) against a fake
Qdrant, a fake embeddings endpoint, a fake FalkorDB and a fake Postgres
connection (pgvector corpora), no network:

* read-only guarantee: the only Qdrant calls are search and scroll, any
  other path raises before a request is sent; the only FalkorDB command is
  GRAPH.RO_QUERY;
* Cypher parameterization: user input reaches FalkorDB only as an encoded
  parameter, so an injection attempt stays inside a string literal;
* merge ordering across collections, hard limits, corpus scoping, auth;
* per-corpus servers at /c/<name>/ that cannot reach another corpus, and
  per-corpus embedding endpoints;
* the coherence check: sampled points re-embedded, the fraction above the
  cosine floor on /health, /metrics and as a warning on tool results;
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
    assert public == {"search", "scroll", "sample"}
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


# --- per-corpus servers ------------------------------------------------------

def test_scoped_server_serves_one_corpus_only():
    client, q, f, _ = make(FakeQdrant({"col-a": [pt("a", 0, 0.9)], "col-c": [pt("c", 0, 0.8)]}))
    r = client.post("/c/example-legal/search", headers=AUTH, json={"query": "x"})
    assert r.status_code == 200 and {h["collection"] for h in r.json()} == {"col-a"}
    # another corpus's collection is not reachable through this corpus's server
    assert client.post("/c/example-legal/chunks", headers=AUTH,
                       json={"collection": "col-c", "file_path": "docs/c.md", "chunk_index": 0}).status_code == 404
    # the corpus argument is not part of the scoped API, so it cannot be redirected
    r = client.post("/c/example-legal/search", headers=AUTH, json={"query": "x", "corpus": "example-novec"})
    assert {h["collection"] for h in r.json()} == {"col-a"}
    assert {p.split("/")[2] for _, p, _ in q.calls} == {"col-a", "col-b"}
    assert client.post("/c/example-legal/search", json={"query": "x"}).status_code == 401
    assert client.post("/c/nope/search", headers=AUTH, json={"query": "x"}).status_code == 404


def test_scoped_openapi_has_no_corpus_argument_and_graph_only_when_configured():
    client, _, _, _ = make()
    spec = client.get("/c/example-legal/openapi.json").json()
    ops = {op["operationId"] for path in spec["paths"].values() for op in path.values()}
    assert ops == {"corpus_list", "corpus_search", "corpus_get", "graph_entity", "graph_neighbors"}
    assert "corpus" not in json.dumps(spec["components"]["schemas"]["ScopedSearchIn"])
    spec2 = client.get("/c/example-novec/openapi.json").json()
    assert {op["operationId"] for path in spec2["paths"].values() for op in path.values()} == {
        "corpus_list", "corpus_search", "corpus_get"}


def test_global_routes_can_be_switched_off():
    c = sk.Corpus(sk.load_corpora(CORPORA), None, None, None)
    client = TestClient(sk.create_app(c, token=TOKEN, global_routes=False))
    assert client.post("/search", headers=AUTH, json={"corpus": "example-legal", "query": "x"}).status_code == 404
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/c/example-legal/openapi.json").status_code == 200
    assert client.get("/health").status_code == 200


def test_corpus_embed_url_overrides_the_default_endpoint():
    default, own = FakeEmbed(), FakeEmbed()
    corpora = [dict(CORPORA[0], embed_url="http://router.invalid/v1", allowed_groups=["example-users"]), CORPORA[1]]
    made = []

    def embedder_for(url):
        made.append(url)
        return sk.Embedder(url, transport=httpx.MockTransport(own.handler))

    c = sk.Corpus(sk.load_corpora(corpora),
                  sk.QdrantReader("http://qdrant.invalid:6333", transport=httpx.MockTransport(FakeQdrant().handler)),
                  None, sk.Embedder("http://embed.invalid/v1", transport=httpx.MockTransport(default.handler)),
                  embedder_for)
    client = TestClient(sk.create_app(c, token=TOKEN))
    client.post("/c/example-legal/search", headers=AUTH, json={"query": "x"})
    client.post("/c/example-novec/search", headers=AUTH, json={"query": "y"})
    assert made == ["http://router.invalid/v1"] and len(own.calls) == 1 and len(default.calls) == 1
    assert sk.load_corpora(corpora)["example-legal"]["allowed_groups"] == ["example-users"]


@pytest.mark.parametrize("bad", [{"embed_url": "ftp://x"}, {"embed_url": "http://a b"},
                                 {"allowed_groups": "example-users"}, {"allowed_groups": ["a,b"]},
                                 {"coherence_threshold": 1.5}, {"coherence_threshold": True}])
def test_bad_new_corpus_fields_are_refused(bad):
    with pytest.raises(sk.ConfigError):
        sk.load_corpora([dict(CORPORA[1], **bad)])


# --- coherence check ---------------------------------------------------------

GOOD = [1.0, 0.0, 0.0]


class VecQdrant(FakeQdrant):
    """Points whose stored vectors either match what the fake embedder returns
    for their text (coherent) or point elsewhere (made by another model)."""

    def __init__(self, coherent, polluted, query_ok=True, named=""):
        super().__init__()
        self.query_ok, self.named = query_ok, named
        pts = [("good", GOOD)] * coherent + [("bad", [0.0, 1.0, 0.0])] * polluted
        self.sample_points = [{"id": i, "payload": {"text": f"{kind} {i}"},
                               "vector": {named: v} if named else v} for i, (kind, v) in enumerate(pts)]

    def handler(self, request):
        path = request.url.path
        if path.endswith("/points/query"):
            self.calls.append((request.method, path, json.loads(request.content)))
            if not self.query_ok:
                return httpx.Response(404, json={"status": "not found"})
            return httpx.Response(200, json={"result": {"points": self.sample_points}})
        if path.endswith("/points/scroll") and "filter" not in json.loads(request.content):
            self.calls.append((request.method, path, json.loads(request.content)))
            return httpx.Response(200, json={"result": {"points": self.sample_points}})
        return super().handler(request)


class TextEmbed(FakeEmbed):
    def handler(self, request):
        self.calls.append((request.url.path, json.loads(request.content), None))
        return httpx.Response(200, json={"data": [{"embedding": GOOD}]})


def coherence_client(qdrant, corpora=None, threshold=None):
    corpora = corpora or [dict(CORPORA[1], **({"coherence_threshold": threshold} if threshold is not None else {}))]
    embed = TextEmbed()
    c = sk.Corpus(sk.load_corpora(corpora),
                  sk.QdrantReader("http://qdrant.invalid:6333", transport=httpx.MockTransport(qdrant.handler)),
                  None, sk.Embedder("http://embed.invalid/v1", transport=httpx.MockTransport(embed.handler)))
    return TestClient(sk.create_app(c, token=TOKEN)), c, embed


def test_coherent_corpus_is_healthy():
    client, c, embed = coherence_client(VecQdrant(10, 0))
    c.coherence.run_once()
    h = client.get("/health").json()
    assert h["status"] == "ok" and h["coherence"]["example-novec"]["ratio"] == 1.0
    assert h["coherence"]["example-novec"]["sampled"] == 10
    assert [b["input"] for _, b, _ in embed.calls][0] == ["good 0"]
    r = client.post("/c/example-novec/search", headers=AUTH, json={"query": "x"})
    assert all("warning" not in hit for hit in r.json())


def test_polluted_corpus_is_degraded_but_keeps_serving_with_a_warning():
    q = VecQdrant(7, 3)
    q.results = {"col-c": [pt("c", 0, 0.5)]}
    client, c, _ = coherence_client(q)
    c.coherence.run_once()
    h = client.get("/health")
    assert h.status_code == 200 and h.json()["status"] == "degraded"
    st = h.json()["coherence"]["example-novec"]
    assert st["ratio"] == pytest.approx(0.7) and st["healthy"] is False and st["threshold"] == 0.9
    hits = client.post("/c/example-novec/search", headers=AUTH, json={"query": "x"}).json()
    assert hits and "failed its coherence check" in hits[0]["warning"] and "0.70" in hits[0]["warning"]
    listed = client.get("/c/example-novec/corpora", headers=AUTH).json()[0]
    assert listed["coherence"] == {"healthy": False, "ratio": pytest.approx(0.7)} and listed["warning"]
    m = client.get("/metrics").text
    assert 'skcorpus_coherence_ratio{corpus="example-novec",collection="col-c"} 0.700000' in m
    assert 'skcorpus_coherence_healthy{corpus="example-novec"} 0' in m
    assert 'skcorpus_coherence_sampled{corpus="example-novec"} 10' in m


def test_threshold_is_configurable_per_corpus():
    client, c, _ = coherence_client(VecQdrant(7, 3), threshold=0.6)
    c.coherence.run_once()
    assert client.get("/health").json()["status"] == "ok"


def test_coherence_uses_random_sample_then_falls_back_to_scroll():
    q = VecQdrant(4, 0)
    _, c, _ = coherence_client(q)
    c.coherence.run_once()
    assert q.calls[0][1].endswith("/points/query") and q.calls[0][2]["query"] == {"sample": "random"}
    assert q.calls[0][2]["with_vector"] is True and q.calls[0][2]["limit"] == sk.COHERENCE_SAMPLE
    q2 = VecQdrant(4, 0, query_ok=False)
    _, c2, _ = coherence_client(q2)
    c2.coherence.run_once()
    assert [p.rsplit("/", 1)[1] for _, p, _ in q2.calls] == ["query", "scroll"]
    assert c2.coherence.status["example-novec"]["healthy"] is True


def test_named_vector_and_document_prefix():
    q = VecQdrant(3, 0, named="dense")
    _, c, embed = coherence_client(q, corpora=[dict(CORPORA[1], vector_name="dense", document_prefix="passage: ")])
    c.coherence.run_once()
    assert q.calls[0][2]["with_vector"] == ["dense"]
    assert embed.calls[0][1]["input"] == ["passage: good 0"]
    assert c.coherence.status["example-novec"]["ratio"] == 1.0


def test_a_failed_check_is_reported_not_raised():
    class Down(FakeQdrant):
        def handler(self, request):
            return httpx.Response(503, json={"status": "down"})
    client, c, _ = coherence_client(Down())
    c.coherence.run_once()
    h = client.get("/health").json()
    assert h["status"] == "degraded" and h["coherence"]["example-novec"]["errors"] == ["col-c"]
    assert h["coherence"]["example-novec"]["ratio"] is None


def test_points_without_text_or_vector_are_skipped():
    q = VecQdrant(2, 0)
    q.sample_points += [{"id": 98, "payload": {}, "vector": GOOD}, {"id": 99, "payload": {"text": "t"}}]
    _, c, _ = coherence_client(q)
    c.coherence.run_once()
    assert c.coherence.status["example-novec"]["sampled"] == 2


def test_coherence_check_is_read_only():
    q = VecQdrant(5, 5)
    _, c, _ = coherence_client(q)
    c.coherence.run_once()
    assert {p.rsplit("/", 1)[1] for _, p, _ in q.calls} <= {"query", "scroll", "search"}


def test_background_check_runs_at_startup():
    c = sk.Corpus(sk.load_corpora([CORPORA[1]]),
                  sk.QdrantReader("http://qdrant.invalid:6333", transport=httpx.MockTransport(VecQdrant(3, 0).handler)),
                  None, sk.Embedder("http://embed.invalid/v1", transport=httpx.MockTransport(TextEmbed().handler)))
    with TestClient(sk.create_app(c, token=TOKEN, coherence_interval=0)) as client:
        for _ in range(100):
            if c.coherence.status:
                break
            threading.Event().wait(0.02)
        assert client.get("/health").json()["coherence"]["example-novec"]["healthy"] is True


def test_cosine():
    assert sk.cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert sk.cosine([1, 0], [0, 1]) == pytest.approx(0.0)
    assert sk.cosine([1, 0], [1, 0, 0]) is None and sk.cosine([0, 0], [1, 0]) is None


# --- pgvector backend (fake psycopg connection, no Postgres) ---------------

PG_CORPUS = {"name": "example-wiki", "backend": "pgvector", "pg_dsn_env": "EXAMPLE_WIKI_DSN",
             "pg_filter_values": ["wiki", "wiki-raw"], "embed_model": "example-embed",
             "query_prefix": "query: ", "document_prefix": "doc: "}


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class FakePgConn:
    """Records every statement; answers by template shape. ``rows`` holds
    (id, corpus, source, chunk_idx, content, meta, embedding)."""

    def __init__(self, rows=None, hybrid=None, read_only="on"):
        self.statements = []
        self.rows = rows or []
        self.hybrid = hybrid or []
        self.read_only = read_only
        self.closed = False
        self.broken = False

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        if sql == "SHOW transaction_read_only":
            return FakeCursor([(self.read_only,)])
        if sql.startswith("SET "):
            return FakeCursor([])
        if " FROM \"hybrid_search_docs_public\"(" in sql or "AS h" in sql:
            return FakeCursor(self.hybrid[: params["k"]])
        if "ANY(%(ids)s)" in sql:
            return FakeCursor([r[:6] for r in self.rows if r[0] in params["ids"] and r[1] in params["vals"]])
        if "ORDER BY random()" in sql:
            return FakeCursor([(r[4], json.dumps(r[6])) for r in self.rows if r[1] == params["val"]][: params["n"]])
        if "BETWEEN" in sql:
            return FakeCursor(sorted([r[:6] for r in self.rows if r[1] == params["val"] and r[2] == params["source"]
                                      and params["lo"] <= r[3] <= params["hi"]], key=lambda r: r[3])[: params["n"]])
        if "<=>" in sql:
            return FakeCursor([r[:6] + (0.5,) for r in self.rows if r[1] in params["vals"]][: params["k"]])
        raise AssertionError(f"unexpected SQL {sql}")

    def close(self):
        self.closed = True


def pgrow(i, corpus, source="docs/a.md", idx=0, emb=(0.1, 0.2, 0.3)):
    return (i, corpus, source, idx, f"content {i}", {"title": f"title {i}", "category": "example"}, list(emb))


def pg_make(conn, corpus=PG_CORPUS, embed=None):
    embed = embed or FakeEmbed()
    cfg = sk.load_corpora([corpus])
    connects = []

    def connect(dsn):
        connects.append(dsn)
        return conn

    readers = {}

    def pg_for(c):
        if c["name"] not in readers:
            readers[c["name"]] = sk.PgReader(c, "postgresql://example.invalid/db", connect=connect)
        return readers[c["name"]]

    c = sk.Corpus(cfg, None, None,
                  sk.Embedder("http://embed.invalid/v1", transport=httpx.MockTransport(embed.handler)), pg_for=pg_for)
    return TestClient(sk.create_app(c, token=TOKEN)), c, embed, connects


def test_pgvector_config_defaults_and_collections_are_the_filter_values():
    c = sk.load_corpora([PG_CORPUS])["example-wiki"]
    assert c["backend"] == "pgvector" and c["collections"] == ["wiki", "wiki-raw"]
    assert c["pg_relation"] == "docs_public" and c["pg_search_fn"] == "hybrid_search_docs_public"
    assert c["pg_filter_column"] == "corpus" and c["graph"] == ""
    assert sk.load_corpora([{**PG_CORPUS, "pg_search_fn": ""}])["example-wiki"]["pg_search_fn"] == ""
    assert sk.load_corpora([{**PG_CORPUS, "pg_relation": "public.docs_v2"}])["example-wiki"]["pg_relation"] == "public.docs_v2"
    assert sk.load_corpora([{"name": "q", "collections": ["a"], "embed_model": "m"}])["q"]["backend"] == "qdrant"


@pytest.mark.parametrize("bad", [
    {"backend": "chroma"},
    {"pg_dsn_env": None},
    {"pg_dsn_env": "API_TOKEN"},
    {"pg_dsn_env": "wiki_dsn"},
    {"pg_relation": 'docs"; DROP TABLE x; --'},
    {"pg_relation": "Docs"},
    {"pg_relation": "a.b.c"},
    {"pg_search_fn": "fn()"},
    {"pg_search_fn": 7},
    {"pg_filter_column": "public.corpus"},
    {"pg_filter_column": "corpus or 1=1"},
    {"pg_filter_values": []},
    {"pg_filter_values": None},
    {"pg_filter_values": ["wiki", "x y"]},
    {"pg_filter_values": "wiki"},
    {"graph": "example-graph"},
    {"collections": ["col-a"]},
])
def test_bad_pgvector_config_is_refused(bad):
    with pytest.raises(sk.ConfigError):
        sk.load_corpora([{**PG_CORPUS, **bad}])


def test_qdrant_corpus_still_needs_collections():
    with pytest.raises(sk.ConfigError):
        sk.load_corpora([{"name": "q", "backend": "qdrant", "embed_model": "m"}])


def test_pg_session_is_read_only_with_a_statement_timeout_before_any_query():
    conn = FakePgConn(rows=[pgrow(1, "wiki")], hybrid=[(1, "wiki", 0.9)])
    client, _, _, connects = pg_make(conn)
    assert client.post("/c/example-wiki/search", headers=AUTH, json={"query": "x"}).status_code == 200
    sqls = [s for s, _ in conn.statements]
    assert sqls[:4] == ["SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY",
                        "SET default_transaction_read_only = on",
                        "SET statement_timeout = 5000",
                        "SHOW transaction_read_only"]
    assert all(s.startswith(("SET ", "SHOW ", "SELECT ")) for s in sqls)
    # the connection is reused, set up once
    client.post("/c/example-wiki/search", headers=AUTH, json={"query": "y"})
    assert connects == ["postgresql://example.invalid/db"] and sqls.count("SHOW transaction_read_only") == 1


def test_pg_session_that_is_not_read_only_is_refused_and_closed():
    conn = FakePgConn(read_only="off")
    reader = sk.PgReader(sk.load_corpora([PG_CORPUS])["example-wiki"], "postgresql://example.invalid/db",
                         connect=lambda dsn: conn)
    with pytest.raises(sk.ReadOnlyViolation):
        reader.search("x", [0.1, 0.2], 3)
    assert conn.closed and not any(s.startswith("SELECT") for s, _ in conn.statements)


def test_pg_reader_refuses_sql_that_is_not_a_template():
    conn = FakePgConn()
    reader = sk.PgReader(sk.load_corpora([PG_CORPUS])["example-wiki"], "postgresql://example.invalid/db",
                         connect=lambda dsn: conn)
    for sql in ("DELETE FROM docs_public", "SELECT 1", "SET default_transaction_read_only = off"):
        with pytest.raises(sk.ReadOnlyViolation):
            reader._execute(conn, sql, {})
    assert conn.statements == []
    assert {n for n in dir(sk.PgReader) if not n.startswith("_")} == {"search", "window", "sample"}


def test_pg_reader_without_a_dsn_fails_without_connecting():
    reader = sk.PgReader(sk.load_corpora([PG_CORPUS])["example-wiki"], "",
                         connect=lambda dsn: pytest.fail("must not connect"))
    with pytest.raises(RuntimeError, match="DSN"):
        reader.sample("wiki", 3)


def test_pg_hybrid_search_over_fetches_filters_and_keeps_score_order():
    hybrid = [(1, "other", 0.99), (2, "wiki", 0.9), (3, "wiki-raw", 0.95), (4, "other", 0.8), (5, "wiki", 0.7)]
    rows = [pgrow(i, c) for i, c, _ in hybrid]
    conn = FakePgConn(rows=rows, hybrid=hybrid)
    client, _, embed, _ = pg_make(conn)
    r = client.post("/c/example-wiki/search", headers=AUTH, json={"query": "what is x", "k": 2})
    assert r.status_code == 200
    hits = r.json()
    assert [h["text"] for h in hits] == ["content 3", "content 2"]
    assert [h["score"] for h in hits] == [0.95, 0.9]
    assert hits[0] == {"text": "content 3", "title": "title 3", "file_path": "docs/a.md", "chunk_index": 0,
                       "category": "example", "collection": "wiki-raw", "score": 0.95}
    hy = next(p for s, p in conn.statements if "AS h" in s)
    assert hy["k"] == 8 and hy["q_text"] == "what is x" and hy["q_vec"] == "[0.1,0.2,0.3]"
    by_ids = next((s, p) for s, p in conn.statements if "ANY(%(ids)s)" in s)
    assert by_ids[1] == {"ids": [3, 2], "vals": ["wiki", "wiki-raw"]}
    assert '"docs_public"' in by_ids[0] and "what is x" not in by_ids[0]
    assert embed.calls[0][1]["input"] == ["query: what is x"]


def test_pg_over_fetch_is_capped():
    conn = FakePgConn()
    reader = sk.PgReader(sk.load_corpora([PG_CORPUS])["example-wiki"], "postgresql://example.invalid/db",
                         connect=lambda dsn: conn)
    assert reader.search("x", [0.1], 100) == []
    assert next(p for s, p in conn.statements if "AS h" in s)["k"] == sk.PG_MAX_FETCH
    assert not any("ANY(%(ids)s)" in s for s, _ in conn.statements)  # nothing kept, no second query


def test_pg_pure_vector_fallback_when_the_search_function_is_empty():
    conn = FakePgConn(rows=[pgrow(1, "wiki"), pgrow(2, "other"), pgrow(3, "wiki-raw")])
    client, _, _, _ = pg_make(conn, {**PG_CORPUS, "pg_search_fn": "", "pg_relation": "public.docs_v2",
                                     "pg_filter_column": "kind"})
    hits = client.post("/c/example-wiki/search", headers=AUTH, json={"query": "x", "k": 5}).json()
    assert [h["text"] for h in hits] == ["content 1", "content 3"] and hits[0]["score"] == 0.5
    sql, params = next((s, p) for s, p in conn.statements if "<=>" in s)
    assert '"public"."docs_v2"' in sql and 'r."kind" = ANY(%(vals)s)' in sql
    assert params == {"q_vec": "[0.1,0.2,0.3]", "vals": ["wiki", "wiki-raw"], "k": 5}
    assert not any("AS h" in s for s, _ in conn.statements)


def test_pg_window_by_source_and_chunk_range_scoped_to_the_corpus():
    rows = [pgrow(i, "wiki", "docs/a.md", i) for i in range(10)] + [pgrow(99, "wiki", "docs/b.md", 4)]
    conn = FakePgConn(rows=rows)
    client, _, _, _ = pg_make(conn)
    r = client.post("/c/example-wiki/chunks", headers=AUTH,
                    json={"collection": "wiki", "file_path": "docs/a.md", "chunk_index": 4, "window": 9})
    assert [h["chunk_index"] for h in r.json()] == [1, 2, 3, 4, 5, 6, 7]
    params = next(p for s, p in conn.statements if "BETWEEN" in s)
    assert params == {"val": "wiki", "source": "docs/a.md", "lo": 1, "hi": 7, "n": 7}
    r = client.post("/c/example-wiki/chunks", headers=AUTH,
                    json={"collection": "other", "file_path": "docs/a.md", "chunk_index": 0})
    assert r.status_code == 404


def test_pg_corpus_has_no_graph_tools():
    client, _, _, _ = pg_make(FakePgConn())
    ops = {op["operationId"] for p in client.get("/c/example-wiki/openapi.json").json()["paths"].values()
           for op in p.values()}
    assert ops == {"corpus_list", "corpus_search", "corpus_get"}
    desc = client.get("/c/example-wiki/corpora", headers=AUTH).json()[0]
    assert desc["backend"] == "pgvector" and desc["collections"] == ["wiki", "wiki-raw"] and desc["has_graph"] is False


def test_pg_coherence_samples_each_filter_value():
    rows = [pgrow(1, "wiki", emb=(1.0, 0.0, 0.0)), pgrow(2, "wiki", emb=(0.0, 1.0, 0.0)),
            pgrow(3, "wiki-raw", emb=(0.0, 1.0, 0.0))]
    conn = FakePgConn(rows=rows)

    class Embed(FakeEmbed):
        def handler(self, request):
            self.calls.append(json.loads(request.content)["input"][0])
            return httpx.Response(200, json={"data": [{"embedding": [0.0, 1.0, 0.0]}]})

    embed = Embed()
    client, c, _, _ = pg_make(conn, embed=embed)
    c.coherence.run_once()
    st = c.coherence.status["example-wiki"]
    assert st["sampled"] == 3 and st["coherent"] == 2 and st["healthy"] is False
    assert st["collections"]["wiki"]["ratio"] == 0.5 and st["collections"]["wiki-raw"]["ratio"] == 1.0
    assert embed.calls == ["doc: content 1", "doc: content 2", "doc: content 3"]
    assert {p["val"] for s, p in conn.statements if "random()" in s} == {"wiki", "wiki-raw"}
    assert client.get("/health").json()["status"] == "degraded"


def test_pg_coherence_without_postgres_is_reported_not_raised():
    cfg = sk.load_corpora([PG_CORPUS])
    c = sk.Corpus(cfg, None, None, sk.Embedder("http://embed.invalid/v1", transport=httpx.MockTransport(FakeEmbed().handler)))
    c.coherence.run_once()
    st = c.coherence.status["example-wiki"]
    assert st["healthy"] is False and "error" in st["collections"]["wiki"]


def test_vector_literal_and_parse():
    assert sk.vector_literal([1, 0.5]) == "[1.0,0.5]"
    for bad in ([], [float("nan")], ["1"], [True]):
        with pytest.raises(ValueError):
            sk.vector_literal(bad)
    assert sk._parse_vector("[0.5,1]") == [0.5, 1.0] and sk._parse_vector("nope") is None
    assert sk._parse_vector(None) is None


def test_env_factory_reads_the_dsn_from_the_named_variable(tmp_path, monkeypatch):
    cfg = tmp_path / "corpora.json"
    cfg.write_text(json.dumps([PG_CORPUS]))
    monkeypatch.setenv("CORPORA_FILE", str(cfg))
    monkeypatch.setenv("EMBED_URL", "http://embed.invalid/v1")
    monkeypatch.setenv("EXAMPLE_WIKI_DSN", "postgresql://example.invalid/db")
    monkeypatch.delenv("QDRANT_URL", raising=False)
    monkeypatch.delenv("FALKORDB_HOST", raising=False)
    built = []

    class Recorder:
        def __init__(self, c, dsn, **kw):
            built.append((c["name"], dsn))

        def search(self, query, vector, k):
            return []

    monkeypatch.setattr(sk, "PgReader", Recorder)
    monkeypatch.setattr(sk.Embedder, "embed", lambda self, model, text: [0.1, 0.2])
    client = TestClient(sk.create_app(token=TOKEN))
    for _ in range(2):
        assert client.post("/c/example-wiki/search", headers=AUTH, json={"query": "x"}).json() == []
    assert built == [("example-wiki", "postgresql://example.invalid/db")]
