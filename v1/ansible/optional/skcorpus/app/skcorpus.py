"""skcorpus: a read-only OpenAPI tool server over vector and graph corpora.

Open WebUI (skai) registers this server as a tool server and calls its
operations as tools. A corpus is configuration, never input: a name, the
Qdrant collections that hold its chunks, an optional FalkorDB graph, and the
embedding model the collections were built with.

Read-only by construction:

* Qdrant is reached through ``QdrantReader``, whose only HTTP calls are
  ``POST /collections/<c>/points/search``, ``.../points/scroll`` and
  ``.../points/query`` (random sampling for the coherence check): an
  allowlist checked on every request, so no other path or method can be sent
  even by a bug. Use a read-only Qdrant API key as well. ``<c>`` may be a
  collection or an alias; Qdrant resolves aliases itself, so a corpus that
  names an alias follows it when the alias is moved.
* FalkorDB is reached through ``FalkorReader``, whose only command is
  ``GRAPH.RO_QUERY`` (FalkorDB refuses any write inside it). The Cypher texts
  are module constants; user input travels only as query parameters, written
  in the ``CYPHER name=value`` header with a strict literal encoder.
* Collections and graphs come from the corpus config. A request names a
  corpus (and for ``corpus_get`` one of that corpus's collections); anything
  else is refused with 404.

Scoping: every corpus is also served on its own at ``/c/<name>/`` with its own
OpenAPI spec (``/c/<name>/openapi.json``) whose operations take no corpus
argument. skai registers one tool server per corpus on that prefix, so a
model given corpus A's tools cannot reach corpus B, and Open WebUI's access
grants on each connection decide who may use which corpus. The all-corpora
routes (``/search`` and friends, taking ``corpus``) exist only with
``GLOBAL_ROUTES=true``.

Coherence check: at startup and every COHERENCE_INTERVAL_SECONDS the server
samples COHERENCE_SAMPLE points per collection, re-embeds their ``text``
payload with the corpus's model and counts the points whose stored vector
has cosine above COHERENCE_MIN_COSINE with the fresh one. A corpus whose
coherent fraction is below its threshold (COHERENCE_THRESHOLD, default 0.9)
is reported unhealthy on ``/health`` and ``/metrics``, and its tool results
carry a ``warning``; it keeps serving.

Hard limits: ``k`` up to MAX_K, ``window`` up to MAX_WINDOW, graph rows up to
MAX_GRAPH_ROWS, depth 1 or 2, returned text cut to MAX_TEXT_CHARS.

Environment:
  API_TOKEN          bearer token callers must send (required)
  CORPORA_FILE       JSON list of corpora (default /config/corpora.json)
  QDRANT_URL         e.g. http://qdrant:6333
  QDRANT_API_KEY     read-only key (optional)
  FALKORDB_HOST / FALKORDB_PORT / FALKORDB_USERNAME / FALKORDB_PASSWORD
  EMBED_URL          OpenAI-compatible base URL, e.g. http://skembed:8080/v1 (a
                     corpus's own ``embed_url`` overrides it)
  EMBED_API_KEY      bearer key for the embeddings endpoints (optional)
  GLOBAL_ROUTES      true to also serve the all-corpora routes (default false)
  COHERENCE_SAMPLE, COHERENCE_INTERVAL_SECONDS (0 = startup only),
  COHERENCE_MIN_COSINE, COHERENCE_THRESHOLD
  MAX_K, MAX_WINDOW, MAX_GRAPH_ROWS, MAX_TEXT_CHARS, TIMEOUT_SECONDS
"""
from __future__ import annotations

import hmac
import json
import math
import os
import re
import socket
import threading
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
PROP_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
QDRANT_ALLOWED = re.compile(r"^/collections/[A-Za-z0-9][A-Za-z0-9._-]{0,127}/points/(search|scroll|query)$")
URL_RE = re.compile(r"^https?://[^\s/?#]+(/[^\s?#]*)?$")
GROUP_RE = re.compile(r"^(\*|[^\s,][^,]{0,127})$")


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


