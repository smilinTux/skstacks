# skhf — foolproof Hugging Face downloads

For when downloading a 1TB dataset keeps **filling your home dir**, **won't resume**,
and **blows the filesystem at 900GB**. skhf fixes all three (verified mid-2026).

## Just run it
```bash
skhf HuggingFaceFW/fineweb --type dataset --dest /mnt/bigdisk
skhf meta-llama/Llama-3.2-1B-Instruct --dest /mnt/bigdisk           # a model
skhf org/gated-model --dest /mnt/bigdisk --token hf_xxx             # gated

# just the safetensors (+ config/tokenizer) — skip duplicate .bin/.gguf/fp16, saves TBs
skhf meta-llama/Llama-3.3-70B-Instruct --dest /mnt/bigdisk --safetensors

# fine-grained: only/skip patterns (repeatable)
skhf org/model --dest /mnt/bigdisk --include "*.safetensors" --include "config.json"
skhf org/model --dest /mnt/bigdisk --exclude "*.gguf" --exclude "*.bin"
```

**Presets / shortcuts**
| Flag | Effect |
|---|---|
| `--safetensors` | `*.safetensors` + config/tokenizer only — skips duplicate `.bin/.pth/.h5/.gguf/fp16` weights |
| `--gguf` | `*.gguf` only (Ollama / llama.cpp) — narrow a quant with `--include "*Q4_K_M*"` |
| `--include P` | only files matching glob P (repeatable) |
| `--exclude P` | skip files matching glob P (repeatable) |

## Find what to download (live)
```bash
skhf search "enron email" --type dataset   # real HF results, ranked by downloads
skhf search "llama 70b gguf" --type model
skhf popular                               # the big training datasets everyone pulls
skhf popular --type model
```
`search` hits the public HF Hub API, so it shows **real, current** datasets/models —
the LLM in the chat window uses this to answer "what training datasets are there for X?"

## …or chat it (via skwire)
> "I need to grab the fineweb dataset onto /mnt/bigdisk, resumable"

skwire asks repo / model-or-dataset / which disk / token and runs skhf for you.

## What it fixes (the gotchas)
| Pain | Cause | skhf's fix |
|---|---|---|
| Fills `~/` or `/` | You set `HF_HUB_CACHE` (or nothing), so the **Xet chunk cache stayed on `~/`** | Sets **`HF_HOME`** → cache **+ Xet + token** all land on `--dest` |
| "Resume won't work" | `hf_transfer` (now **dead/ignored**) force-re-downloaded; `--local-dir` resume is buggy | Downloads into the **cache** (Xet **chunk-resume**) + an `until` **retry loop** |
| FS full at 900GB | No pre-flight | `--dry-run` size **vs** `df` free space before starting |
| `huggingface-cli not found` | Renamed; removed in v1.0 | Uses the new **`hf`** CLI; installs `huggingface_hub[hf_xet]` if missing |

Notes: env vars are read **at import**, so skhf exports them in the shell *before*
calling `hf`. `HF_XET_HIGH_PERFORMANCE=1` replaces the old `HF_HUB_ENABLE_HF_TRANSFER`.
Want plain files in one folder afterward? add `--local-dir /your/dir` (accepts the
local-dir resume caveat — the retry loop mitigates it).
