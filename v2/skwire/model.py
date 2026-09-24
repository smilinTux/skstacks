"""
skwire model — provision the tiny conversational model on a fresh machine.

The whole point: a brand-new box has NO LLM. `ensure_model()` makes one appear,
preferring the least-effort path:

    1. SKWIRE_LLM_URL set        → already have an endpoint, nothing to do
    2. Ollama present            → `ollama pull <tiny gemma>` (a few hundred MB)
    3. neither                   → download a tiny instruct GGUF via skhf (our own
                                   downloader) into the cache; run it with llama.cpp

The default is a *small* Gemma (≈270M, CPU-friendly) so it works on any laptop.
All side-effecting deps (subprocess, which, network probe) are injectable → unit-tested.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

from .llm import SMALL_MODEL, _reachable

# Tiny instruct GGUF for the embedded (no-Ollama) path. Override w/ SKWIRE_MODEL_GGUF.
GGUF_REPO = os.environ.get("SKWIRE_MODEL_GGUF", "ggml-org/gemma-3-270m-it-GGUF")
MODELS_DIR = os.path.expanduser(os.environ.get("SKWIRE_MODELS_DIR", "~/.cache/skwire/models"))


@dataclass
class ModelStatus:
    provider: str            # "configured" | "ollama" | "llama.cpp" | "none"
    model: str
    detail: str = ""
    ready: bool = False      # True = a client can talk to it right now


def ensure_model(model: str = SMALL_MODEL, *, ollama_host: str = "http://127.0.0.1:11434",
                 probe=_reachable, run=subprocess.run, have=shutil.which) -> ModelStatus:
    """Make a conversational model available. Returns what was provisioned."""
    if os.environ.get("SKWIRE_LLM_URL"):
        return ModelStatus("configured", os.environ.get("SKWIRE_MODEL", model),
                           "using your configured SKWIRE_LLM_URL", ready=True)

    # Ollama is the easy button — pull the tiny model and we're done.
    if have("ollama") or probe(ollama_host, "/api/tags"):
        run(["ollama", "pull", model], check=False)
        return ModelStatus("ollama", model, f"pulled {model} via Ollama", ready=True)

    # No Ollama: fetch a tiny GGUF with our own skhf downloader, run via llama.cpp.
    skhf = _skhf_path()
    if skhf and (have("llama-server") or have("llama-cli")):
        run([skhf, GGUF_REPO, "--gguf", MODELS_DIR], check=False)
        return ModelStatus("llama.cpp", model,
                           f"downloaded {GGUF_REPO} → {MODELS_DIR}; start it with `skwire model serve`",
                           ready=False)
    if skhf:
        run([skhf, GGUF_REPO, "--gguf", MODELS_DIR], check=False)
        return ModelStatus("llama.cpp", model,
                           f"downloaded {GGUF_REPO} → {MODELS_DIR}. Install llama.cpp "
                           "(brew/apt install llama.cpp) then `skwire model serve`.", ready=False)
    return ModelStatus("none", model,
                       "No Ollama and skhf helper not found — install Ollama (ollama.com) "
                       "for the simplest path, then re-run `skwire model setup`.", ready=False)


def _skhf_path():
    """Locate the bundled skhf downloader. On a normal install it's a real file;
    inside a zipapp it lives in the zip, so extract it to a cache dir + chmod +x."""
    here = os.path.dirname(__file__)
    cand = os.path.join(here, "packs", "skhf", "skhf")
    if os.path.exists(cand):
        return cand
    try:                                   # zipapp / wheel: materialise via resources
        from importlib import resources
        data = (resources.files("skwire.packs.skhf") / "skhf").read_bytes()
        out = os.path.join(MODELS_DIR, "..", "skhf")
        out = os.path.abspath(out)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "wb") as f:
            f.write(data)
        os.chmod(out, 0o755)
        return out
    except Exception:
        return shutil.which("skhf")
