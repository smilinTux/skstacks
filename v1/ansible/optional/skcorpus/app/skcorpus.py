"""skcorpus: a read-only OpenAPI tool server over vector and graph corpora.

Open WebUI (skai) registers this server as a tool server and calls its
operations as tools. A corpus is configuration, never input: a name, the
Qdrant collections that hold its chunks, an optional FalkorDB graph, and the
embedding model the collections were built with.

Read-only by construction:

* Qdrant is reached through ``QdrantReader``, whose only HTTP calls are
  ``POST /collections/<c>/points/search`` and ``POST /collections/<c>/points/scroll``
  (an allowlist checked on every request, so no other path or method can be
  sent even by a bug). Use a read-only Qdrant API key as well.
* FalkorDB is reached through ``FalkorReader``, whose only command is
  ``GRAPH.RO_QUERY`` (FalkorDB refuses any write inside it). The Cypher texts
  are module constants; user input travels only as query parameters, written
  in the ``CYPHER name=value`` header with a strict literal encoder.
* Collections and graphs come from the corpus config. A request names a
  corpus (and for ``corpus_get`` one of that corpus's collections); anything
  else is refused with 404.

Hard limits: ``k`` up to MAX_K, ``window`` up to MAX_WINDOW, graph rows up to
MAX_GRAPH_ROWS, depth 1 or 2, returned text cut to MAX_TEXT_CHARS.

Environment:
  API_TOKEN          bearer token callers must send (required)
  CORPORA_FILE       JSON list of corpora (default /config/corpora.json)
  QDRANT_URL         e.g. http://qdrant:6333
  QDRANT_API_KEY     read-only key (optional)
  FALKORDB_HOST / FALKORDB_PORT / FALKORDB_USERNAME / FALKORDB_PASSWORD
  EMBED_URL          OpenAI-compatible base URL, e.g. http://skembed:8080/v1
  EMBED_API_KEY      bearer key for the embeddings endpoint (optional)
  MAX_K, MAX_WINDOW, MAX_GRAPH_ROWS, MAX_TEXT_CHARS, TIMEOUT_SECONDS
"""
from __future__ import annotations

import hmac
import json
import math
import os
import re
import socket
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
PROP_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
QDRANT_ALLOWED = re.compile(r"^/collections/[A-Za-z0-9][A-Za-z0-9._-]{0,127}/points/(search|scroll)$")


def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except ValueError:
        value = default
    return max(lo, min(hi, value))


MAX_K = _int_env("MAX_K", 20, 1, 100)
MAX_WINDOW = _int_env("MAX_WINDOW", 3, 0, 10)
MAX_GRAPH_ROWS = _int_env("MAX_GRAPH_ROWS", 50, 1, 500)
MAX_TEXT_CHARS = _int_env("MAX_TEXT_CHARS", 4000, 200, 100000)
TIMEOUT = float(_int_env("TIMEOUT_SECONDS", 20, 1, 120))


class ConfigError(ValueError):
    pass


class ReadOnlyViolation(RuntimeError):
    """A call that is not on the read-only allowlist was attempted."""


# ---------------------------------------------------------------------------
# Corpus config
# ---------------------------------------------------------------------------

def load_corpora(raw: Any) -> dict[str, dict]:
    """Validate the corpora list; returns {name: corpus}. An empty list is fine."""
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ConfigError("corpora must be a list")
    out: dict[str, dict] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise ConfigError("each corpus must be a mapping")
        name = item.get("name")
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise ConfigError(f"corpus name {name!r} is not a plain token")
        if name in out:
            raise ConfigError(f"corpus {name} is defined twice")
        cols = item.get("collections") or []
        if not isinstance(cols, list) or not cols or not all(isinstance(c, str) and NAME_RE.match(c) for c in cols):
            raise ConfigError(f"corpus {name}: collections must be a non-empty list of plain names")
        graph = item.get("graph") or ""
        if graph and (not isinstance(graph, str) or not NAME_RE.match(graph)):
            raise ConfigError(f"corpus {name}: graph must be a plain name")
        model = item.get("embed_model")
        if not isinstance(model, str) or not model.strip():
            raise ConfigError(f"corpus {name}: embed_model is required")
        name_prop = item.get("name_property") or "name"
        if not PROP_RE.match(name_prop):
            raise ConfigError(f"corpus {name}: name_property must be an identifier")
        vector_name = item.get("vector_name") or ""
        if vector_name and not PROP_RE.match(vector_name):
            raise ConfigError(f"corpus {name}: vector_name must be an identifier")
        out[name] = {
            "name": name,
            "description": str(item.get("description") or ""),
            "collections": list(dict.fromkeys(cols)),
            "graph": graph,
            "embed_model": model,
            "query_prefix": str(item.get("query_prefix") or ""),
            "name_property": name_prop,
            "vector_name": vector_name,
        }
    return out


