"""Cluster configs must fit k3d's v1alpha5 schema: under `options` only
k3d/k3s/kubeconfig/runtime are allowed (kubeAPI is top level; k3d refuses
the file otherwise: "Additional property kubeAPI is not allowed")."""
import pathlib

import yaml

CLUSTERS = pathlib.Path(__file__).resolve().parents[1] / "clusters"
OPTION_KEYS = {"k3d", "k3s", "kubeconfig", "runtime"}


def test_options_only_hold_schema_keys():
    bad = {}
    for f in sorted(CLUSTERS.glob("*.yaml")):
        extra = set((yaml.safe_load(f.read_text()).get("options") or {})) - OPTION_KEYS
        if extra:
            bad[f.name] = sorted(extra)
    assert not bad, bad
