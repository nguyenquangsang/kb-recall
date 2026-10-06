import json

import pytest

from kb_recall import server


@pytest.fixture
def kb_env(tmp_path, monkeypatch):
    """Isolate server.py's KB_ROOT/CONFIG_PATH/LOG_FILE/LOG_JSONL into tmp_path
    so tests never touch the real ~/.recall-mcp. Registers one project, "myproj"."""
    kb_root = tmp_path / "kb_root"
    kb_root.mkdir()
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"projects": [str(tmp_path / "myproj")]}))

    monkeypatch.setattr(server, "KB_ROOT", kb_root)
    monkeypatch.setattr(server, "CONFIG_PATH", config_path)
    monkeypatch.setattr(server, "LOG_FILE", kb_root / "usage.log")
    monkeypatch.setattr(server, "LOG_JSONL", kb_root / "usage.jsonl")
    return kb_root