# ---------------------------------------------------------------------------
# Qdrant (HTTP, search and scroll only)
# ---------------------------------------------------------------------------

class QdrantReader:
    def __init__(self, url: str, api_key: str = "", transport: httpx.BaseTransport | None = None):
        headers = {"api-key": api_key} if api_key else {}
        self._client = httpx.Client(base_url=url.rstrip("/"), headers=headers, timeout=TIMEOUT, transport=transport)

    def _post(self, path: str, body: dict) -> dict:
        if not QDRANT_ALLOWED.match(path):
            raise ReadOnlyViolation(f"Qdrant call {path} is not search or scroll")
        r = self._client.post(path, json=body)
        r.raise_for_status()
        return r.json()

    def search(self, collection: str, vector: list[float], limit: int, vector_name: str = "") -> list[dict]:
        body: dict[str, Any] = {"vector": {"name": vector_name, "vector": vector} if vector_name else vector,
                                "limit": limit, "with_payload": True, "with_vector": False}
        return self._post(f"/collections/{collection}/points/search", body).get("result") or []

    def scroll(self, collection: str, flt: dict, limit: int) -> list[dict]:
        body = {"filter": flt, "limit": limit, "with_payload": True, "with_vector": False}
        res = self._post(f"/collections/{collection}/points/scroll", body).get("result") or {}
        return res.get("points") or []


# ---------------------------------------------------------------------------
# FalkorDB (RESP, GRAPH.RO_QUERY only)
# ---------------------------------------------------------------------------

