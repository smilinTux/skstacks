import os
from skred.denylist import main

PAT = r"\bacmehost\d{4}\b"  # neutral: this file is public, so no real estate values


def _deny(tmp_path, *lines):
    f = tmp_path / "deny.txt"
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(f)


def _tree(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    return root


def test_clean_tree_passes(tmp_path, capsys):
    root = _tree(tmp_path)
    (root / "a.md").write_text("nothing here\n")
    assert main([str(root), "--denylist", _deny(tmp_path, PAT)]) == 0


def test_hit_is_reported_without_echoing_pattern_or_match(tmp_path, capsys):
    root = _tree(tmp_path)
    (root / "a.md").write_text("line one\nbuild host acmehost1003\n")
    assert main([str(root), "--denylist", _deny(tmp_path, PAT)]) == 1
    out = capsys.readouterr().out
    assert "a.md:2: pattern 1" in out
    assert "acmehost1003" not in out and "acmehost" not in out


def test_matching_is_case_insensitive(tmp_path):
    root = _tree(tmp_path)
    (root / "a.md").write_text("ACMEHOST1003\n")
    assert main([str(root), "--denylist", _deny(tmp_path, PAT)]) == 1


def test_comments_and_blank_lines_are_not_patterns(tmp_path):
    root = _tree(tmp_path)
    (root / "a.md").write_text("# a comment line\n\n")
    assert main([str(root), "--denylist", _deny(tmp_path, "# a comment", "", PAT)]) == 0


def test_vault_ciphertext_fails_without_denylist(tmp_path, capsys):
    root = _tree(tmp_path)
    (root / "x_vault.yml").write_text("$ANSIBLE_VAULT;1.1;AES256\n6162\n")
    assert main([str(root)]) == 1
    assert "x_vault.yml:1: vault" in capsys.readouterr().out


def test_binary_file_is_skipped(tmp_path):
    root = _tree(tmp_path)
    (root / "rg").write_bytes(b"\x7fELF\x00\x00acmehost1003\xff\xfe")
    assert main([str(root), "--denylist", _deny(tmp_path, PAT)]) == 0


def test_non_utf8_text_does_not_crash(tmp_path):
    root = _tree(tmp_path)
    (root / "latin1.txt").write_bytes("caf\xe9 acmehost1003\n".encode("latin-1"))
    assert main([str(root), "--denylist", _deny(tmp_path, PAT)]) == 1


def test_git_and_node_modules_are_skipped(tmp_path):
    root = _tree(tmp_path)
    for d in (".git", "node_modules"):
        (root / d).mkdir()
        (root / d / "f").write_text("acmehost1003\n")
    (root / "real.md").write_text("clean\n")  # so the tree is not empty
    assert main([str(root), "--denylist", _deny(tmp_path, PAT)]) == 0


def test_symlinks_are_not_followed(tmp_path):
    root = _tree(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("acmehost1003\n")
    os.symlink(outside, root / "link.txt")
    (root / "real.md").write_text("clean\n")  # so the tree is not empty
    assert main([str(root), "--denylist", _deny(tmp_path, PAT)]) == 0


def test_empty_denylist_refuses_to_pass(tmp_path, capsys):
    root = _tree(tmp_path)
    assert main([str(root), "--denylist", _deny(tmp_path, "", "# only comments")]) == 2
    assert "empty" in capsys.readouterr().err


def test_missing_denylist_refuses_to_pass(tmp_path):
    root = _tree(tmp_path)
    assert main([str(root), "--denylist", str(tmp_path / "nope.txt")]) == 2


def test_bad_regex_reports_number_only(tmp_path, capsys):
    root = _tree(tmp_path)
    assert main([str(root), "--denylist", _deny(tmp_path, PAT, "secret(unclosed")]) == 2
    err = capsys.readouterr().err
    assert "pattern 2" in err and "secret" not in err


def test_missing_root_refuses_to_pass(tmp_path, capsys):
    """A gate that scanned nothing must not report clean."""
    assert main([str(tmp_path / "nope"), "--denylist", _deny(tmp_path, PAT)]) == 2
    assert "not a directory" in capsys.readouterr().err


def test_root_that_is_a_file_refuses_to_pass(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("x\n")
    assert main([str(f), "--denylist", _deny(tmp_path, PAT)]) == 2


def test_empty_tree_refuses_to_pass(tmp_path, capsys):
    root = _tree(tmp_path)
    assert main([str(root), "--denylist", _deny(tmp_path, PAT)]) == 2
    assert "0 files" in capsys.readouterr().err


def test_reports_files_scanned(tmp_path, capsys):
    root = _tree(tmp_path)
    (root / "a.md").write_text("ok\n")
    (root / "b.md").write_text("ok\n")
    assert main([str(root), "--denylist", _deny(tmp_path, PAT)]) == 0
    assert "2 files scanned" in capsys.readouterr().out


def test_non_utf8_denylist_is_unusable_not_a_finding(tmp_path, capsys):
    root = _tree(tmp_path)
    (root / "a.md").write_text("fine\n")
    bad = tmp_path / "deny.txt"
    bad.write_bytes(b"\xff\xfe\xfa not utf-8\n")
    assert main([str(root), "--denylist", str(bad)]) == 2
    assert "not UTF-8" in capsys.readouterr().err


def test_unreadable_file_is_reported_not_a_crash(tmp_path, capsys):
    root = _tree(tmp_path)
    locked = root / "locked.md"
    locked.write_text("acmehost1003\n")
    locked.chmod(0)
    try:
        assert main([str(root), "--denylist", _deny(tmp_path, PAT)]) == 1
        assert "locked.md:0: unreadable" in capsys.readouterr().out
    finally:
        locked.chmod(0o600)


def test_fifo_is_skipped_instead_of_hanging(tmp_path):
    import signal
    root = _tree(tmp_path)
    (root / "a.md").write_text("fine\n")
    os.mkfifo(root / "pipe")
    signal.signal(signal.SIGALRM, lambda *a: (_ for _ in ()).throw(TimeoutError("hung on FIFO")))
    signal.alarm(5)
    try:
        assert main([str(root), "--denylist", _deny(tmp_path, PAT)]) == 0
    finally:
        signal.alarm(0)


def test_utf16_file_is_searched_not_skipped_as_binary(tmp_path):
    root = _tree(tmp_path)
    (root / "notes.txt").write_text("build host acmehost1003\n", encoding="utf-16")
    assert main([str(root), "--denylist", _deny(tmp_path, PAT)]) == 1


def test_worktree_git_pointer_file_is_skipped(tmp_path):
    # In a `git worktree`, .git is a FILE ("gitdir: /home/<user>/...") that
    # holds a local path; it is plumbing, never content, so it must not match.
    root = _tree(tmp_path)
    (root / ".git").write_text("gitdir: /home/acmehost1003/repo/.git/worktrees/x\n")
    (root / "a.md").write_text("fine\n")
    assert main([str(root), "--denylist", _deny(tmp_path, PAT)]) == 0
