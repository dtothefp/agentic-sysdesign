"""load_local_env is env-first: values already in os.environ win, .env only fills gaps.
That contract is what keeps compose/Railway values authoritative and prod (no .env) a no-op.
The loader anchors on the `.moon` dir (the workspace-root marker), so the fake root here
needs one; a bare .env without the marker must be ignored (that's the nested-workspace guard)."""

import os

from common.env import load_local_env


def test_env_first_and_gap_fill(tmp_path, monkeypatch):
    (tmp_path / ".moon").mkdir()
    (tmp_path / ".env").write_text(
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


def test_env_without_moon_marker_is_ignored(tmp_path, monkeypatch):
    # No .moon dir: this .env could belong to some parent workspace, so it must not load.
    (tmp_path / ".env").write_text("SYSDESIGN_TEST_STRAY=should_not_load\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SYSDESIGN_TEST_STRAY", raising=False)

    load_local_env()

    assert "SYSDESIGN_TEST_STRAY" not in os.environ
