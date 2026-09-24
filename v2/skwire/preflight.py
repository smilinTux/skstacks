"""
skwire preflight — the consent-gated env scan + the Socratic opener.

Flow: "Mind if I take a quick look at your environment?" → `probe_env()` →
tailored `suggest()` (compute/model, orchestrator, exposure) + the
"deploy here or elsewhere?" question. The probe is injectable (DI) so the logic
is testable without touching the real system, and so a consuming project can
supply its own probe.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field


@dataclass
class EnvProfile:
    os: str
    cpu_cores: int
    ram_gb: float
    gpus: list[str]
    disk_free_gb: float
    has_docker: bool
    has_kubectl: bool
    has_ollama: bool
    public_ip: str | None
    free_ports: set[int]


@dataclass
class FakeProbe:
    """Test/embeddable probe — same fields as EnvProfile, mutable."""
    os: str = "linux"
    cpu_cores: int = 4
    ram_gb: float = 8
    gpus: list[str] = field(default_factory=list)
    disk_free_gb: float = 50
    has_docker: bool = False
    has_kubectl: bool = False
    has_ollama: bool = False
    public_ip: str | None = None
    free_ports: set[int] = field(default_factory=set)


class SystemProbe:
    """Best-effort real-system probe (stdlib only, NO network calls without consent)."""
    @property
    def os(self) -> str:
        import platform
        return platform.system().lower()

    @property
    def cpu_cores(self) -> int:
        return os.cpu_count() or 1

    @property
    def ram_gb(self) -> float:
        # POSIX (Linux/macOS)
        try:
            return round(os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 1e9, 1)
        except (ValueError, OSError, AttributeError):
            pass
        # Windows
        try:
            import ctypes

            class _MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            ms = _MS()
            ms.dwLength = ctypes.sizeof(_MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))  # type: ignore[attr-defined]
            return round(ms.ullTotalPhys / 1e9, 1)
        except Exception:
            return 0.0

    @property
    def gpus(self) -> list[str]:
        return ["nvidia (nvidia-smi present)"] if shutil.which("nvidia-smi") else []

    @property
    def disk_free_gb(self) -> float:
        # Use the platform-appropriate root (Windows has no "/").
        root = (os.environ.get("SystemDrive", "C:") + os.sep) if os.name == "nt" else os.sep
        try:
            return round(shutil.disk_usage(root).free / 1e9, 1)
        except OSError:
            return 0.0

    has_docker = property(lambda self: shutil.which("docker") is not None)
    has_kubectl = property(lambda self: shutil.which("kubectl") is not None)
    has_ollama = property(lambda self: shutil.which("ollama") is not None)
    public_ip = None                      # don't phone home in a probe; ask/consent separately
    free_ports = property(lambda self: set())


def probe_env(probe=None) -> EnvProfile:
    p = probe if probe is not None else SystemProbe()
    return EnvProfile(
        os=p.os, cpu_cores=p.cpu_cores, ram_gb=p.ram_gb, gpus=list(p.gpus),
        disk_free_gb=p.disk_free_gb, has_docker=p.has_docker, has_kubectl=p.has_kubectl,
        has_ollama=p.has_ollama, public_ip=p.public_ip, free_ports=set(p.free_ports),
    )


@dataclass(frozen=True)
class Suggestion:
    kind: str           # "suggestion" | "question"
    text: str


def suggest(p: EnvProfile) -> list[Suggestion]:
    out: list[Suggestion] = []
    # Always establish WHERE — this drives every downstream choice.
    out.append(Suggestion("question",
        "Are you deploying here, or somewhere else (another box / cloud)?"))

    # Compute + AI-model tier
    if p.gpus and p.ram_gb >= 16:
        out.append(Suggestion("suggestion",
            f"You've got a GPU ({p.gpus[0]}) and {p.ram_gb:g}GB RAM — I can run the "
            f"AI-guided install with a local model, fully offline."))
    elif p.ram_gb < 8:
        out.append(Suggestion("suggestion",
            "Limited RAM here — I'd use a lean profile and a hosted-model fallback "
            "for the AI guidance (your call; everything works without AI too)."))

    # Orchestrator
    if p.has_kubectl:
        out.append(Suggestion("suggestion",
            "I see kubectl — I can deploy onto your existing Kubernetes cluster."))
    elif p.has_docker:
        out.append(Suggestion("suggestion",
            "Docker's installed — simplest path is Docker Swarm on this node."))
    else:
        out.append(Suggestion("question",
            "No container runtime found — want me to install Docker (for Swarm)?"))

    # Exposure (only relevant if deploying here without a public IP)
    if not p.public_ip:
        out.append(Suggestion("suggestion",
            "No public IP detected — for remote access I'd use cloudflared/Pangolin "
            "(tunnel) or Tailscale (mesh); the LAN stays the always-on baseline."))
    return out
