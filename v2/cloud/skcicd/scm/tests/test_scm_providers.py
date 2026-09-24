"""Guard tests for the pluggable SCM/Git providers (GitOps repo + CI backend)."""
from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_SCM = Path(__file__).resolve().parents[1]


def test_readme_covers_the_provider_ladder():
    r = (_SCM / "README.md").read_text().lower()
    for p in ("skgit", "gitlab", "github", "gitea", "forgejo"):
        assert p in r
    assert "sovereign" in r and "gitops repo" in r


def test_gitlab_selfhosted_deploy_present():
    sw = yaml.safe_load((_SCM / "gitlab" / "swarm-compose.yml").read_text())
    assert "gitlab/gitlab-ce" in sw["services"]["gitlab"]["image"]
    hc = yaml.safe_load((_SCM / "gitlab" / "k8s-helmchart.yaml").read_text())
    assert hc["spec"]["chart"] == "gitlab"


def test_skgit_forgejo_is_sovereign_default():
    sw = yaml.safe_load((_SCM / "skgit" / "swarm-compose.yml").read_text())
    assert "forgejo" in sw["services"]["forgejo"]["image"]
    app = yaml.safe_load((_SCM.parent / "app.yaml").read_text())
    assert app["scm_default"] == "skgit"
    assert set(app["scm_providers"]) >= {"skgit", "gitlab", "github", "gitea"}


def test_no_inline_secrets_env_file_pattern():
    for f in _SCM.rglob("*compose*.yml"):
        doc = yaml.safe_load(f.read_text())
        for svc in doc["services"].values():
            # creds come from env_file / secret backend, never inline values
            assert "env_file" in svc or "environment" in svc
            blob = str(svc.get("environment", ""))
            assert "password=" not in blob.lower()