def cypher_literal(value: Any) -> str:
    """Encode a parameter value as a Cypher literal for the CYPHER header.
    Strings are double-quoted with backslash escapes, so a quote or a
    backslash in user input can never end the literal."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite number")
        return repr(value)
    if isinstance(value, str):
        out = []
        for ch in value:
            if ch == "\\":
                out.append("\\\\")
            elif ch == '"':
                out.append('\\"')
            elif ch == "\n":
                out.append("\\n")
            elif ch == "\r":
                out.append("\\r")
            elif ch == "\t":
                out.append("\\t")
            elif ord(ch) < 0x20 or ch == "\x7f":
                out.append(" ")
            else:
                out.append(ch)
        return '"' + "".join(out) + '"'
    raise TypeError(f"unsupported parameter type {type(value).__name__}")


def parameterized(query: str, params: dict[str, Any]) -> str:
    for key in params:
        if not PROP_RE.match(key):
            raise ValueError(f"bad parameter name {key!r}")
    header = " ".join(f"{k}={cypher_literal(v)}" for k, v in params.items())
    return f"CYPHER {header} {query}" if header else query


class RespConnection:
    """The smallest RESP2 client that FalkorDB needs: AUTH, then commands."""

    def __init__(self, host: str, port: int, username: str = "", password: str = ""):
        self.host, self.port, self.username, self.password = host, port, username, password

    def command(self, *args: str) -> Any:
        with socket.create_connection((self.host, self.port), timeout=TIMEOUT) as sock:
            f = sock.makefile("rwb")
            if self.password:
                self._send(f, *(["AUTH", self.username, self.password] if self.username else ["AUTH", self.password]))
                self._read(f)
            self._send(f, *args)
            return self._read(f)

    @staticmethod
    def _send(f, *args: str) -> None:
        parts = [f"*{len(args)}\r\n".encode()]
        for a in args:
            b = a.encode()
            parts.append(b"$%d\r\n%s\r\n" % (len(b), b))
        f.write(b"".join(parts))
        f.flush()

    def _read(self, f) -> Any:
        line = f.readline()
        if not line:
            raise ConnectionError("FalkorDB closed the connection")
        kind, rest = line[:1], line[1:-2]
        if kind == b"+":
            return rest.decode()
        if kind == b"-":
            raise RuntimeError(rest.decode())
        if kind == b":":
            return int(rest)
        if kind == b"$":
            n = int(rest)
            if n < 0:
                return None
            data = f.read(n + 2)[:-2]
            return data.decode(errors="replace")
        if kind == b"*":
            n = int(rest)
            return None if n < 0 else [self._read(f) for _ in range(n)]
        raise RuntimeError(f"unexpected RESP reply {line!r}")


ENTITY_QUERY = (
    "MATCH (n) WHERE toLower(toString(n.{prop})) = toLower($name) "
    "RETURN n.{prop} AS name, labels(n) AS labels, properties(n) AS properties LIMIT $limit"
)
NEIGHBORS_1 = (
    "MATCH (n)-[r]-(m) WHERE toLower(toString(n.{prop})) = toLower($name) "
    "RETURN n.{prop} AS name, type(r) AS relation, m.{prop} AS neighbor, labels(m) AS neighbor_labels LIMIT $limit"
)
NEIGHBORS_2 = (
    "MATCH (n)-[r1]-(m1)-[r2]-(m2) WHERE toLower(toString(n.{prop})) = toLower($name) AND m2 <> n "
    "RETURN n.{prop} AS name, type(r1) AS relation, m1.{prop} AS via, type(r2) AS relation2, "
    "m2.{prop} AS neighbor, labels(m2) AS neighbor_labels LIMIT $limit"
)


class FalkorReader:
    COMMAND = "GRAPH.RO_QUERY"

    def __init__(self, conn: Any):
        self._conn = conn

    def ro_query(self, graph: str, query: str, params: dict[str, Any]) -> list[dict]:
        if not NAME_RE.match(graph):
            raise ValueError("bad graph name")
        reply = self._conn.command(self.COMMAND, graph, parameterized(query, params))
        return rows_from_reply(reply)


def _maybe_map(value: Any) -> Any:
    if isinstance(value, list) and len(value) % 2 == 0 and value and all(isinstance(k, str) for k in value[0::2]):
        return {value[i]: value[i + 1] for i in range(0, len(value), 2)}
    return value


def rows_from_reply(reply: Any) -> list[dict]:
    """Verbose GRAPH.RO_QUERY reply: [header, rows, stats] -> list of dicts."""
    if not isinstance(reply, list) or len(reply) < 2:
        return []
    header = [h[1] if isinstance(h, list) and len(h) == 2 else h for h in reply[0] or []]
    rows = []
    for row in reply[1] or []:
        rec = {}
        for col, val in zip(header, row):
            rec[str(col)] = _maybe_map(val) if col == "properties" else val
        rows.append(rec)
    return rows


# ---------------------------------------------------------------------------
# Embeddings (OpenAI-compatible)
# ---------------------------------------------------------------------------

class Embedder:
    def __init__(self, url: str, api_key: str = "", transport: httpx.BaseTransport | None = None):
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.Client(base_url=url.rstrip("/"), headers=headers, timeout=TIMEOUT, transport=transport)

    def embed(self, model: str, text: str) -> list[float]:
        r = self._client.post("/embeddings", json={"model": model, "input": [text]})
        r.raise_for_status()
        return r.json()["data"][0]["embedding"]


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

def _clip(text: Any) -> str:
    s = "" if text is None else str(text)
    return s if len(s) <= MAX_TEXT_CHARS else s[:MAX_TEXT_CHARS] + " [...]"


def _hit(collection: str, point: dict, score: float | None) -> dict:
    p = point.get("payload") or {}
    return {
        "text": _clip(p.get("text")),
        "title": p.get("title"),
        "file_path": p.get("file_path"),
        "chunk_index": p.get("chunk_index"),
        "category": p.get("category"),
        "collection": collection,
        "score": score,
    }


def merge_hits(per_collection: list[tuple[str, list[dict]]], k: int) -> list[dict]:
    """Merge search results from several collections by score (highest
    first). Ties keep collection order, then each collection's own order."""
    flat = []
    for c_idx, (collection, points) in enumerate(per_collection):
        for p_idx, pt in enumerate(points):
            score = float(pt.get("score") or 0.0)
            flat.append((-score, c_idx, p_idx, _hit(collection, pt, score)))
    flat.sort(key=lambda t: t[:3])
    return [t[3] for t in flat[:k]]


