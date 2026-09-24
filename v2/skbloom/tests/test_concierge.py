"""The concierge turns intent into a VALIDATED profile. The model only ever proposes
from the real catalog (it can't invent a service); the deterministic matcher is the
floor, and a profile is only ever made of services that actually exist."""
from __future__ import annotations

import pytest

import os

from skbloom.concierge import Service, match, propose_profile, concierge_reply, load_services

V2 = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


CATALOG = [
    Service("cloud/skfence", "skfence", "Edge / ingress — TLS, rate limiting", "Traefik v3"),
    Service("core/sksso", "sksso", "Single sign-on — OIDC/SAML/LDAP", "Authentik"),
    Service("core/sksec", "sksec", "Intrusion detection / IPS", "CrowdSec"),
    Service("core/skca", "skca", "Internal certificate authority", "step-ca"),
    Service("comms/skchat", "skchat", "Human/agent chat + federation — Matrix homeserver", "Tuwunel"),
]


def test_match_ranks_by_relevance():
    hits = match("i need single sign-on for my apps", CATALOG)
    assert hits and hits[0].name == "sksso"


def test_match_finds_by_provider_or_capability_words():
    assert match("set up traefik ingress", CATALOG)[0].name == "skfence"
    assert match("intrusion detection", CATALOG)[0].name == "sksec"


def test_propose_profile_only_contains_real_services():
    prof = propose_profile("i want sso and an ingress", CATALOG, cluster="c1")
    names = {p.split("/")[-1] for p in prof.services}
    assert {"sksso", "skfence"} <= names
    # never proposes something not in the catalog
    assert all(any(s.path == p for s in CATALOG) for p in prof.services)


def test_everything_proposes_the_whole_catalog():
    prof = propose_profile("just install everything", CATALOG, cluster="c1")
    assert len(prof.services) == len(CATALOG)


def test_unmatched_intent_yields_empty_not_garbage():
    prof = propose_profile("xyzzy nonsense", CATALOG, cluster="c1")
    assert prof.services == []                       # nothing invented


def test_vanity_name_from_natural_language():
    # "call it / name it / called X" → the service deploys under that vanity name
    for phrase in ("set up sso and call it login", "i want sso named login",
                   "give me single sign-on called login"):
        prof = propose_profile(phrase, CATALOG, cluster="c1")
        assert prof.services == ["core/sksso=login"], phrase


def test_vanity_only_applies_when_one_service_matched():
    # ambiguous (two services) → don't guess which gets the name
    prof = propose_profile("sso and ingress called login", CATALOG, cluster="c1")
    assert all("=" not in s for s in prof.services)  # no vanity applied to a multi-match
    assert {s.split("/")[-1] for s in prof.services} == {"sksso", "skfence"}


def test_reply_uses_the_model_when_present_else_deterministic():
    prof = propose_profile("sso please", CATALOG, cluster="c1")

    class FakeLLM:
        name = "fake"
        def chat(self, messages):
            return "Great — I'll set up Authentik SSO for you. Ready when you are!"
    r = concierge_reply("sso please", prof, CATALOG, client=FakeLLM())
    assert "SSO" in r or "Authentik" in r

    # no model → still a sensible, profile-accurate reply (names the chosen services)
    r2 = concierge_reply("sso please", prof, CATALOG, client=None)
    assert "sksso" in r2


def test_chat_intent_resolves_to_skchat():
    # natural "chat server" intent → skchat (Matrix homeserver)
    assert match("set up a chat server", CATALOG)[0].name == "skchat"
    assert match("i want a matrix homeserver", CATALOG)[0].name == "skchat"
    prof = propose_profile("set up a chat server", CATALOG, cluster="c1")
    assert "skchat" in {p.split("/")[-1] for p in prof.services}


def test_skchat_is_a_deployable_in_the_real_catalog():
    # the descriptor on disk declares a deploy: block, so it loads as a real Service,
    # and the chat intent resolves to it against the live catalog.
    services = load_services(V2)
    assert any(s.name == "skchat" for s in services), "skchat must be a deployable"
    prof = propose_profile("set up a chat server", services, cluster="c1")
    assert "skchat" in {p.split("/")[-1] for p in prof.services}


def test_reply_never_claims_services_not_in_the_profile():
    prof = propose_profile("sso please", CATALOG, cluster="c1")  # only sksso
    r = concierge_reply("sso please", prof, CATALOG, client=None)
    assert "sksec" not in r and "skca" not in r
