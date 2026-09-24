from skred.cli import main


def test_gate_refuses_to_pass_when_every_target_is_out_of_scope(capsys):
    # example.org is not in the loopback-only default scope, so nothing is scanned
    assert main(["gate", "example.org"]) == 2
    assert "refusing to pass" in capsys.readouterr().err


def test_gate_still_passes_an_in_scope_clean_tree(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text("nothing\n")
    monkeypatch.chdir(tmp_path)
    assert main(["gate", str(tmp_path)]) == 0