class SearchIn(BaseModel):
    corpus: str = Field(description="Corpus name from corpus_list")
    query: str = Field(min_length=1, max_length=2000, description="What to look for, in natural language")
    k: int = Field(default=8, ge=1, description=f"Number of chunks to return (at most {MAX_K})")


class GetIn(BaseModel):
    corpus: str
    collection: str = Field(description="The collection of a search hit")
    file_path: str = Field(min_length=1, max_length=1024)
    chunk_index: int = Field(ge=0)
    window: int = Field(default=1, ge=0, description=f"Neighbouring chunks on each side (at most {MAX_WINDOW})")


class EntityIn(BaseModel):
    corpus: str
    name: str = Field(min_length=1, max_length=300, description="Entity name, case-insensitive exact match")


class NeighborsIn(EntityIn):
    depth: int = Field(default=1, ge=1, le=2, description="1 or 2 hops")


class Corpus:
    def __init__(self, corpora: dict[str, dict], qdrant: QdrantReader | None, falkor: FalkorReader | None,
                 embedder: Embedder | None):
        self.corpora, self.qdrant, self.falkor, self.embedder = corpora, qdrant, falkor, embedder

    def get(self, name: str) -> dict:
        c = self.corpora.get(name)
        if c is None:
            raise HTTPException(404, f"unknown corpus {name!r}; call corpus_list")
        return c

    def list(self) -> list[dict]:
        return [{"name": c["name"], "description": c["description"], "collections": c["collections"],
                 "has_graph": bool(c["graph"])} for c in self.corpora.values()]

    def search(self, req: SearchIn) -> list[dict]:
        c = self.get(req.corpus)
        if self.qdrant is None or self.embedder is None:
            raise HTTPException(503, "vector search is not configured")
        k = min(req.k, MAX_K)
        vector = self.embedder.embed(c["embed_model"], c["query_prefix"] + req.query)
        per = [(col, self.qdrant.search(col, vector, k, c["vector_name"])) for col in c["collections"]]
        return merge_hits(per, k)

    def chunks(self, req: GetIn) -> list[dict]:
        c = self.get(req.corpus)
        if req.collection not in c["collections"]:
            raise HTTPException(404, f"collection {req.collection!r} is not part of corpus {c['name']}")
        if self.qdrant is None:
            raise HTTPException(503, "vector search is not configured")
        w = min(req.window, MAX_WINDOW)
        flt = {"must": [
            {"key": "file_path", "match": {"value": req.file_path}},
            {"key": "chunk_index", "range": {"gte": max(0, req.chunk_index - w), "lte": req.chunk_index + w}},
        ]}
        points = self.qdrant.scroll(req.collection, flt, 2 * w + 1)
        hits = [_hit(req.collection, p, None) for p in points]
        return sorted(hits, key=lambda h: (h["chunk_index"] is None, h["chunk_index"] or 0))

    def _graph(self, corpus: dict) -> str:
        if not corpus["graph"]:
            raise HTTPException(404, f"corpus {corpus['name']} has no graph")
        if self.falkor is None:
            raise HTTPException(503, "graph search is not configured")
        return corpus["graph"]

    def entity(self, req: EntityIn) -> list[dict]:
        c = self.get(req.corpus)
        g = self._graph(c)
        q = ENTITY_QUERY.format(prop=c["name_property"])
        return self.falkor.ro_query(g, q, {"name": req.name, "limit": MAX_GRAPH_ROWS})[:MAX_GRAPH_ROWS]

    def neighbors(self, req: NeighborsIn) -> list[dict]:
        c = self.get(req.corpus)
        g = self._graph(c)
        q = (NEIGHBORS_1 if req.depth == 1 else NEIGHBORS_2).format(prop=c["name_property"])
        return self.falkor.ro_query(g, q, {"name": req.name, "limit": MAX_GRAPH_ROWS})[:MAX_GRAPH_ROWS]


