"""
skwire — the universal bootstrapper / wiring fabric.

Embeddable, pure-stdlib core. Public API:

    from skwire import (
        probe_env, suggest,                 # preflight (scan + ask)
        build_plan, Plan, WireEdge,         # resolver (graph -> plan)
        explain,                            # narrate the plan
        approve, verify_approval,           # the "heck yeah" handshake
        Pack, register_pack, all_nodes,     # pack n ship
        SecretStore, Injector, Signer, Probe,  # extension points
    )
"""
from __future__ import annotations

from .models import WireEdge, Plan
from .resolver import build_plan, WireError, WireCycleError, MissingProviderError
from .explain import explain
from .approval import approve, verify_approval, ApprovalToken
from .preflight import probe_env, suggest, EnvProfile, Suggestion, FakeProbe, SystemProbe
from .protocols import SecretStore, Injector, Signer, Probe
from .branding import Branding, set_branding, get_branding, active_branding, banner
from .pack import (
    Pack, register_pack, get_pack, list_packs, all_nodes, clear_registry,
    load_packs_from_entrypoints,
)
from .mint import RandomSecretStore, mint_secrets
from .inject import RecordingInjector, InjectCall, EnvFileInjector, JsonConfigInjector, ApiPostInjector
from .executor import execute, rotate, ExecutionResult
from .rotation import (
    rotation_schedule, due_for_rotation, cron_for, offer_rotation_schedule, RotationOffer,
)
from .redundancy import critical_nodes, redundancy_advice, make_redundant, MANTRA
from .stages import Stage, run_stages, completed_stages, reset_stages, StageResult
from .doctor import doctor
from .conversation import Option, Choice, auto_question, option_choices, questions_for
from .nextstep import next_step, next_steps, Step
from .chat import interpret, Intent
from .llm import (
    converse, ConverseResult, build_system_prompt, parse_action,
    LLMClient, OpenAIClient, OllamaClient, resolve_client, SMALL_MODEL,
)
from .catalog import (
    Offering, register_offering, list_offerings, install_offering,
    load_builtin_catalog, load_catalog_file, load_remote_catalog, load_catalog,
    clear_catalog, recommend, best_offering, DEFAULT_CATALOG_URL,
)

__version__ = "0.1.0"

__all__ = [
    "WireEdge", "Plan",
    "build_plan", "WireError", "WireCycleError", "MissingProviderError",
    "explain",
    "approve", "verify_approval", "ApprovalToken",
    "probe_env", "suggest", "EnvProfile", "Suggestion", "FakeProbe", "SystemProbe",
    "SecretStore", "Injector", "Signer", "Probe",
    "Branding", "set_branding", "get_branding", "active_branding", "banner",
    "Pack", "register_pack", "get_pack", "list_packs", "all_nodes", "clear_registry",
    "load_packs_from_entrypoints",
    "RandomSecretStore", "mint_secrets", "RecordingInjector", "InjectCall",
    "EnvFileInjector", "JsonConfigInjector", "ApiPostInjector",
    "execute", "rotate", "ExecutionResult",
    "rotation_schedule", "due_for_rotation", "cron_for", "offer_rotation_schedule", "RotationOffer",
    "Offering", "register_offering", "list_offerings", "install_offering",
    "load_builtin_catalog", "load_catalog_file", "load_remote_catalog", "load_catalog",
    "clear_catalog", "recommend", "best_offering", "DEFAULT_CATALOG_URL",
    "critical_nodes", "redundancy_advice", "make_redundant", "MANTRA",
    "Stage", "run_stages", "completed_stages", "reset_stages", "StageResult",
    "doctor",
    "Option", "Choice", "auto_question", "option_choices", "questions_for",
    "next_step", "next_steps", "Step",
    "interpret", "Intent",
    "converse", "ConverseResult", "build_system_prompt", "parse_action",
    "LLMClient", "OpenAIClient", "OllamaClient", "resolve_client", "SMALL_MODEL",
    "__version__",
]
