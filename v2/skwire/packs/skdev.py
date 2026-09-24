"""
skdev — the AI dev toolchain: Claude Code / Codex / OpenCode wired to your model
endpoints (OpenRouter / OpenAI / local Ollama). An agent that "needs a model
endpoint + key" is just another node; the key is a minted secret, the endpoint is
wired config. Use it to contribute back or fork your own.
"""
from __future__ import annotations

from skwire import Pack
from skwire.inject import EnvFileInjector


def pack() -> Pack:
    return Pack(
        name="skdev",
        nodes=[
            {"name": "ollama", "provides": {"url": "http://ollama:11434", "api_kind": "ollama"}},
            {"name": "openrouter", "provides": {"url": "https://openrouter.ai/api/v1", "api_kind": "openai"},
             "secrets": [{"key": "openrouter_api_key", "rotation_days": 90}]},
            {"name": "opencode", "needs": [{"service": "openrouter", "secret": "openrouter_api_key"}]},
            {"name": "codex", "needs": [{"service": "openrouter", "secret": "openrouter_api_key"}]},
            {"name": "claude-code", "needs": [{"service": "openrouter", "secret": "openrouter_api_key"}]},
            {"name": "hermes", "needs": [{"service": "ollama", "secret": "ollama_token"},
                                          {"service": "openrouter", "secret": "openrouter_api_key"}]},
        ],
        injectors={"agent-env": EnvFileInjector("~/.config/skdev")},
        questions=["Default model provider — OpenRouter, OpenAI, or local Ollama?",
                   "Which agents — Claude Code, Codex, OpenCode (any combo)?"],
    )
