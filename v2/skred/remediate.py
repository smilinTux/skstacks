"""
skred remediate — the closed loop.

Each finding already carries a built-in remediation hint. When a local LLM is
available (we reuse skwire's model ladder — abliterated qwen / tiny gemma / whatever
is wired), we ask it for a sharper, actionable fix. Fail safe: any model error leaves
the existing hint untouched. This is the "patch in real time" half of the loop.
"""
from __future__ import annotations


def _resolve_client(client):
    if client is not None:
        return client
    try:                                   # opportunistically reuse skwire's ladder
        from skwire import resolve_client
        return resolve_client()
    except Exception:
        return None


_SYS = ("You are a security remediation assistant. Given one finding, reply with a "
        "single concrete fix in 1-2 sentences — exact commands/config where possible. "
        "No preamble.")


def remediate(findings, *, client=None, max_items: int = 50) -> list:
    """Enrich findings' remediation using an LLM when present; else keep hints."""
    llm = _resolve_client(client)
    if llm is None:
        return findings
    for f in findings[:max_items]:
        prompt = (f"Finding from {f.scanner}: {f.title}\n"
                  f"Target: {f.target}:{f.line}\n"
                  f"Detail: {f.description or '(none)'}\n"
                  f"Current hint: {f.remediation or '(none)'}\n"
                  "Give the concrete fix.")
        try:
            reply = llm.chat([{"role": "system", "content": _SYS},
                              {"role": "user", "content": prompt}])
            reply = (reply or "").strip()
            if reply and "<" not in reply[:2]:        # ignore degenerate/placeholder output
                f.remediation = reply
        except Exception:
            pass                                       # keep the built-in hint
    return findings