def _float_env(name: str, default: float, lo: float, hi: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except ValueError:
        value = default
    return max(lo, min(hi, value)) if math.isfinite(value) else default


COHERENCE_SAMPLE = _int_env("COHERENCE_SAMPLE", 32, 1, 1000)
COHERENCE_INTERVAL = _int_env("COHERENCE_INTERVAL_SECONDS", 3600, 0, 7 * 86400)
COHERENCE_MIN_COSINE = _float_env("COHERENCE_MIN_COSINE", 0.99, 0.0, 1.0)
COHERENCE_THRESHOLD = _float_env("COHERENCE_THRESHOLD", 0.9, 0.0, 1.0)


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
        embed_url = item.get("embed_url") or ""
        if embed_url and (not isinstance(embed_url, str) or not URL_RE.match(embed_url)):
            raise ConfigError(f"corpus {name}: embed_url must be an http(s) URL")
        groups = item.get("allowed_groups")
        groups = [] if groups is None else groups
        if not isinstance(groups, list) or not all(isinstance(g, str) and GROUP_RE.match(g) for g in groups):
            raise ConfigError(f"corpus {name}: allowed_groups must be a list of group names")
        threshold = item.get("coherence_threshold")
        if threshold is not None and (isinstance(threshold, bool) or not isinstance(threshold, (int, float))
                                      or not 0.0 <= float(threshold) <= 1.0):
            raise ConfigError(f"corpus {name}: coherence_threshold must be a number from 0 to 1")
        out[name] = {
            "name": name,
            "title": str(item.get("title") or name),
            "description": str(item.get("description") or ""),
            "embed_url": embed_url,
            "document_prefix": str(item.get("document_prefix") or ""),
            "allowed_groups": list(dict.fromkeys(groups)),
            "coherence_threshold": COHERENCE_THRESHOLD if threshold is None else float(threshold),
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
            raise ReadOnlyViolation(f"Qdrant call {path} is not search, scroll or query")
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

    def sample(self, collection: str, limit: int, vector_name: str = "") -> list[dict]:
        """Random points with their text and stored vector (Qdrant 1.11+
        ``sample: random``); older servers fall back to the first points of a
        scroll, which is a weaker sample but still a check."""
        with_vector: Any = [vector_name] if vector_name else True
        try:
            res = self._post(f"/collections/{collection}/points/query",
                             {"query": {"sample": "random"}, "limit": limit,
                              "with_payload": ["text"], "with_vector": with_vector}).get("result") or {}
            return res.get("points") or []
        except httpx.HTTPStatusError as e:
            if e.response.status_code not in (400, 404, 405, 422):
                raise
        body = {"limit": limit, "with_payload": ["text"], "with_vector": with_vector}
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
# Coherence check (were the stored vectors made by this corpus's model?)
# ---------------------------------------------------------------------------

def cosine(a: list[float], b: list[float]) -> float | None:
    if not a or not b or len(a) != len(b):
        return None
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return None if na == 0 or nb == 0 else dot / (na * nb)


def _stored_vector(point: dict, vector_name: str) -> list[float] | None:
    v = point.get("vector")
    if isinstance(v, dict):
        v = v.get(vector_name) if vector_name else (next(iter(v.values()), None) if len(v) == 1 else None)
    return v if isinstance(v, list) and v and all(isinstance(x, (int, float)) for x in v) else None


class Coherence:
    """Samples each corpus, re-embeds the sampled text and keeps the result.
    ``status[name]`` is replaced whole on every run, so readers never see a
    half-written entry."""

    def __init__(self, corpus: "Corpus", sample: int = COHERENCE_SAMPLE, min_cosine: float = COHERENCE_MIN_COSINE):
        self.corpus, self.sample, self.min_cosine = corpus, sample, min_cosine
        self.status: dict[str, dict] = {}

    def check(self, c: dict) -> dict:
        cols, coherent, sampled, errors = {}, 0, 0, []
        for col in c["collections"]:
            entry = {"sampled": 0, "coherent": 0, "ratio": None}
            try:
                if self.corpus.qdrant is None:
                    raise RuntimeError("vector search is not configured")
                embedder = self.corpus.embedder_of(c)
                for point in self.corpus.qdrant.sample(col, self.sample, c["vector_name"]):
                    text = (point.get("payload") or {}).get("text")
                    stored = _stored_vector(point, c["vector_name"])
                    if not isinstance(text, str) or not text.strip() or stored is None:
                        continue
                    sim = cosine(stored, embedder.embed(c["embed_model"], c["document_prefix"] + text))
                    entry["sampled"] += 1
                    if sim is not None and sim > self.min_cosine:
                        entry["coherent"] += 1
                if entry["sampled"]:
                    entry["ratio"] = entry["coherent"] / entry["sampled"]
            except Exception as e:  # a check failure is reported, never fatal
                entry["error"] = f"{type(e).__name__}: {e}"[:300]
                errors.append(col)
            cols[col] = entry
            coherent += entry["coherent"]
            sampled += entry["sampled"]
        ratio = coherent / sampled if sampled else None
        healthy = not errors and ratio is not None and ratio >= c["coherence_threshold"]
        return {"healthy": healthy, "ratio": ratio, "sampled": sampled, "coherent": coherent,
                "threshold": c["coherence_threshold"], "checked_at": time.time(), "collections": cols}

    def run_once(self) -> None:
        for name, c in self.corpus.corpora.items():
            self.status[name] = self.check(c)

    def warning(self, name: str) -> str | None:
        st = self.status.get(name)
        if st is None or st["healthy"]:
            return None
        ratio = "unknown" if st["ratio"] is None else f"{st['ratio']:.2f}"
        return (f"corpus {name} failed its coherence check (coherent fraction {ratio}, threshold "
                f"{st['threshold']:.2f}): some stored vectors may come from a different embedding model, "
                "so results can be incomplete or off-topic")

    def loop(self, stop: threading.Event, interval: int) -> None:
        while not stop.is_set():
            self.run_once()
            if interval <= 0 or stop.wait(interval):
                return


def metrics_text(corpus: "Corpus") -> str:
    def lbl(**kw: str) -> str:
        return "{" + ",".join(f'{k}="{v}"' for k, v in kw.items()) + "}"
    lines = [
        "# HELP skcorpus_coherence_ratio Fraction of sampled points whose stored vector matches a fresh embedding.",
        "# TYPE skcorpus_coherence_ratio gauge",
    ]
    rest = ["# HELP skcorpus_coherence_healthy 1 when the corpus is at or above its coherence threshold.",
            "# TYPE skcorpus_coherence_healthy gauge",
            "# HELP skcorpus_coherence_sampled Points sampled in the last check.",
            "# TYPE skcorpus_coherence_sampled gauge",
            "# HELP skcorpus_coherence_last_check_timestamp_seconds When the last check finished.",
            "# TYPE skcorpus_coherence_last_check_timestamp_seconds gauge"]
    for name in corpus.corpora:
        st = corpus.coherence.status.get(name)
        if st is None:
            continue
        for col, e in st["collections"].items():
            if e["ratio"] is not None:
                lines.append(f"skcorpus_coherence_ratio{lbl(corpus=name, collection=col)} {e['ratio']:.6f}")
        if st["ratio"] is not None:
            lines.append(f"skcorpus_coherence_ratio{lbl(corpus=name, collection='')} {st['ratio']:.6f}")
        rest.append(f"skcorpus_coherence_healthy{lbl(corpus=name)} {1 if st['healthy'] else 0}")
        rest.append(f"skcorpus_coherence_sampled{lbl(corpus=name)} {st['sampled']}")
        rest.append(f"skcorpus_coherence_last_check_timestamp_seconds{lbl(corpus=name)} {st['checked_at']:.0f}")
    return "\n".join(lines + rest) + "\n"


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


class ScopedSearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=2000, description="What to look for, in natural language")
    k: int = Field(default=8, ge=1, description=f"Number of chunks to return (at most {MAX_K})")


class ScopedGetIn(BaseModel):
    collection: str = Field(description="The collection of a search hit")
    file_path: str = Field(min_length=1, max_length=1024)
    chunk_index: int = Field(ge=0)
    window: int = Field(default=1, ge=0, description=f"Neighbouring chunks on each side (at most {MAX_WINDOW})")


class ScopedEntityIn(BaseModel):
    name: str = Field(min_length=1, max_length=300, description="Entity name, case-insensitive exact match")


class ScopedNeighborsIn(ScopedEntityIn):
    depth: int = Field(default=1, ge=1, le=2, description="1 or 2 hops")


class _Named(BaseModel):
    corpus: str = Field(description="Corpus name from corpus_list")


class SearchIn(_Named, ScopedSearchIn):
    pass


class GetIn(_Named, ScopedGetIn):
    pass


class EntityIn(_Named, ScopedEntityIn):
    pass


class NeighborsIn(_Named, ScopedNeighborsIn):
    pass


class Corpus:
    def __init__(self, corpora: dict[str, dict], qdrant: QdrantReader | None, falkor: FalkorReader | None,
                 embedder: Embedder | None, embedder_for: Any = None):
        """``embedder`` serves corpora without their own ``embed_url``;
        ``embedder_for(url)`` returns the Embedder for a corpus that has one."""
        self.corpora, self.qdrant, self.falkor, self.embedder = corpora, qdrant, falkor, embedder
        self.embedder_for = embedder_for
        self.coherence = Coherence(self)

    def embedder_of(self, c: dict) -> Embedder:
        e = self.embedder_for(c["embed_url"]) if c["embed_url"] and self.embedder_for else (
            None if c["embed_url"] else self.embedder)
        if e is None:
            raise HTTPException(503, "embeddings are not configured for this corpus")
        return e

    def _warn(self, name: str, hits: list[dict]) -> list[dict]:
        w = self.coherence.warning(name)
        if w:
            for h in hits:
                h["warning"] = w
        return hits

    def get(self, name: str) -> dict:
        c = self.corpora.get(name)
        if c is None:
            raise HTTPException(404, f"unknown corpus {name!r}; call corpus_list")
        return c

    def describe(self, c: dict) -> dict:
        st = self.coherence.status.get(c["name"])
        return {"name": c["name"], "title": c["title"], "description": c["description"],
                "collections": c["collections"], "has_graph": bool(c["graph"]),
                "coherence": None if st is None else {"healthy": st["healthy"], "ratio": st["ratio"]},
                "warning": self.coherence.warning(c["name"])}

    def list(self) -> list[dict]:
        return [self.describe(c) for c in self.corpora.values()]

    def search(self, name: str, req: ScopedSearchIn) -> list[dict]:
        c = self.get(name)
        if self.qdrant is None:
            raise HTTPException(503, "vector search is not configured")
        embedder = self.embedder_of(c)
        k = min(req.k, MAX_K)
        vector = embedder.embed(c["embed_model"], c["query_prefix"] + req.query)
        per = [(col, self.qdrant.search(col, vector, k, c["vector_name"])) for col in c["collections"]]
        return self._warn(name, merge_hits(per, k))

    def chunks(self, name: str, req: ScopedGetIn) -> list[dict]:
        c = self.get(name)
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
        return self._warn(name, sorted(hits, key=lambda h: (h["chunk_index"] is None, h["chunk_index"] or 0)))

    def _graph(self, corpus: dict) -> str:
        if not corpus["graph"]:
            raise HTTPException(404, f"corpus {corpus['name']} has no graph")
        if self.falkor is None:
            raise HTTPException(503, "graph search is not configured")
        return corpus["graph"]

    def entity(self, name: str, req: ScopedEntityIn) -> list[dict]:
        c = self.get(name)
        g = self._graph(c)
        q = ENTITY_QUERY.format(prop=c["name_property"])
        return self.falkor.ro_query(g, q, {"name": req.name, "limit": MAX_GRAPH_ROWS})[:MAX_GRAPH_ROWS]

    def neighbors(self, name: str, req: ScopedNeighborsIn) -> list[dict]:
        c = self.get(name)
        g = self._graph(c)
        q = (NEIGHBORS_1 if req.depth == 1 else NEIGHBORS_2).format(prop=c["name_property"])
        return self.falkor.ro_query(g, q, {"name": req.name, "limit": MAX_GRAPH_ROWS})[:MAX_GRAPH_ROWS]


DESCRIPTION = (
    "Read-only search over configured knowledge corpora. Layered retrieval: use graph_entity and "
    "graph_neighbors for who or what connects to what, corpus_search for substance, corpus_get "
    "for the surrounding text of a hit. Always cite title, file_path and chunk_index. A result with a "
    "warning comes from a corpus that failed its coherence check: say so when you use it."
)


def _scoped_app(corpus: Corpus, c: dict, auth: Any) -> FastAPI:
    """One corpus, its own OpenAPI spec, operations without a corpus argument."""
    name = c["name"]
    deps = [Depends(auth)]
    sub = FastAPI(title=f"skcorpus {name}", version="1.1.0",
                  description=f"{DESCRIPTION} This server searches the corpus {c['title']} ({name}) only.")

    @sub.get("/corpora", operation_id="corpus_list", dependencies=deps, summary="This corpus and its health")
    def corpus_list() -> list[dict]:
        return [corpus.describe(c)]

    @sub.post("/search", operation_id="corpus_search", dependencies=deps,
              summary="Semantic search; returns chunks with title, file_path, chunk_index, score")
    def corpus_search(req: ScopedSearchIn) -> list[dict]:
        return corpus.search(name, req)

    @sub.post("/chunks", operation_id="corpus_get", dependencies=deps,
              summary="Neighbouring chunks of one search hit (same file, chunk_index +/- window)")
    def corpus_get(req: ScopedGetIn) -> list[dict]:
        return corpus.chunks(name, req)

    if c["graph"]:
        @sub.post("/graph/entity", operation_id="graph_entity", dependencies=deps,
                  summary="Look up an entity by name in the corpus graph")
        def graph_entity(req: ScopedEntityIn) -> list[dict]:
            return corpus.entity(name, req)

        @sub.post("/graph/neighbors", operation_id="graph_neighbors", dependencies=deps,
                  summary="Entities connected to a named entity (depth 1 or 2)")
        def graph_neighbors(req: ScopedNeighborsIn) -> list[dict]:
            return corpus.neighbors(name, req)

    return sub


def create_app(corpus: Corpus | None = None, token: str | None = None, global_routes: bool = True,
               coherence_interval: int | None = None) -> FastAPI:
    """``coherence_interval``: None = no background check (tests drive
    ``corpus.coherence.run_once()``), 0 = once at startup, N = startup and
    every N seconds. The env factory ``app()`` passes COHERENCE_INTERVAL."""
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
        e_key = os.environ.get("EMBED_API_KEY", "")
        f_host = os.environ.get("FALKORDB_HOST", "")
        embedders: dict[str, Embedder] = {}

        def embedder_for(url: str) -> Embedder:
            if url not in embedders:
                embedders[url] = Embedder(url, e_key)
            return embedders[url]

        corpus = Corpus(
            corpora,
            QdrantReader(q_url, os.environ.get("QDRANT_API_KEY", "")) if q_url else None,
            FalkorReader(RespConnection(f_host, int(os.environ.get("FALKORDB_PORT", "6379")),
                                        os.environ.get("FALKORDB_USERNAME", ""),
                                        os.environ.get("FALKORDB_PASSWORD", ""))) if f_host else None,
            embedder_for(e_url) if e_url else None,
            embedder_for,
        )

    def auth(request: Request) -> None:
        got = request.headers.get("authorization", "")
        if not hmac.compare_digest(got.encode(), f"Bearer {token}".encode()):
            raise HTTPException(401, "missing or wrong bearer token", headers={"WWW-Authenticate": "Bearer"})

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        stop = threading.Event()
        if coherence_interval is not None and corpus.corpora:
            threading.Thread(target=corpus.coherence.loop, args=(stop, coherence_interval),
                             name="coherence", daemon=True).start()
        yield
        stop.set()

    app = FastAPI(title="skcorpus", version="1.1.0", description=DESCRIPTION, lifespan=lifespan,
                  openapi_url="/openapi.json" if global_routes else None, docs_url=None, redoc_url=None)

    @app.get("/health", include_in_schema=False)
    def health() -> dict:
        """Always HTTP 200 (the container is alive); ``status`` is degraded
        while any corpus is below its coherence threshold or a check failed."""
        st = corpus.coherence.status
        degraded = any(not v["healthy"] for v in st.values())
        return {"status": "degraded" if degraded else "ok", "service": "skcorpus", "corpora": len(corpus.corpora),
                "coherence": {n: {k: v[k] for k in ("healthy", "ratio", "sampled", "threshold", "checked_at")}
                              | {"errors": [c for c, e in v["collections"].items() if "error" in e]}
                              for n, v in st.items()}}

    @app.get("/metrics", include_in_schema=False, response_class=PlainTextResponse)
    def metrics() -> str:
        return metrics_text(corpus)

    if global_routes:
        @app.get("/corpora", operation_id="corpus_list", dependencies=[Depends(auth)],
                 summary="List the corpora that can be searched")
        def corpus_list() -> list[dict]:
            return corpus.list()

        @app.post("/search", operation_id="corpus_search", dependencies=[Depends(auth)],
                  summary="Semantic search in a corpus; returns chunks with title, file_path, chunk_index, score")
        def corpus_search(req: SearchIn) -> list[dict]:
            return corpus.search(req.corpus, req)

        @app.post("/chunks", operation_id="corpus_get", dependencies=[Depends(auth)],
                  summary="Neighbouring chunks of one search hit (same file, chunk_index +/- window)")
        def corpus_get(req: GetIn) -> list[dict]:
            return corpus.chunks(req.corpus, req)

        @app.post("/graph/entity", operation_id="graph_entity", dependencies=[Depends(auth)],
                  summary="Look up an entity by name in the corpus graph")
        def graph_entity(req: EntityIn) -> list[dict]:
            return corpus.entity(req.corpus, req)

        @app.post("/graph/neighbors", operation_id="graph_neighbors", dependencies=[Depends(auth)],
                  summary="Entities connected to a named entity (depth 1 or 2)")
        def graph_neighbors(req: NeighborsIn) -> list[dict]:
            return corpus.neighbors(req.corpus, req)

    for c in corpus.corpora.values():
        app.mount(f"/c/{c['name']}", _scoped_app(corpus, c, auth))

    return app


def app() -> FastAPI:  # uvicorn --factory skcorpus:app
    return create_app(global_routes=os.environ.get("GLOBAL_ROUTES", "false").lower() == "true",
                      coherence_interval=COHERENCE_INTERVAL)
