"""tools/changelog_placement.py on a throwaway git repo."""
import subprocess
import sys
from pathlib import Path

TOOL = Path(__file__).resolve().parent / "changelog_placement.py"

BASE = """# Changelog

## Unreleased

- one

## skstacks-v1.0.1 - 2026-01-02

- fix a

## skstacks-v1.0.0 - 2026-01-01

- first
"""


def repo(tmp_path, new, tag=None):
    def git(*a):
        subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)
    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "CHANGELOG.md").write_text(BASE)
    git("add", "."); git("commit", "-qm", "base")
    if tag:
        git("tag", tag)
    git("checkout", "-qb", "pr")
    (tmp_path / "CHANGELOG.md").write_text(new)
    git("commit", "-qam", "pr")
    return subprocess.run([sys.executable, str(TOOL), "main"], cwd=tmp_path, capture_output=True, text=True)


def test_addition_in_top_section_passes(tmp_path):
    r = repo(tmp_path, BASE.replace("- one\n", "- one\n- two\n"))
    assert r.returncode == 0, r.stdout + r.stderr


def test_addition_under_a_tagged_release_fails(tmp_path):
    r = repo(tmp_path, BASE.replace("- fix a\n", "- fix a\n- sneaked in\n"))
    assert r.returncode == 1
    assert "skstacks-v1.0.1" in r.stdout


def test_moving_old_lines_up_passes(tmp_path):
    new = BASE.replace("- one\n", "- one\n- fix a\n").replace("\n- fix a\n\n## skstacks-v1.0.0", "\n\n## skstacks-v1.0.0")
    r = repo(tmp_path, new)
    assert r.returncode == 0, r.stdout + r.stderr


def test_new_release_heading_on_top_passes(tmp_path):
    new = BASE.replace("## Unreleased\n\n- one\n", "## skstacks-v1.0.2 - 2026-01-03\n\n- one\n- two\n")
    r = repo(tmp_path, new)
    assert r.returncode == 0, r.stdout + r.stderr


def test_blank_lines_below_are_ignored(tmp_path):
    r = repo(tmp_path, BASE.replace("- first\n", "- first\n\n"))
    assert r.returncode == 0, r.stdout + r.stderr


TAGGED_TOP = BASE.replace("## Unreleased\n\n- one\n\n", "")


def test_addition_under_a_tagged_top_section_fails(tmp_path):
    """The #474 case: the top heading was tagged without the new notes."""
    r = repo(tmp_path, TAGGED_TOP.replace("- fix a\n", "- fix a\n- late note\n"), tag="skstacks-v1.0.1")
    assert r.returncode == 1 and "skstacks-v1.0.1" in r.stdout


def test_untagged_top_release_section_accepts_additions(tmp_path):
    r = repo(tmp_path, TAGGED_TOP.replace("- fix a\n", "- fix a\n- note\n"))
    assert r.returncode == 0, r.stdout + r.stderr


def test_fresh_unreleased_above_a_tagged_top_passes(tmp_path):
    new = TAGGED_TOP.replace("## skstacks-v1.0.1", "## Unreleased\n\n- new\n\n## skstacks-v1.0.1", 1)
    r = repo(tmp_path, new, tag="skstacks-v1.0.1")
    assert r.returncode == 0, r.stdout + r.stderr
