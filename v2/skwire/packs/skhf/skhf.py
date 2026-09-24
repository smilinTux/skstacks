"""
skhf — Hugging Face download assistant. Chat your way to a resumable 1TB download
on the right disk: skwire asks repo / model-or-dataset / which big disk / token,
then runs the bundled `skhf` helper which sets HF_HOME correctly (so the Xet chunk
cache doesn't fill ~/), pre-flights size vs free space, and retries with Xet
chunk-resume. Fixes the wrong-dir / no-resume / FS-full-at-900GB trifecta.
"""
from __future__ import annotations

from skwire import Pack, Option


def pack() -> Pack:
    return Pack(
        name="skhf",
        nodes=[{"name": "hf-download", "provides": {"url": "local", "api_kind": "hf"}}],
        # The conversation auto-generates from these option specs:
        options=[
            Option("repo", flag="<repo_id>", required=True,
                   prompt="Which Hugging Face repo? (or describe it — I'll `skhf search` live for it)"),
            Option("type", flag="--type", type="choice", choices=("model", "dataset"), default="model"),
            Option("dest", flag="--dest", type="path", required=True,
                   prompt="Which big disk should it download to? (never ~/ or /)"),
            Option("preset", flag="--safetensors/--gguf", type="choice",
                   choices=("everything", "safetensors", "gguf"), default="everything",
                   prompt="Whole repo, just safetensors, or just GGUF (for Ollama/llama.cpp)?"),
            Option("token", flag="--token", type="str", required=False,
                   prompt="Is it gated/private? (paste an HF token, or skip)"),
        ],
        post_install="skhf is ready 🎉  Download anything with:\n"
                     "  skhf <repo_id> --gguf /path/to/big-disk\n"
                     "It resumes, won't fill ~/, and `skhf search <query>` finds repos live.",
    )
