"""End-to-end lifecycle test: drive the MCP tools in sequence against one
feature KB and assert state persists correctly across calls.

This complements the per-function unit tests in test_server.py, which each
seed their own fixture state. Here one KB is created by init_feature and then
read/written by the other tools, so a regression in the interplay (e.g. a save
whose promotion leaves README/memories out of sync with what load reads back)
surfaces as a single trace.
"""

import re

import pytest

from kb_recall import server


@pytest.fixture
def fixed_ids(monkeypatch):
    """Deterministic username and per-save entry ids, mirroring test_server.py."""
    monkeypatch.setattr(server, "_resolve_username", lambda confirmed="": "alice")
    ids = iter(["aaa111", "bbb222", "ccc333", "ddd444", "eee555"])
    monkeypatch.setattr(server.secrets, "token_hex", lambda n: next(ids))
    return "alice"


def _section(d, name):
    text = (d / "README.md").read_text()
    return text.split(f"<{name}>")[1].split(f"</{name}>")[0]


def _from_marker(text, entry_id):
    return re.search(rf"<!-- from:{entry_id}(?: \d{{4}}-\d{{2}}-\d{{2}})? -->", text)


def test_full_tool_lifecycle(kb_env, fixed_ids):
    slug = "payment-gateway"

    # 1. init_feature creates the KB directory + index row.
    created = server.init_feature(
        name="Payment Gateway",
        slug=slug,
        summary="Stripe checkout + webhook reconciliation",
        project="myproj",
        branch="feat/payment-gateway",
    )
    d = kb_env / "myproj" / slug
    assert f"Created feature KB '{slug}'" in created
    assert (d / "README.md").exists()
    assert (d / "memories-alice.md").exists()
    assert slug in (kb_env / "myproj" / "features.md").read_text()

    # 2. save_memory([gotcha]) promotes into critical_warnings with a marker.
    gotcha = "**[gotcha] pooled conns are per-process**\nWhat: w\nWhy: y\nApply: a"
    r = server.save_memory(slug=slug, content=gotcha, project="myproj")
    assert "Promoted to <critical_warnings>" in r
    critical = _section(d, "critical_warnings")
    assert "pooled conns are per-process" in critical
    assert _from_marker(critical, "aaa111")

    # 3. save_memory([decision]) promotes into architecture.
    decision = "**[decision] chose append-only Postgres**\nWhat: w\nWhy: y\nApply: a"
    r = server.save_memory(slug=slug, content=decision, project="myproj")
    assert "Promoted to <architecture>" in r
    assert "chose append-only Postgres" in _section(d, "architecture")

    # 4. load_feature_context reads both back, under the project/slug header.
    loaded = server.load_feature_context(slug=slug, project="myproj")
    assert "myproj/payment-gateway" in loaded
    assert "pooled conns are per-process" in loaded
    assert "chose append-only Postgres" in loaded

    # 5. search_features finds the KB from the gotcha's distinctive words.
    found = server.search_features(query="pooled conns per-process", project="myproj")
    assert slug in found

    # 6. update_readme: diff first (writes nothing), then confirm (writes).
    rule = "**[rule] payment must be idempotent**"
    before = (d / "README.md").read_text()
    diff = server.update_readme(
        slug=slug, section="business_rules", content=rule, project="myproj"
    )
    assert diff  # a diff came back
    assert (d / "README.md").read_text() == before  # nothing written yet
    confirmed = server.update_readme(
        slug=slug,
        section="business_rules",
        content=rule,
        project="myproj",
        confirm=True,
    )
    assert "Updated <business_rules>" in confirmed
    assert "payment must be idempotent" in _section(d, "business_rules")

    # 7. report_miss records a [MISS] entry in the memories journal.
    r = server.report_miss(
        slug=slug, description="Forgot webhooks are unordered.", project="myproj"
    )
    assert "[MISS]" in r
    assert "Forgot webhooks are unordered." in (d / "memories-alice.md").read_text()
