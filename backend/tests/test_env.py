"""load_local_env is env-first: values already in os.environ win, .env only fills gaps.
That contract is what keeps compose/Railway values authoritative and prod (no .env) a no-op."""

import os

from common.env import load_local_env


def test_env_first_and_gap_fill(tmp_path, monkeypatch):
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / ".env").write_text(
        'SYSDESIGN_TEST_FOO=from_file\nSYSDESIGN_TEST_BAR=bar_file\n# a comment line\n\nSYSDESIGN_TEST_QUOTED="quoted value"\n'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SYSDESIGN_TEST_FOO", "from_env")
    monkeypatch.delenv("SYSDESIGN_TEST_BAR", raising=False)
    monkeypatch.delenv("SYSDESIGN_TEST_QUOTED", raising=False)

    load_local_env()

    assert os.environ["SYSDESIGN_TEST_FOO"] == "from_env"  # existing env value wins
    assert os.environ["SYSDESIGN_TEST_BAR"] == "bar_file"  # gap filled from the file
    assert os.environ["SYSDESIGN_TEST_QUOTED"] == "quoted value"  # surrounding quotes stripped
