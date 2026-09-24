"""
skwire LLM — the conversation brain.

A bootstrapper can't assume the user has *any* model installed, so skwire ships a
tiny embedded default (small Gemma) that auto-provisions on first run, behind a
fallback ladder:

    1. SKWIRE_LLM_URL          — a configured OpenAI-compatible endpoint (any model)
    2. local Ollama            — http://127.0.0.1:11434 (pull the tiny model)
    3. embedded tiny model     — bundled/auto-downloaded GGUF run via llama.cpp
    4. keyword router          — chat.interpret(), always-works floor (no model)

The conversation engine (`converse`) takes an INJECTED client, so a real multi-turn
back-and-forth is unit-testable offline with a scripted fake (see tests/test_converse.py).
The client is anything with `.chat(messages) -> str`; nothing here needs network at import.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable

# The tiny default model. Small enough to run on a CPU-only laptop; auto-pulled.
# Override with SKWIRE_MODEL (e.g. a bigger local model if you have the VRAM).
SMALL_MODEL = os.environ.get("SKWIRE_MODEL", "gemma3:270m")


@runtime_checkable
class LLMClient(Protocol):
    name: str
    def chat(self, messages: list) -> str: ...


@dataclass
class ConverseResult:
    reply: str
    action: dict                       # {"kind": "...", "target": "..."}
    history: list = field(default_factory=list)   # [{role, content}, ...]
    raw: str = ""
    ok: bool = True                    # False = degenerate output; caller should fall back


# ── prompt + parsing ───────────────────────────────────────────────────────────
VALID_KINDS = ("add_one", "add_all", "search", "plan", "approve", "none")


def build_system_prompt(offerings) -> str:
    cat = "; ".join(f"{o.name}={o.title}" for o in offerings) or "(catalog loading)"
    # Few-shot, not a schema: small models mimic concrete examples far better than they
    # follow abstract format descriptions (a 270M model will otherwise echo the placeholders).
    return (
        "You are skwire — a warm, concise setup assistant that wires up self-hosted software. "
        "Be friendly and brief.\n"
        f"Apps you can install: {cat}.\n"
        "Reply to the user with ONLY one JSON object, exactly like these examples:\n\n"
        'User: i want to download models from huggingface\n'
        'You: {"reply":"I can set up the HF downloader for you — want me to add it?","action":{"kind":"add_one","target":"skhf"}}\n\n'
        'User: set me up to watch movies\n'
        'You: {"reply":"Sweet, the media server is perfect for that. Add it?","action":{"kind":"add_one","target":"skmedia"}}\n\n'
        'User: just install everything\n'
        'You: {"reply":"You got it — setting up the whole stack.","action":{"kind":"add_all","target":""}}\n\n'
        'User: search for llama 3 8b\n'
        'You: {"reply":"Let me look that up on Hugging Face.","action":{"kind":"search","target":"llama 3 8b"}}\n\n'
        'User: ok go ahead and do it\n'
        'You: {"reply":"On it — here is the plan.","action":{"kind":"plan","target":""}}\n\n'
        'User: hey there\n'
        'You: {"reply":"Hi! What would you like to set up today?","action":{"kind":"none","target":""}}\n\n'
        "Now reply to the next user message the same way — real values, never the words in brackets. "
        f"target must be one of: {', '.join(o.name for o in offerings)} (or a search query). "
        "If unsure, ask a question with kind none."
    )


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_action(text: str):
    """Pull (reply, action, ok) out of model output, tolerating fences/extra prose.
    ok=False means the output was degenerate (no JSON, or schema placeholders echoed)
    and the caller should fall back to the keyword router."""
    raw = (text or "").strip()
    blob = raw
    if "```" in blob:                                  # strip ```json fences
        parts = re.findall(r"```(?:json)?\s*(.*?)```", blob, re.DOTALL)
        if parts:
            blob = parts[0].strip()
    m = _JSON_RE.search(blob)
    if m:
        try:
            obj = json.loads(m.group(0))
            reply = str(obj.get("reply") or "").strip()
            action = obj.get("action") or {}
            kind = str(action.get("kind") or "none")
            target = str(action.get("target") or "")
            # reject template echo from weak models: "<...>" placeholders or a piped kind
            placeholder = ("<" in reply or "|" in kind or "<" in kind or kind not in VALID_KINDS)
            if reply and not placeholder:
                act = {"kind": kind, "target": target} if target else {"kind": kind}
                return reply, act, True
        except (ValueError, AttributeError):
            pass
    # degenerate: clean fallback message, signal not-ok so the caller can use the router
    return ("Let me help with that — what would you like to set up?"), {"kind": "none"}, False


def converse(history: list, user_msg: str, client: LLMClient, *, offerings) -> ConverseResult:
    """One conversational turn. Threads `history` so the model has real memory."""
    messages = [{"role": "system", "content": build_system_prompt(offerings)}]
    messages += [{"role": h["role"], "content": h["content"]} for h in (history or [])]
    messages.append({"role": "user", "content": user_msg})
    raw = client.chat(messages)
    reply, action, ok = parse_action(raw)
    new_history = list(history or []) + [
        {"role": "user", "content": user_msg},
        {"role": "assistant", "content": reply},
    ]
    return ConverseResult(reply=reply, action=action, history=new_history, raw=raw, ok=ok)


# ── clients (zero-dep, urllib) ───────────────────────────────────────────────────
class OpenAIClient:
    """Talks to any OpenAI-compatible /v1/chat/completions endpoint (incl. llama.cpp
    server, vLLM, Ollama's /v1, OpenRouter). Pure stdlib."""
    def __init__(self, base_url: str, model: str, api_key: str = "", timeout: int = 60):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.name = f"openai:{model}@{self.base_url}"

    def chat(self, messages: list) -> str:
        import urllib.request
        body = json.dumps({"model": self.model, "messages": messages,
                           "temperature": 0.3, "stream": False}).encode()
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(self.base_url + "/v1/chat/completions", body, headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as r:   # nosec
            data = json.loads(r.read().decode())
        return data["choices"][0]["message"]["content"]


class OllamaClient:
    """Talks to a local Ollama via its native /api/chat with JSON-mode for reliable
    structured output. Pure stdlib."""
    def __init__(self, base_url: str = "http://127.0.0.1:11434", model: str = SMALL_MODEL, timeout: int = 60):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.name = f"ollama:{model}"

    def chat(self, messages: list) -> str:
        import urllib.request
        # cap generation (replies are short JSON) + keep the model warm in RAM
        body = json.dumps({"model": self.model, "messages": messages, "stream": False,
                           "format": "json", "keep_alive": "30m",
                           "options": {"temperature": 0.2, "num_predict": 220}}).encode()
        req = urllib.request.Request(self.base_url + "/api/chat", body,
                                     {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:   # nosec
            data = json.loads(r.read().decode())
        return data["message"]["content"]


def _reachable(url: str, path: str = "/", timeout: int = 2) -> bool:
    import urllib.request
    try:
        urllib.request.urlopen(url.rstrip("/") + path, timeout=timeout).read(1)   # nosec
        return True
    except Exception:
        return False


def resolve_client(*, probe=_reachable) -> Optional[LLMClient]:
    """Walk the fallback ladder and return the best available client, or None
    (→ caller uses the keyword router). `probe` is injectable for tests."""
    if os.environ.get("SKWIRE_LLM_DISABLE") == "1":
        return None                                    # escape hatch / deterministic tests
    url = os.environ.get("SKWIRE_LLM_URL")
    if url:
        model = os.environ.get("SKWIRE_MODEL", SMALL_MODEL)
        return OpenAIClient(url, model, os.environ.get("SKWIRE_LLM_KEY", ""))
    ollama = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
    if probe(ollama, "/api/tags"):
        return OllamaClient(ollama, os.environ.get("SKWIRE_MODEL", SMALL_MODEL))
    # embedded llama.cpp rung is provisioned by model.ensure_model(); if it left a
    # running server, SKWIRE_LLM_URL would be set. Nothing reachable → keyword floor.
    return None
