import json

import pytest

from kb_recall import server


def fake_home(home):
    """Env overrides that make `Path.home()` resolve to `home` on every platform.

    `HOME` alone is not enough. Windows' `ntpath.expanduser()` reads
    `USERPROFILE` (falling back to `HOMEDRIVE`+`HOMEPATH`) and never consults
    `HOME` at all, so a subprocess isolated only by `HOME` would silently read
    the real profile — and write into it, since every KB path is built from
    `Path.home()`.
    """
    return {"HOME": str(home), "USERPROFILE": str(home)}


@pytest.fixture
def kb_env(tmp_path, monkeypatch):
    """Isolate server.py's KB_ROOT/CONFIG_PATH/LOG_FILE/LOG_JSONL into tmp_path
    so tests never touch the real ~/.recall-mcp. Registers one project, "myproj"."""
    kb_root = tmp_path / "kb_root"
    kb_root.mkdir()
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"projects": [str(tmp_path / "myproj")]}), encoding="utf-8"
    )

    monkeypatch.setattr(server, "KB_ROOT", kb_root)
    monkeypatch.setattr(server, "CONFIG_PATH", config_path)
    monkeypatch.setattr(server, "LOG_FILE", kb_root / "usage.log")
    monkeypatch.setattr(server, "LOG_JSONL", kb_root / "usage.jsonl")
    return kb_root
