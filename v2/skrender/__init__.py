"""skrender — descriptor (app.yaml) → deployable Swarm stack + K8s manifests."""
from __future__ import annotations
from .render import load_descriptor, render_swarm, render_k8s, SecretRef
__all__ = ["load_descriptor", "render_swarm", "render_k8s", "SecretRef"]
