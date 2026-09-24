"""
skbloom concierge — intent → a VALIDATED profile.

The safety property from the design: the LLM only ever produces the *what* (which
services), and only ever from the real catalog. The deterministic matcher is the floor
(it can't invent a service); the model just makes the conversation natural. A Profile is
only ever built from services that actually exist on disk.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from .flow import Profile

_STOP = {"i", "a", "an", "the", "to", "my", "me", "want", "need", "set", "up", "please",
         "for", "and", "with", "some", "just", "install", "get", "of", "on", "can", "you",
         "call", "called", "name", "named", "it", "give"}
_ALL = ("everything", "whole stack", "all of it", "the works", "full stack", "all the")

# Curated intent→service aliases — robust matching regardless of descriptor wording
# (the user says "single sign-on" / "login", the descriptor says "SSO").
ALIASES = {
    "sksso":    {"sso", "single", "signon", "sign", "login", "auth", "authentication",
                 "identity", "oidc", "saml", "ldap", "authentik"},
    "skfence":  {"ingress", "gateway", "proxy", "edge", "tls", "https", "traefik",
                 "reverse", "loadbalancer", "lb"},
    "skca":     {"ca", "cert", "certs", "certificate", "certificates", "pki", "mtls", "acme"},
    "sksec":    {"ids", "ips", "intrusion", "crowdsec", "threat", "detection"},
    "capauth":  {"pgp", "capability", "sovereign"},
    "skvault":  {"vault", "secret", "secrets", "openbao", "kms"},
    "skcache":  {"cache", "redis", "valkey", "keyvalue", "kv"},
    "skobject": {"s3", "object", "storage", "blob", "bucket", "minio", "garage"},
    "skmodel":  {"model", "models", "llm", "ollama", "inference", "embeddings"},
    "skbus":    {"bus", "queue", "messaging", "events", "nats", "pubsub", "stream", "jetstream"},
    "skchat":   {"chat", "messaging", "matrix", "im", "conversation", "conversations",
                 "rooms", "room", "homeserver", "federation", "tuwunel", "synapse"},
}


@dataclass(frozen=True)
class Service:
    path: str            # e.g. "core/sksso"
    name: str            # e.g. "sksso"
    capability: str = ""
    provider: str = ""
    description: str = ""
    config: dict = field(default_factory=dict)    # tunable knobs (key → default value)
    secrets: tuple = ()                            # secret key names (display only)
    ha: bool = False
    min_replicas: int = 1

    @property
    def _words(self) -> set:
        blob = f"{self.name} {self.capability} {self.provider} {self.description}".lower()
        return set(re.findall(r"[a-z0-9]+", blob))


def load_services(v2_root: str) -> list:
    """Every descriptor that declares a deploy: block, as a catalog of Services."""
    import glob, yaml
    out = []
    for f in sorted(glob.glob(os.path.join(v2_root, "*/*/app.yaml"))):
        try:
            d = yaml.safe_load(open(f)) or {}
        except Exception:
            continue
        if not d.get("deploy"):
            continue
        rel = os.path.relpath(os.path.dirname(f), v2_root)
        out.append(Service(
            path=rel, name=d.get("name", os.path.basename(rel)),
            capability=d.get("capability", ""), provider=d.get("provider", ""),
            description=d.get("description", ""),
            config=dict(d.get("config") or {}),
            secrets=tuple(s.get("key") for s in (d.get("secrets") or []) if s.get("key")),
            ha=bool(d.get("ha")), min_replicas=int(d.get("min_replicas", 1) or 1)))
    return out


def _query_words(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in _STOP and len(w) > 1}


def _score(qw: set, s: "Service") -> int:
    sc = len(qw & s._words)              # word overlap with capability/provider/desc
    if s.name in qw:
        sc += 3                          # named the service exactly
    sc += 3 * len(qw & ALIASES.get(s.name, set()))   # curated intent aliases (robust)
    # substring: "sso" → "sksso"
    sc += sum(1 for w in qw if len(w) >= 3 and (w in s.name or s.name.endswith(w)))
    return sc


def match(text: str, services: list) -> list:
    """Rank services by relevance to the intent (deterministic). Unmatched → [].
    Uses a relative floor so an incidental single-word overlap (e.g. Garage's "single
    binary" matching "single sign-on") is dropped next to a real alias hit."""
    qw = _query_words(text)
    scored = sorted(((_score(qw, s), s) for s in services), key=lambda t: t[0], reverse=True)
    scored = [(sc, s) for sc, s in scored if sc > 0]
    if not scored:
        return []
    floor = max(2, scored[0][0] * 0.4)               # within 40% of the best, min 2
    return [s for sc, s in scored if sc >= floor]


_VANITY_RE = re.compile(r"\b(?:call(?:ed)?\s+it|name[d]?\s+it|named|called)\s+([a-z][a-z0-9-]{1,30})", re.I)


def _vanity(text: str):
    m = _VANITY_RE.search(text or "")
    return m.group(1).lower() if m else None


def propose_profile(text: str, services: list, *, cluster: str = "skbloom") -> Profile:
    """Build a Profile from the intent — only ever real services. If the user named it
    ('call it login') and exactly one service matched, deploy under that vanity name."""
    m = (text or "").lower()
    chosen = list(services) if any(w in m for w in _ALL) else match(text, services)
    paths = [s.path for s in chosen]
    vanity = _vanity(text)
    if vanity and len(paths) == 1:                   # unambiguous → apply the vanity name
        paths = [f"{paths[0]}={vanity}"]
    return Profile(cluster=cluster, services=paths)


def concierge_reply(text: str, profile: Profile, services: list, *, client=None) -> str:
    """A friendly, profile-accurate reply. Uses the model when present (reusing skwire's
    ladder via the injected client), else a deterministic sentence. NEVER claims a service
    that isn't in the profile."""
    # each entry is "path" or "path=vanity" — resolve the descriptor + the deployed name
    def _epath(e): return e.split("=", 1)[0]
    def _ename(e): return e.split("=", 1)[1] if "=" in e else e.split("/")[-1]
    names = [_ename(e) for e in profile.services]
    if not names:
        return ("I didn't catch a service in that — I can set up things like an ingress, "
                "SSO, a CA, intrusion detection. What are you after?")
    paths = {_epath(e): _ename(e) for e in profile.services}
    chosen = [s for s in services if s.path in paths]
    parts = []
    for s in chosen:
        label = paths[s.path]
        cap = f" ({s.capability})" if s.capability else ""
        parts.append(f"{label}{cap}" + (f" [{s.name}]" if label != s.name else ""))
    listing = ", ".join(parts)
    deterministic = f"I'll set up: {listing}. Say 'go' and I'll skbloom up."
    if client is None:
        return deterministic + f"  [{' '.join(names)}]"
    try:
        sys_p = ("You are skbloom's setup concierge. The user's stack will be EXACTLY these "
                 f"services (do not mention others): {listing}. Reply in 1-2 friendly sentences "
                 "confirming what you'll set up and inviting them to proceed.")
        out = (client.chat([{"role": "system", "content": sys_p},
                            {"role": "user", "content": text}]) or "").strip()
        # guard: never let the model name a service outside the profile
        allowed = set(names) | {s.name for s in chosen}
        bad = [s.name for s in services if s.name not in allowed and s.name in out]
        if out and "<" not in out[:2] and not bad:
            return out
    except Exception:
        pass
    return deterministic
