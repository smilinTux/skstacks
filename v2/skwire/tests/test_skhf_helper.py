"""The skhf helper has the safetensors preset + multi include/exclude + the disk fix."""
from pathlib import Path
import shutil, subprocess

H = Path(__file__).resolve().parents[1] / "packs" / "skhf" / "skhf"


def test_helper_exists_and_is_valid_bash():
    assert H.is_file()
    if shutil.which("bash"):
        assert subprocess.run(["bash", "-n", str(H)]).returncode == 0


def test_safetensors_preset_and_shortcuts_present():
    t = H.read_text()
    assert "--safetensors" in t and "*.safetensors" in t
    assert "--include) INCLUDES+=" in t and "--exclude) EXCLUDES+=" in t


def test_gguf_preset_and_live_search_present():
    t = H.read_text()
    assert "--gguf) INCLUDES+=" in t and "*.gguf" in t
    assert "_search" in t and "huggingface.co/api/" in t   # live HF search
    assert "_popular" in t and "fineweb" in t              # big training datasets


def test_the_disk_fix_sets_hf_home_not_just_hub_cache():
    t = H.read_text()
    assert 'export HF_HOME=' in t                    # the cascade fix
    assert "HF_XET_HIGH_PERFORMANCE=1" in t          # not the dead hf_transfer
    assert "--dry-run" in t and "df -h" in t         # pre-flight
    assert "until hf download" in t                  # retry/resume loop
