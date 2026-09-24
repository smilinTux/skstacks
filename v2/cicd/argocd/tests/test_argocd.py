"""Guard tests for the ArgoCD app-of-apps GitOps wiring."""
from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_ARGOCD = Path(__file__).resolve().parents[1]
_CANON = "https://github.com/smilinTux/skstacks.git"


def _doc(rel):
    return next(d for d in yaml.safe_load_all((_ARGOCD / rel).read_text()) if d)


def test_app_of_apps_points_at_apps_dir_on_canonical_repo():
    app = _doc("app-of-apps.yaml")
    assert app["kind"] == "Application"
    assert app["spec"]["source"]["repoURL"] == _CANON
    assert app["spec"]["source"]["path"].endswith("cicd/argocd/apps")


def test_child_apps_present_for_platform_secrets_and_service():
    names = {p.name for p in (_ARGOCD / "apps").glob("*.yaml")}
    assert {"platform.yaml", "secrets-eso.yaml", "skfence.yaml"} <= names


def test_platform_app_deploys_prod_overlay():
    app = _doc("apps/platform.yaml")
    assert app["spec"]["source"]["path"] == "v2/platform/kubernetes/overlays/prod"


def test_project_whitelists_gatewayclass_not_ingressclass():
    proj = _doc("argocd-project.yaml")
    wl = proj["spec"]["clusterResourceWhitelist"]
    kinds = {(w["group"], w["kind"]) for w in wl}
    assert ("gateway.networking.k8s.io", "GatewayClass") in kinds
    assert ("networking.k8s.io", "IngressClass") not in kinds   # nginx era gone


def test_project_allows_traefik_helm_repo():
    proj = _doc("argocd-project.yaml")
    assert any("traefik" in r for r in proj["spec"]["sourceRepos"])


def test_no_changeme_repo_placeholders():
    for f in (_ARGOCD).rglob("*.yaml"):
        assert "CHANGEME_GIT_HOST" not in f.read_text(), f"unresolved repo in {f.name}"