def create_app(corpus: Corpus | None = None, token: str | None = None) -> FastAPI:
    if token is None:
        token = os.environ.get("API_TOKEN", "")
    if len(token) < 16:
        raise ConfigError("API_TOKEN must be set (16+ characters)")
    if corpus is None:
        path = os.environ.get("CORPORA_FILE", "/config/corpora.json")
        raw = json.loads(open(path, encoding="utf-8").read()) if os.path.exists(path) else []
        corpora = load_corpora(raw)
        q_url = os.environ.get("QDRANT_URL", "")
        e_url = os.environ.get("EMBED_URL", "")
        f_host = os.environ.get("FALKORDB_HOST", "")
        corpus = Corpus(
            corpora,
            QdrantReader(q_url, os.environ.get("QDRANT_API_KEY", "")) if q_url else None,
            FalkorReader(RespConnection(f_host, int(os.environ.get("FALKORDB_PORT", "6379")),
                                        os.environ.get("FALKORDB_USERNAME", ""),
                                        os.environ.get("FALKORDB_PASSWORD", ""))) if f_host else None,
            Embedder(e_url, os.environ.get("EMBED_API_KEY", "")) if e_url else None,
        )

    def auth(request: Request) -> None:
        got = request.headers.get("authorization", "")
        if not hmac.compare_digest(got.encode(), f"Bearer {token}".encode()):
            raise HTTPException(401, "missing or wrong bearer token", headers={"WWW-Authenticate": "Bearer"})

    app = FastAPI(
        title="skcorpus",
        version="1.0.0",
        description=(
            "Read-only search over configured knowledge corpora. Layered retrieval: use graph_entity and "
            "graph_neighbors for who or what connects to what, corpus_search for substance, corpus_get "
            "for the surrounding text of a hit. Always cite title, file_path and chunk_index."
        ),
    )

    @app.get("/health", include_in_schema=False)
    def health() -> dict:
        return {"status": "ok", "service": "skcorpus", "corpora": len(corpus.corpora)}

    @app.get("/corpora", operation_id="corpus_list", dependencies=[Depends(auth)],
             summary="List the corpora that can be searched")
    def corpus_list() -> list[dict]:
        return corpus.list()

    @app.post("/search", operation_id="corpus_search", dependencies=[Depends(auth)],
              summary="Semantic search in a corpus; returns chunks with title, file_path, chunk_index, score")
    def corpus_search(req: SearchIn) -> list[dict]:
        return corpus.search(req)

    @app.post("/chunks", operation_id="corpus_get", dependencies=[Depends(auth)],
              summary="Neighbouring chunks of one search hit (same file, chunk_index +/- window)")
    def corpus_get(req: GetIn) -> list[dict]:
        return corpus.chunks(req)

    @app.post("/graph/entity", operation_id="graph_entity", dependencies=[Depends(auth)],
              summary="Look up an entity by name in the corpus graph")
    def graph_entity(req: EntityIn) -> list[dict]:
        return corpus.entity(req)

    @app.post("/graph/neighbors", operation_id="graph_neighbors", dependencies=[Depends(auth)],
              summary="Entities connected to a named entity (depth 1 or 2)")
    def graph_neighbors(req: NeighborsIn) -> list[dict]:
        return corpus.neighbors(req)

    return app


def app() -> FastAPI:  # uvicorn --factory skcorpus:app
    return create_app()
