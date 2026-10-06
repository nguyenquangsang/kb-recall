import json
import re
from datetime import date
from pathlib import Path

import pytest

from kb_recall import server

# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


def make_feature(kb_root, project_name, slug, readme="", memories=None):
    """Create KB_ROOT/<project_name>/<slug>/ with a README.md and memories files."""
    d = kb_root / project_name / slug
    d.mkdir(parents=True)
    (d / "README.md").write_text(readme)
    for username, content in (memories or {}).items():
        (d / f"memories-{username}.md").write_text(content)
    return d


def _kf_readme(*bullets):
    lines = "\n".join(bullets)
    return f"<key_files>\n{lines}\n</key_files>"


def assert_marker(text, entry_id):
    """Assert `text` carries `entry_id`'s `from:` provenance marker.

    The date is optional on purpose: markers written since 2026-10-03 carry the
    source entry's date, older ones (and hand-written fixture READMEs) do not,
    and both are valid markers the server must accept. Pinning one shape here
    would silently stop covering the other.
    """
    assert re.search(rf"<!-- from:{entry_id}(?: \d{{4}}-\d{{2}}-\d{{2}})? -->", text), (
        f"no marker for {entry_id} in: {text}"
    )


# ---------------------------------------------------------------------------
# _merge_memories
# ---------------------------------------------------------------------------


class TestMergeMemories:
    def test_sorts_by_date_descending(self, tmp_path):
        f = tmp_path / "memories-a.md"
        f.write_text(
            "- **2026-01-01** [id:aaaa]: **[gotcha] old**\n\n"
            "- **2026-06-01** [id:bbbb]: **[gotcha] new**\n"
        )
        merged, malformed = server._merge_memories([f])
        assert merged.index("new") < merged.index("old")
        assert malformed == 0

    def test_hides_superseded_entry_keeps_replacement(self, tmp_path):
        f = tmp_path / "memories-a.md"
        f.write_text(
            "- **2026-01-01** [id:aaaa]: **[gotcha] original**\n\n"
            "- **2026-02-01** [id:bbbb]: [gotcha][supersedes:aaaa] replacement\n"
        )
        merged, _ = server._merge_memories([f])
        assert "original" not in merged
        assert "replacement" in merged

    def test_hides_resolved_entry_keeps_closure(self, tmp_path):
        f = tmp_path / "memories-a.md"
        f.write_text(
            "- **2026-01-01** [id:aaaa]: **[bug] the bug**\n\n"
            "- **2026-02-01** [id:bbbb]: [resolved:aaaa] fixed now\n"
        )
        merged, _ = server._merge_memories([f])
        assert "the bug" not in merged
        assert "fixed now" in merged

    def test_counts_malformed_entry_missing_date(self, tmp_path):
        f = tmp_path / "memories-a.md"
        f.write_text(
            "- **2026-01-01** [id:aaaa]: **[gotcha] has date**\n\n"
            "- **[gotcha] no date, hand-edited**\n"
        )
        merged, malformed = server._merge_memories([f])
        assert malformed == 1
        # malformed entry sorts as oldest ("0000-00-00"), so after the dated one
        assert merged.index("has date") < merged.index("no date")

    def test_blank_line_inside_entry_keeps_the_body(self, tmp_path):
        """A blank line inside an entry is content, not a delimiter.

        save_memory writes the model's content verbatim, and a blank line
        between What/Why/Apply paragraphs is ordinary Markdown formatting.
        Splitting entries on it turned such an entry into a title-only line
        and discarded the body — invisible through every tool.
        """
        f = tmp_path / "memories-a.md"
        f.write_text(
            "- **2026-01-01** [id:aaaa]: **[gotcha] has a body**\n"
            "\n"
            "What: the body survives.\n"
            "Why: blank lines are not delimiters.\n"
            "\n"
            "- **2026-02-01** [id:bbbb]: **[gotcha] second**\n"
        )
        merged, malformed = server._merge_memories([f])
        assert malformed == 0
        assert "the body survives." in merged
        assert "blank lines are not delimiters." in merged
        # The body must stay with ITS entry, not be adopted by the next one.
        # Newest sorts first, so entry "second" precedes entry "aaaa" and
        # everything from its marker on belongs to it.
        assert merged.index("second") < merged.index("[id:aaaa]")
        assert "the body survives." in merged[merged.index("[id:aaaa]") :]

    def test_superseded_entry_still_hides_its_reattached_body(self, tmp_path):
        """Body reattachment must not defeat the supersede filter.

        The real store has damaged entries whose body belongs to an entry that
        [supersedes]/[resolved] already hides. Reattaching the body then
        filters it together with its entry — still hidden, by design.
        """
        f = tmp_path / "memories-a.md"
        f.write_text(
            "- **2026-01-01** [id:aaaa]: **[gotcha] original**\n"
            "\n"
            "What: original body, must stay hidden.\n"
            "\n"
            "- **2026-02-01** [id:bbbb]: [gotcha][supersedes:aaaa] replacement\n"
        )
        merged, malformed = server._merge_memories([f])
        assert malformed == 0
        assert "original" not in merged
        assert "original body" not in merged
        assert "replacement" in merged

    def test_body_line_opening_with_marker_splits_into_malformed_entry(self, tmp_path):
        """Documented cost of dropping the blank-line delimiter.

        A body line that itself begins with "- **" now opens an entry even
        with no blank line before it. The split tail is counted malformed and
        sorts last, so it is visible — unlike a silently discarded body.
        """
        f = tmp_path / "memories-a.md"
        f.write_text(
            "- **2026-01-01** [id:aaaa]: **[gotcha] entry**\n"
            "Apply: keep this line\n"
            "- **Important:** a body bullet\n"
        )
        merged, malformed = server._merge_memories([f])
        assert malformed == 1
        assert "keep this line" in merged
        assert "a body bullet" in merged

    def test_skips_file_preamble_before_the_first_entry(self, tmp_path):
        f = tmp_path / "memories-a.md"
        f.write_text(
            "# Engineering Memory — someone\n"
            "\n"
            "*Prepend-only.*\n"
            "\n"
            "---\n"
            "\n"
            "- **2026-01-01** [id:aaaa]: **[gotcha] first**\n"
        )
        merged, malformed = server._merge_memories([f])
        assert malformed == 0
        assert "Engineering Memory" not in merged
        assert "first" in merged

    def test_merges_across_multiple_files(self, tmp_path):
        f1 = tmp_path / "memories-a.md"
        f1.write_text("- **2026-01-01** [id:aaaa]: **[gotcha] from a**\n")
        f2 = tmp_path / "memories-b.md"
        f2.write_text("- **2026-02-01** [id:bbbb]: **[gotcha] from b**\n")
        merged, _ = server._merge_memories([f1, f2])
        assert "from a" in merged and "from b" in merged

    def test_new_6char_id_supersedes_old_4char_id(self, tmp_path):
        """Backward-compat: a new 6-hex-char entry can still hide an old 4-hex-char one."""
        f = tmp_path / "memories-a.md"
        f.write_text(
            "- **2026-01-01** [id:aaaa]: **[gotcha] original**\n\n"
            "- **2026-02-01** [id:a1b2c3]: [gotcha][supersedes:aaaa] replacement\n"
        )
        merged, _ = server._merge_memories([f])
        assert "original" not in merged
        assert "replacement" in merged

    def test_6char_id_entry_can_itself_be_superseded(self, tmp_path):
        f = tmp_path / "memories-a.md"
        f.write_text(
            "- **2026-01-01** [id:a1b2c3]: **[gotcha] original**\n\n"
            "- **2026-02-01** [id:d4e5f6]: [gotcha][supersedes:a1b2c3] replacement\n"
        )
        merged, _ = server._merge_memories([f])
        assert "original" not in merged
        assert "replacement" in merged


# ---------------------------------------------------------------------------
# _strip_readme_for_context / _strip_html_comments
# ---------------------------------------------------------------------------


class TestStripReadmeForContext:
    def test_strips_html_comments(self):
        text = "<overview>real <!-- hidden --> content</overview>"
        out = server._strip_readme_for_context(text)
        assert "hidden" not in out
        assert "real" in out and "content" in out

    def test_drops_empty_sections(self):
        text = (
            "<architecture>\n<!-- placeholder -->\n</architecture>\n"
            "<overview>x</overview>"
        )
        out = server._strip_readme_for_context(text)
        assert "<architecture>" not in out
        assert "<overview>x</overview>" in out

    def test_collapses_excess_blank_lines(self):
        out = server._strip_readme_for_context("a\n\n\n\n\nb")
        assert "\n\n\n" not in out

    def test_keep_markers_preserves_from_marker_only(self):
        text = (
            "<critical_warnings>**[gotcha] t**\n<!-- from:aaa111 -->\n"
            "<!-- placeholder -->\n</critical_warnings>"
        )
        out = server._strip_readme_for_context(text, keep_markers=True)
        assert_marker(out, "aaa111")
        assert "placeholder" not in out

    def test_default_strips_markers_too(self):
        text = "<critical_warnings>**[gotcha] t**\n<!-- from:aaa111 -->\n</critical_warnings>"
        out = server._strip_readme_for_context(text)
        assert "from:aaa111" not in out


class TestStripHtmlComments:
    def test_removes_comment_and_trims(self):
        assert server._strip_html_comments("  <!-- x -->text<!-- y -->  ") == "text"

    def test_no_comment_just_trims(self):
        assert server._strip_html_comments("  plain  ") == "plain"


# ---------------------------------------------------------------------------
# _key_files_index / _related_by_key_files / _unverified_key_files
# ---------------------------------------------------------------------------


class TestKeyFilesIndex:
    def test_indexes_bare_path_and_symbol_separately(self, kb_env):
        make_feature(
            kb_env,
            "myproj",
            "a",
            readme=_kf_readme("- `core/x.py::do_thing` — does a thing"),
        )
        file_index, symbol_index = server._key_files_index([Path("myproj")])
        assert file_index["core/x.py"] == ["a"]
        assert symbol_index["core/x.py::do_thing"] == ["a"]

    def test_multiple_backtick_tokens_per_bullet_all_captured(self, kb_env):
        make_feature(
            kb_env,
            "myproj",
            "a",
            readme=_kf_readme("- `a.py`, `b.py` — two real paths on one line"),
        )
        file_index, _ = server._key_files_index([Path("myproj")])
        assert "a.py" in file_index and "b.py" in file_index

    def test_skips_bare_symbol_name_not_path_shaped(self, kb_env):
        make_feature(
            kb_env,
            "myproj",
            "a",
            readme=_kf_readme("- `real/path.py` — mentions `RegistryAdapter` in prose"),
        )
        file_index, _ = server._key_files_index([Path("myproj")])
        assert "RegistryAdapter" not in file_index
        assert "real/path.py" in file_index

    def test_skips_template_placeholder(self, kb_env):
        make_feature(
            kb_env,
            "myproj",
            "a",
            readme=_kf_readme("- `{project-name}/features.md` — illustrative example"),
        )
        file_index, _ = server._key_files_index([Path("myproj")])
        assert not any("{" in p for p in file_index)

    def test_skips_non_path_slash_token(self, kb_env):
        # OI-2: a data identifier like `twc-reanalysis/v1` contains a `/` but
        # isn't a file -- must not be indexed as a path.
        make_feature(
            kb_env,
            "myproj",
            "a",
            readme=_kf_readme(
                "- `core/registry.json` — Source System Registry (entry: `twc-reanalysis/v1`)"
            ),
        )
        file_index, _ = server._key_files_index([Path("myproj")])
        assert "twc-reanalysis/v1" not in file_index
        assert "core/registry.json" in file_index

    def test_directory_reference_with_trailing_slash_still_indexed(self, kb_env):
        make_feature(
            kb_env,
            "myproj",
            "a",
            readme=_kf_readme(
                "- `services/some-service/` — dynamic lookup, no startup check"
            ),
        )
        file_index, _ = server._key_files_index([Path("myproj")])
        assert "services/some-service/" in file_index

    def test_descriptive_prefix_before_backtick_path_still_indexed(self, kb_env):
        # A bullet that leads with a label ("Tests:", "Pattern reference:", ...)
        # before the backtick-quoted path used to be skipped entirely because
        # the backtick had to be the very first thing after "- ". Relaxed so
        # the backtick can appear anywhere on the bullet line.
        make_feature(
            kb_env,
            "myproj",
            "a",
            readme=_kf_readme("- Tests: `tests/test_thing.py`, `tests/test_other.py`"),
        )
        file_index, _ = server._key_files_index([Path("myproj")])
        assert "tests/test_thing.py" in file_index
        assert "tests/test_other.py" in file_index


class TestRelatedByKeyFiles:
    def test_two_slugs_sharing_a_path_are_strong(self, kb_env):
        make_feature(kb_env, "myproj", "a", readme=_kf_readme("- `shared.py` — thing"))
        make_feature(kb_env, "myproj", "b", readme=_kf_readme("- `shared.py` — thing"))
        file_index, symbol_index = server._key_files_index([Path("myproj")])
        strong, weak = server._related_by_key_files("a", file_index, symbol_index)
        assert strong == {"b": ["shared.py"]}
        assert weak == {}

    def test_hub_path_demoted_to_weak_not_dropped(self, kb_env):
        # OI-1: previously a path shared by >=3 slugs vanished entirely.
        for slug in ("a", "b", "c"):
            make_feature(
                kb_env, "myproj", slug, readme=_kf_readme("- `hub.py` — core file")
            )
        file_index, symbol_index = server._key_files_index([Path("myproj")])
        strong, weak = server._related_by_key_files("a", file_index, symbol_index)
        assert strong == {}
        assert weak == {"b": ["hub.py"], "c": ["hub.py"]}

    def test_generic_basename_below_hub_threshold_is_strong_not_filtered(self, kb_env):
        # A boilerplate filename (__init__.py, pyproject.toml, ...) shared by
        # only 2 slugs is deliberately NOT code-filtered -- discounting an
        # obviously-generic match is left to Claude's own judgment when
        # reading the footer (see the key_files_hint prompt text), not a
        # maintained denylist. Only the hub threshold demotes to `weak` here.
        make_feature(
            kb_env, "myproj", "a", readme=_kf_readme("- `__init__.py` — package init")
        )
        make_feature(
            kb_env, "myproj", "b", readme=_kf_readme("- `__init__.py` — package init")
        )
        file_index, symbol_index = server._key_files_index([Path("myproj")])
        strong, weak = server._related_by_key_files("a", file_index, symbol_index)
        assert strong == {"b": ["__init__.py"]}
        assert weak == {}

    def test_exact_symbol_match_rescues_hub_path_into_strong(self, kb_env):
        make_feature(
            kb_env, "myproj", "a", readme=_kf_readme("- `hub.py::shared_fn` — core")
        )
        make_feature(
            kb_env, "myproj", "b", readme=_kf_readme("- `hub.py::shared_fn` — core")
        )
        make_feature(kb_env, "myproj", "c", readme=_kf_readme("- `hub.py` — core"))
        file_index, symbol_index = server._key_files_index([Path("myproj")])
        strong, weak = server._related_by_key_files("a", file_index, symbol_index)
        assert strong == {"b": ["hub.py::shared_fn"]}
        assert "b" not in weak  # rescued out of weak entirely
        assert weak == {"c": ["hub.py"]}


class TestUnverifiedKeyFiles:
    def test_missing_path_flagged_existing_path_not(self, tmp_path):
        proj_root = tmp_path / "myproj"
        proj_root.mkdir()
        (proj_root / "real.py").write_text("x")
        result = server._unverified_key_files(["real.py", "missing.py"], [proj_root])
        assert result == {"missing.py"}


class TestKeyFilesFormatHint:
    def test_empty_when_all_bullets_have_a_backtick_path(self):
        content = "- `src/foo.py` — does a thing\n- Tests: `tests/test_foo.py`"
        assert server._key_files_format_hint(content) == ""

    def test_flags_bare_path_with_no_backtick_at_all(self):
        # The template-style bug: path never wrapped in backticks anywhere,
        # only a trailing symbol name is -- unrecoverable by any parser change.
        content = "- src/foo.py — `SomeClass` does the thing"
        hint = server._key_files_format_hint(content)
        assert "1 bullet" in hint
        assert "backtick" in hint

    def test_counts_multiple_bad_bullets(self):
        content = (
            "- src/foo.py — `SomeClass`\n"
            "- src/bar.py — `OtherClass`\n"
            "- `src/good.py` — this one is fine"
        )
        hint = server._key_files_format_hint(content)
        assert "2 bullet" in hint

    def test_ignores_dash_lines_inside_html_comments(self):
        # A dash-line that's part of an HTML comment (e.g. a leftover template
        # example, or a note) isn't real key_files content and must not be
        # counted as a malformed bullet.
        content = "<!--\n- old draft note without any path\n-->\n- `real/path.py` — real thing"
        assert server._key_files_format_hint(content) == ""


# ---------------------------------------------------------------------------
# _kb_health_hints
# ---------------------------------------------------------------------------


class TestKbHealthHints:
    def test_no_hints_when_under_thresholds(self):
        assert (
            server._kb_health_hints("<architecture>short</architecture>", "short") == []
        )

    def test_memories_over_threshold_hints_compact(self):
        big_memories = "x" * (server.KB_MEMORIES_COMPACT_THRESHOLD + 1)
        hints = server._kb_health_hints("", big_memories)
        assert any("/recall:compact" in h for h in hints)

    def test_critical_warnings_uses_entry_count(self):
        cw = "**[gotcha] x**\n\n" * (server.KB_SECTION_TIDY_ENTRY_THRESHOLD + 1)
        readme = f"<critical_warnings>{cw}</critical_warnings>"
        hints = server._kb_health_hints(readme, "")
        assert any("critical_warnings" in h and "entries" in h for h in hints)

    def test_architecture_prose_uses_char_count_not_entries(self):
        # long prose, zero "**[" tag markers -- must still trigger via char count,
        # since architecture is prose by design and rarely uses tags
        prose = "word " * 2000
        assert "**[" not in prose
        readme = f"<architecture>{prose}</architecture>"
        hints = server._kb_health_hints(readme, "")
        assert any("architecture" in h and "chars" in h for h in hints)

    def test_business_rules_uses_char_count(self):
        content = "x" * (server.KB_BUSINESS_RULES_TIDY_THRESHOLD + 1)
        readme = f"<business_rules>{content}</business_rules>"
        hints = server._kb_health_hints(readme, "")
        assert any("business_rules" in h and "chars" in h for h in hints)


# ---------------------------------------------------------------------------
# _health_hint_suffix + write-time hints in save_memory/update_readme
#
# load_feature_context already runs _kb_health_hints, but a long session often
# calls save_memory/update_readme many times without ever calling
# load_feature_context again -- these tests confirm the same hint now also
# fires on the write path, not just on load.
# ---------------------------------------------------------------------------


class TestHealthHintSuffix:
    def test_empty_when_under_threshold(self, kb_env):
        d = make_feature(
            kb_env, "myproj", "my-feature", readme="<overview>x</overview>"
        )
        assert server._health_hint_suffix(d) == ""

    def test_nonempty_when_memories_over_threshold(self, kb_env):
        big_entry = f"- **2026-01-01** [id:aaaa]: {'x' * server.KB_MEMORIES_COMPACT_THRESHOLD}\n"
        d = make_feature(
            kb_env,
            "myproj",
            "my-feature",
            readme="<overview>x</overview>",
            memories={"alice": big_entry},
        )
        assert "/recall:compact" in server._health_hint_suffix(d)


class TestUnpromotedGatingHint:
    """Promotion backlog detection — gating memories (gotcha/constraint/rule/
    decision) that still live full-body in memories instead of README."""

    def _gating_entries(self, n):
        return "".join(
            f"- **2026-01-{i:02d}** [id:{i:04x}]: **[gotcha] invariant {i}**\n"
            for i in range(1, n + 1)
        )

    def test_count_unpromoted_gating(self, kb_env):
        d = make_feature(
            kb_env,
            "myproj",
            "my-feature",
            readme="<overview>x</overview>",
            memories={"alice": self._gating_entries(12)},
        )
        entries, _ = server._collect_memory_entries(server._memories_files(d))
        assert server._count_unpromoted_gating(entries) == 12

    def test_idea_and_pattern_are_not_counted(self, kb_env):
        d = make_feature(
            kb_env,
            "myproj",
            "my-feature",
            readme="<overview>x</overview>",
            memories={
                "alice": (
                    "- **2026-01-01** [id:0001]: **[idea] an idea**\n"
                    "- **2026-01-02** [id:0002]: **[pattern] a pattern**\n"
                    "- **2026-01-03** [id:0003]: **[gotcha] a gotcha**\n"
                )
            },
        )
        entries, _ = server._collect_memory_entries(server._memories_files(d))
        assert server._count_unpromoted_gating(entries) == 1

    def test_hint_fires_when_backlog_over_threshold(self, kb_env):
        d = make_feature(
            kb_env,
            "myproj",
            "my-feature",
            readme="<overview>x</overview>",
            memories={"alice": self._gating_entries(9)},
        )
        assert "gating memories" in server._health_hint_suffix(d)

    def test_no_hint_when_backlog_under_threshold(self, kb_env):
        d = make_feature(
            kb_env,
            "myproj",
            "my-feature",
            readme="<overview>x</overview>",
            memories={"alice": self._gating_entries(3)},
        )
        assert "gating memories" not in server._health_hint_suffix(d)


class TestSaveMemoryWriteTimeHint:
    def test_hint_appears_in_response_when_memories_already_large(
        self, kb_env, monkeypatch
    ):
        monkeypatch.setattr(server, "_resolve_username", lambda confirmed="": "alice")
        big_entry = f"- **2026-01-01** [id:aaaa]: {'x' * server.KB_MEMORIES_COMPACT_THRESHOLD}\n"
        make_feature(
            kb_env,
            "myproj",
            "my-feature",
            readme="<overview>x</overview>",
            memories={"alice": big_entry},
        )
        result = server.save_memory(
            slug="my-feature", content="[gotcha] small new entry", project="myproj"
        )
        assert "/recall:compact" in result

    def test_no_hint_when_under_threshold(self, kb_env, monkeypatch):
        monkeypatch.setattr(server, "_resolve_username", lambda confirmed="": "alice")
        make_feature(kb_env, "myproj", "my-feature", readme="<overview>x</overview>")
        result = server.save_memory(
            slug="my-feature", content="[gotcha] small entry", project="myproj"
        )
        assert "/recall:compact" not in result


class TestSaveBlockedAtHardLimit:
    """The hard save-side gate: a write must never push the KB past the load
    gate's limit. Mirrors `test_a_targeted_render_rescues_a_kb_over_the_hard_limit`
    on the write path — growth is refused and directed to compact, not allowed
    to reach the point where the KB stops loading."""

    def test_save_refused_when_write_would_make_kb_unloadable(
        self, kb_env, monkeypatch
    ):
        monkeypatch.setattr(server, "_resolve_username", lambda confirmed="": "alice")
        big_readme = "x" * (server.KB_FULL_RENDER_LIMIT_CHARS - 500)
        d = make_feature(
            kb_env, "myproj", "my-feature", readme=f"<overview>{big_readme}</overview>"
        )
        result = server.save_memory(
            slug="my-feature",
            content="[gotcha] " + "y" * 1000,
            project="myproj",
        )

        assert "Save blocked" in result
        assert "/recall:compact" in result
        assert "NOT written" in result
        # Refused before the write — no memories file was created.
        assert not (d / "memories-alice.md").exists()

    def test_save_still_writes_when_under_the_limit(self, kb_env, monkeypatch):
        monkeypatch.setattr(server, "_resolve_username", lambda confirmed="": "alice")
        d = make_feature(
            kb_env, "myproj", "my-feature", readme="<overview>x</overview>"
        )
        result = server.save_memory(
            slug="my-feature",
            content="[gotcha] a normal entry well under any limit",
            project="myproj",
        )
        assert "Save blocked" not in result
        assert (d / "memories-alice.md").exists()

    def test_closure_not_blocked_when_kb_over_limit(self, kb_env, monkeypatch):
        """A `[resolved:XXXX]` closure shrinks the KB (net-negative) — it hides the
        source memory — so the gate must allow it through even when the KB is
        already over the limit, or the gate deadlocks on the very closure that
        recovers it."""
        monkeypatch.setattr(server, "_resolve_username", lambda confirmed="": "alice")
        big_readme = "x" * (server.KB_FULL_RENDER_LIMIT_CHARS - 100)
        d = make_feature(
            kb_env, "myproj", "my-feature", readme=f"<overview>{big_readme}</overview>"
        )
        # Pre-existing entry the closure will hide.
        (d / "memories-alice.md").write_text(
            "- **2026-01-01** [id:aaaa]: **[gotcha] " + "z" * 500 + "**\n"
        )
        result = server.save_memory(
            slug="my-feature",
            content="[resolved:aaaa] captured elsewhere",
            project="myproj",
        )
        assert "Save blocked" not in result
        assert (d / "memories-alice.md").exists()


def test_growth_ceiling_never_exceeds_render_limit():
    """Growth must be refused at or before the unbudgeted render fails — otherwise
    a KB can grow unloadable (past KB_FULL_RENDER_LIMIT_CHARS) with no save-side
    refusal, which is how KBs drift to the render edge."""
    assert server.KB_FULL_BODY_GROWTH_LIMIT_CHARS <= server.KB_FULL_RENDER_LIMIT_CHARS


def test_post_save_total_chars_matches_render_size(kb_env):
    """The save-side render gate must measure exactly what load_feature_context
    renders — including the `<!-- from:XXXX -->` markers and the `"\\n\\n"`
    separators between memory blocks. The pre-0.2 metric stripped both, so a save
    could push the KB past KB_FULL_RENDER_LIMIT_CHARS with no refusal, leaving it
    unloadable on the very next load."""
    readme = (
        "<critical_warnings>\n"
        "**[gotcha] pooled conns**\nWhat: w\nWhy: y\nApply: a\n"
        "<!-- from:aaaa -->\n"
        "</critical_warnings>\n"
    )
    memories = {
        "alice": (
            "- **2026-01-01** [id:bbbb]: **[gotcha] one**\nbody one\n\n"
            "- **2026-01-02** [id:cccc]: **[gotcha] two**\nbody two\n"
        )
    }
    d = make_feature(kb_env, "myproj", "my-feature", readme=readme, memories=memories)

    total = server._post_save_total_chars(d, "")

    entries, _ = server._collect_memory_entries(server._memories_files(d), readme)
    assert total == server._render_size_chars(readme, entries)

    # The old metric (markers stripped, no separators) was strictly smaller — the
    # exact under-count that let an oversized KB slip through the gate.
    naive = len(server._strip_readme_for_context(readme)) + sum(
        len(b) for _, b in entries
    )
    assert total > naive


class TestFullBodyGrowthCeiling:
    """R1 fix — index-all removes the hard limit's growth pressure (the title-only
    gate metric stays small no matter how big full-body gets). This second ceiling
    caps full-body size so compaction is still forced even when the KB is loadable."""

    def test_growth_blocked_when_full_body_exceeds_ceiling(self, kb_env, monkeypatch):
        monkeypatch.setattr(server, "_resolve_username", lambda confirmed="": "alice")
        # A small README + a single huge memory body past the full-body ceiling.
        huge_body = "y" * (server.KB_FULL_BODY_GROWTH_LIMIT_CHARS + 100)
        make_feature(
            kb_env,
            "myproj",
            "my-feature",
            readme="<overview>x</overview>",
            memories={
                "alice": f"- **2026-01-01** [id:aaaa]: **[gotcha] big**\n{huge_body}\n"
            },
        )
        result = server.save_memory(
            slug="my-feature",
            content="[gotcha] " + "z" * 500,
            project="myproj",
        )
        assert "growth ceiling" in result
        assert "NOT written" in result

    def test_closure_still_allowed_over_ceiling(self, kb_env, monkeypatch):
        monkeypatch.setattr(server, "_resolve_username", lambda confirmed="": "alice")
        huge_body = "y" * (server.KB_FULL_BODY_GROWTH_LIMIT_CHARS + 100)
        make_feature(
            kb_env,
            "myproj",
            "my-feature",
            readme="<overview>x</overview>",
            memories={
                "alice": f"- **2026-01-01** [id:aaaa]: **[gotcha] big**\n{huge_body}\n"
            },
        )
        result = server.save_memory(
            slug="my-feature",
            content="[resolved:aaaa] captured elsewhere",
            project="myproj",
        )
        assert "Save blocked" not in result

    def test_growth_allowed_under_ceiling(self, kb_env, monkeypatch):
        monkeypatch.setattr(server, "_resolve_username", lambda confirmed="": "alice")
        d = make_feature(
            kb_env, "myproj", "my-feature", readme="<overview>x</overview>"
        )
        result = server.save_memory(
            slug="my-feature",
            content="[gotcha] a normal entry well under any ceiling",
            project="myproj",
        )
        assert "Save blocked" not in result
        assert (d / "memories-alice.md").exists()


class TestSaveMemoryEntrySizeGuard:
    """Per-entry size guard — the only limit keyed on a single write, not the
    whole KB. Hard rejects a pathological blob; soft only appends a note."""

    def test_entry_over_hard_limit_rejected(self, kb_env, monkeypatch):
        monkeypatch.setattr(server, "_resolve_username", lambda confirmed="": "alice")
        d = make_feature(
            kb_env, "myproj", "my-feature", readme="<overview>x</overview>"
        )
        result = server.save_memory(
            slug="my-feature",
            content="[gotcha] " + "x" * (server.KB_MEMORY_ENTRY_HARD_LIMIT_CHARS + 100),
            project="myproj",
        )
        assert "Rejected" in result
        assert "per-entry" in result
        assert not (d / "memories-alice.md").exists()

    def test_entry_over_soft_limit_gets_note(self, kb_env, monkeypatch):
        monkeypatch.setattr(server, "_resolve_username", lambda confirmed="": "alice")
        d = make_feature(
            kb_env, "myproj", "my-feature", readme="<overview>x</overview>"
        )
        result = server.save_memory(
            slug="my-feature",
            content="[gotcha] " + "y" * (server.KB_MEMORY_ENTRY_SOFT_LIMIT_CHARS + 100),
            project="myproj",
        )
        assert "soft limit" in result
        assert (d / "memories-alice.md").exists()

    def test_entry_under_soft_limit_no_note(self, kb_env, monkeypatch):
        monkeypatch.setattr(server, "_resolve_username", lambda confirmed="": "alice")
        d = make_feature(
            kb_env, "myproj", "my-feature", readme="<overview>x</overview>"
        )
        result = server.save_memory(
            slug="my-feature",
            content="[gotcha] a normal short entry",
            project="myproj",
        )
        assert "soft limit" not in result
        assert (d / "memories-alice.md").exists()


class TestS1Promotion:
    """S1 — deterministic promotion at save time. The tag->section map is a pure
    function, so promotion is one mechanical step rather than a five-step
    LLM-compliance chain. Reversibility (marker + replace-on-supersede) is what
    makes promoting with no settling window (W = 0) safe.

    Only `[supersedes:XXXX]` writes to the README. `[resolved:XXXX]` is a
    read-side receipt — it hides the source memory, and the block it names is the
    live copy, so it must leave that block alone."""

    README = "<overview>x</overview>\n<critical_warnings>\n</critical_warnings>"

    @pytest.fixture
    def fixed_id(self, monkeypatch):
        monkeypatch.setattr(server, "_resolve_username", lambda confirmed="": "alice")
        monkeypatch.setattr(server.secrets, "token_hex", lambda n: "aaa111")
        return "aaa111"

    def section(self, d, name="critical_warnings"):
        text = (d / "README.md").read_text()
        return text.split(f"<{name}>")[1].split(f"</{name}>")[0]

    def test_promotable_tag_is_promoted_with_a_provenance_marker(
        self, kb_env, fixed_id
    ):
        d = make_feature(kb_env, "myproj", "my-feature", readme=self.README)
        result = server.save_memory(
            slug="my-feature",
            content="**[gotcha] pooled conns are per-process**\nWhat: w",
            project="myproj",
        )
        body = self.section(d)
        assert "pooled conns are per-process" in body
        assert_marker(body, "aaa111")
        assert "Promoted to <critical_warnings> (id aaa111)." in result

    def test_tag_to_section_map_is_pure(self, kb_env, monkeypatch):
        monkeypatch.setattr(server, "_resolve_username", lambda confirmed="": "alice")
        monkeypatch.setattr(server.secrets, "token_hex", lambda n: "bbb222")
        d = make_feature(
            kb_env,
            "myproj",
            "my-feature",
            readme="<overview>x</overview>\n<architecture>\n</architecture>\n"
            "<business_rules>\n</business_rules>",
        )
        server.save_memory(
            slug="my-feature",
            content="**[decision] chose X**\nWhat: w",
            project="myproj",
        )
        server.save_memory(
            slug="my-feature",
            content="**[rule] X must hold**\nWhat: w",
            project="myproj",
        )
        assert "chose X" in self.section(d, "architecture")
        assert "X must hold" in self.section(d, "business_rules")
        assert "X must hold" not in self.section(d, "architecture")
        assert "chose X" not in self.section(d, "business_rules")

    def test_non_promotable_tag_leaves_readme_untouched(self, kb_env, fixed_id):
        d = make_feature(kb_env, "myproj", "my-feature", readme=self.README)
        before = (d / "README.md").read_text()
        result = server.save_memory(
            slug="my-feature",
            content="**[idea] maybe try X**\nWhat: w",
            project="myproj",
        )
        assert (d / "README.md").read_text() == before
        assert "Promoted to <critical_warnings>" not in result

    def test_promoted_title_drops_the_closure_ref(self, kb_env, fixed_id):
        """A `[supersedes:XXXX]` in the title names a memory entry the README
        cannot see — bookkeeping that leaks. Worse, it is exactly what a
        rewording rewrite would drop, which would take the marker with it."""
        d = make_feature(kb_env, "myproj", "my-feature", readme=self.README)
        server.save_memory(
            slug="my-feature",
            content="**[gotcha][supersedes:999999] the real cause**\nWhat: w",
            project="myproj",
        )
        body = self.section(d)
        assert "**[gotcha] the real cause**" in body
        assert "[supersedes:999999]" not in body

    def test_supersede_replaces_the_block_instead_of_appending_a_second(
        self, kb_env, monkeypatch
    ):
        """The load-bearing property: without it, an entry written 09:00 and
        superseded 09:04 promotes both, and README holds the wrong version *and*
        its correction with no link between them."""
        monkeypatch.setattr(server, "_resolve_username", lambda confirmed="": "alice")
        ids = iter(["aaa111", "bbb222"])
        monkeypatch.setattr(server.secrets, "token_hex", lambda n: next(ids))
        d = make_feature(kb_env, "myproj", "my-feature", readme=self.README)

        server.save_memory(
            slug="my-feature",
            content="**[gotcha] the wrong first pass**\nWhat: w",
            project="myproj",
        )
        result = server.save_memory(
            slug="my-feature",
            content="**[gotcha][supersedes:aaa111] the corrected root cause**\nWhat: w",
            project="myproj",
        )

        body = self.section(d)
        assert "the corrected root cause" in body
        assert "the wrong first pass" not in body  # replaced, not duplicated
        assert "from:bbb222" in body  # marker re-pointed at the new source
        assert "from:aaa111" not in body
        assert "replaced 1 block(s)" in result

    def test_resolved_receipt_leaves_the_promoted_block_in_place(
        self, kb_env, monkeypatch
    ):
        """`[resolved:XXXX]` means "dealt with, the knowledge lives in the README",
        so it hides the source memory on the read side and must NOT touch the
        block it is the receipt for. Deleting there destroys the only live copy:
        the memory body is hidden at the same moment (test_hides_resolved_entry_
        keeps_closure), so the knowledge would survive nowhere the tools reach."""
        monkeypatch.setattr(server, "_resolve_username", lambda confirmed="": "alice")
        ids = iter(["aaa111", "bbb222"])
        monkeypatch.setattr(server.secrets, "token_hex", lambda n: next(ids))
        d = make_feature(kb_env, "myproj", "my-feature", readme=self.README)
        server.save_memory(
            slug="my-feature",
            content="**[gotcha] memory leak in the pool**\nWhat: w",
            project="myproj",
        )
        result = server.save_memory(
            slug="my-feature",
            content="**[idea][resolved:aaa111] promoted and dealt with**\nWhat: w",
            project="myproj",
        )
        body = self.section(d)
        assert "memory leak in the pool" in body  # the live copy survives
        assert_marker(body, "aaa111")  # and keeps its provenance
        assert "README updated" not in result  # no note: nothing was removed
        assert "Saved to" in result  # the memory write is still the primary act

    def test_supersede_of_an_unpromoted_entry_appends_normally(self, kb_env, fixed_id):
        """Superseding a never-promoted entry (an [idea]) is normal — no README
        block should have existed, so it appends quietly, no stale-duplicate warn."""
        d = make_feature(
            kb_env,
            "myproj",
            "my-feature",
            readme=self.README,
            memories={
                "alice": "- **2026-01-01** [id:aaaa]: **[idea] undecided**\nWhat: w\n"
            },
        )
        result = server.save_memory(
            slug="my-feature",
            content="**[gotcha][supersedes:aaaa] fresh thought**\nWhat: w",
            project="myproj",
        )
        assert "fresh thought" in self.section(d)
        assert "check for a stale duplicate" not in result
        assert "not found in memories" not in result

    def test_supersede_of_a_marker_lost_block_warns(self, kb_env, fixed_id):
        """A supersede ref whose block lost its `from:` marker (hand edit) would
        otherwise append a silent duplicate — S1 must surface it."""
        d = make_feature(
            kb_env,
            "myproj",
            "my-feature",
            readme=(
                "<critical_warnings>\n**[gotcha] original**\ntext\n</critical_warnings>"
            ),
            memories={
                "alice": "- **2026-01-01** [id:aaaa]: **[gotcha] original**\nWhat: w\n"
            },
        )
        result = server.save_memory(
            slug="my-feature",
            content="**[gotcha][supersedes:aaaa] corrected**\nWhat: w",
            project="myproj",
        )
        assert "corrected" in self.section(d)  # the correction still lands
        assert "check for a stale duplicate" in result  # but the miss is loud

    def test_supersede_of_a_nonexistent_id_warns(self, kb_env, fixed_id):
        d = make_feature(kb_env, "myproj", "my-feature", readme=self.README)
        result = server.save_memory(
            slug="my-feature",
            content="**[gotcha][supersedes:999999] fresh thought**\nWhat: w",
            project="myproj",
        )
        assert "fresh thought" in self.section(d)
        assert "not found in memories" in result

    def test_supersede_of_a_retired_id_warns_about_the_successor(
        self, kb_env, fixed_id
    ):
        """Superseding an id that was already superseded is pointing at a dead
        entry — its block was replaced on purpose, so the warning must say
        'retired', not 'marker lost' (SE6)."""
        make_feature(
            kb_env,
            "myproj",
            "my-feature",
            readme=(
                "<critical_warnings>\n**[gotcha] original**\ntext\n"
                "<!-- from:bbbb -->\n</critical_warnings>"
            ),
            memories={
                "alice": (
                    "- **2026-01-01** [id:aaaa]: **[gotcha] original**\nWhat: w\n"
                    "- **2026-01-02** [id:bbbb]: **[gotcha][supersedes:aaaa] corrected**\nWhat: w\n"
                )
            },
        )
        result = server.save_memory(
            slug="my-feature",
            content="**[gotcha][supersedes:aaaa] re-corrected**\nWhat: w",
            project="myproj",
        )
        assert "already retired" in result
        assert "marker was lost" not in result

    def test_promotion_is_best_effort_and_never_fails_the_save(
        self, kb_env, fixed_id, monkeypatch
    ):
        d = make_feature(kb_env, "myproj", "my-feature", readme=self.README)

        def boom(*a, **k):
            raise OSError("read-only filesystem")

        monkeypatch.setattr(server, "_apply_promotion", boom)
        result = server.save_memory(
            slug="my-feature",
            content="**[gotcha] still worth keeping**\nWhat: w",
            project="myproj",
        )
        assert "saved entry" in result  # the save is the primary act
        assert (d / "memories-alice.md").exists()
        assert "Promotion to <critical_warnings> failed" in result  # but never silent
        assert "still worth keeping" in (d / "memories-alice.md").read_text()

    def test_missing_section_is_auto_created(self, kb_env, fixed_id):
        """A missing section is created on the fly rather than skipped, so the
        promotion never strands the memory as unpromoted (SE3)."""
        d = make_feature(
            kb_env, "myproj", "my-feature", readme="<overview>x</overview>"
        )
        result = server.save_memory(
            slug="my-feature",
            content="**[gotcha] no section for this**\nWhat: w",
            project="myproj",
        )
        assert "Promotion skipped:" not in result
        assert "<critical_warnings>" in (d / "README.md").read_text()
        assert "no section for this" in self.section(d)

    def test_append_is_refused_when_it_would_break_the_growth_ceiling(
        self, kb_env, monkeypatch
    ):
        """The save-side gates ran before this and measured a README without the
        promoted block, so an append needs its own check — otherwise promotion
        silently pushes full-body past the ceiling it was supposed to respect."""
        d = make_feature(kb_env, "myproj", "my-feature", readme=self.README)
        monkeypatch.setattr(server, "KB_FULL_BODY_GROWTH_LIMIT_CHARS", 200)
        note = server._apply_promotion(
            d,
            "critical_warnings",
            "abc123",
            "**[gotcha] " + "y" * 400 + "**",
            "",
            set(),
        )
        assert "Promotion skipped" in note
        assert "/recall:tidy" in note
        assert "from:abc123" not in (d / "README.md").read_text()

    def test_replacement_is_not_size_guarded(self, kb_env, monkeypatch):
        """Only appends grow. A replace shrinks or holds, so the ceiling
        must never block a correction — that would strand the wrong version."""
        d = make_feature(
            kb_env,
            "myproj",
            "my-feature",
            readme="<critical_warnings>\n**[gotcha] old**\ntext\n<!-- from:aaa111 -->\n"
            "</critical_warnings>",
        )
        monkeypatch.setattr(server, "KB_FULL_BODY_GROWTH_LIMIT_CHARS", 1)
        note = server._apply_promotion(
            d, "critical_warnings", "bbb222", "**[gotcha] corrected**", "", {"aaa111"}
        )
        assert "Promotion skipped" not in note
        body = self.section(d)
        assert "corrected" in body and "old" not in body


class TestS4Dedup:
    """S4 — near-duplicate promotion gate. Jaccard over *distinctive* tokens
    (stopwords and >50%-frequency tokens dropped) skips promotion when the
    incoming entry would add a near-copy of an existing block. Promotion-only:
    it never blocks the save (the memory still lands), and it never blocks a
    `[supersedes]` correction, which replaces a block rather than adding a copy."""

    README = "<overview>x</overview>\n<critical_warnings>\n</critical_warnings>"

    @pytest.fixture
    def fixed_id(self, monkeypatch):
        monkeypatch.setattr(server, "_resolve_username", lambda confirmed="": "alice")
        monkeypatch.setattr(server.secrets, "token_hex", lambda n: "aaa111")
        return "aaa111"

    def section(self, d, name="critical_warnings"):
        text = (d / "README.md").read_text()
        return text.split(f"<{name}>")[1].split(f"</{name}>")[0]

    def test_near_duplicate_skips_promotion_but_keeps_the_save(
        self, kb_env, monkeypatch
    ):
        # A README block with no memory behind it: the incoming save is near-dup
        # of the BLOCK, not of any live memory — so the save-time dedup gate (B)
        # passes and only the promotion gate (S4) skips, keeping the save.
        readme = (
            "<overview>x</overview>\n<critical_warnings>\n"
            "**[gotcha] pooled conns are per-process**\n"
            "What: each worker opens its own pool\n"
            "</critical_warnings>"
        )
        monkeypatch.setattr(server.secrets, "token_hex", lambda n: "aaa111")
        monkeypatch.setattr(server, "_resolve_username", lambda confirmed="": "alice")
        d = make_feature(kb_env, "myproj", "my-feature", readme=readme)

        result = server.save_memory(
            slug="my-feature",
            content="**[gotcha] pooled conns are per-process**\n"
            "What: every worker opens its own pool so the maxconn limit is not "
            "shared across processes",
            project="myproj",
        )
        body = self.section(d)
        assert "Promotion skipped" in result
        assert "near-duplicate" in result
        assert body.count("**[gotcha] pooled conns are per-process**") == 1
        # The save is the primary act — the memory still landed.
        assert (d / "memories-alice.md").read_text().count("[id:") == 1

    def test_near_duplicate_save_is_rejected(self, kb_env, monkeypatch):
        # B gate: the second save is near-dup of a LIVE memory entry, so it is
        # hard-rejected before promotion even runs.
        ids = iter(["aaa111", "bbb222"])
        monkeypatch.setattr(server.secrets, "token_hex", lambda n: next(ids))
        monkeypatch.setattr(server, "_resolve_username", lambda confirmed="": "alice")
        d = make_feature(kb_env, "myproj", "my-feature", readme=self.README)

        server.save_memory(
            slug="my-feature",
            content="**[gotcha] pooled conns are per-process**\n"
            "What: each worker opens its own pool so the maxconn limit is not "
            "shared across processes",
            project="myproj",
        )
        result = server.save_memory(
            slug="my-feature",
            content="**[gotcha] pooled conns are per-process**\n"
            "What: every worker opens its own pool so the maxconn limit is not "
            "shared across processes",
            project="myproj",
        )
        assert "Rejected: near-duplicate" in result
        assert "[id:aaa111]" in result
        # The second save did not land.
        assert (d / "memories-alice.md").read_text().count("[id:") == 1

    def test_distinct_entries_both_promote(self, kb_env, monkeypatch):
        ids = iter(["aaa111", "bbb222"])
        monkeypatch.setattr(server.secrets, "token_hex", lambda n: next(ids))
        monkeypatch.setattr(server, "_resolve_username", lambda confirmed="": "alice")
        d = make_feature(kb_env, "myproj", "my-feature", readme=self.README)

        server.save_memory(
            slug="my-feature",
            content="**[gotcha] pooled conns are per-process**\nWhat: each worker "
            "opens its own pool",
            project="myproj",
        )
        result = server.save_memory(
            slug="my-feature",
            content="**[gotcha] slow queries are dropped silently**\nWhat: the "
            "timeout default kills long queries without a log",
            project="myproj",
        )
        body = self.section(d)
        assert "near-duplicate" not in result
        assert "Promoted to <critical_warnings>" in result
        assert "pooled conns are per-process" in body
        assert "slow queries are dropped silently" in body

    def test_supersede_correction_is_never_dedup_blocked(self, kb_env, monkeypatch):
        ids = iter(["aaa111", "bbb222"])
        monkeypatch.setattr(server.secrets, "token_hex", lambda n: next(ids))
        monkeypatch.setattr(server, "_resolve_username", lambda confirmed="": "alice")
        d = make_feature(kb_env, "myproj", "my-feature", readme=self.README)

        server.save_memory(
            slug="my-feature",
            content="**[gotcha] pooled conns are per-process**\nWhat: each worker "
            "opens its own pool so limits are not shared",
            project="myproj",
        )
        # A near-identical correction: without the supersede gate this would be
        # dedup-skipped; with it, the block must be replaced instead.
        result = server.save_memory(
            slug="my-feature",
            content="**[gotcha][supersedes:aaa111] pooled conns are per-process**\n"
            "What: every worker opens its own pool so limits are not shared",
            project="myproj",
        )
        assert "near-duplicate" not in result
        assert "replaced 1 block(s)" in result
        assert self.section(d).count("**[gotcha] pooled conns are per-process**") == 1

    def test_distinctive_tokens_keep_a_one_block_section_at_plain_jaccard(self):
        blocks = ["the cache limit is not shared across processes"]
        assert server._distinctive_tokens(blocks) == server._tokenize(blocks[0])

    def test_distinctive_tokens_drop_tokens_in_more_than_half(self):
        blocks = [
            "the cache limit is per process",
            "the cache limit is global",
            "the cache limit is shared",
        ]
        distinctive = server._distinctive_tokens(blocks)
        assert "cache" not in distinctive  # in all 3 → background
        assert "process" in distinctive  # in 1 of 3 → distinctive


class TestPromotedMarkerCarryForward:
    """update_readme replaces a section wholesale, which would drop every marker
    on the first rewrite — after that a later [supersedes:Y] appends a duplicate
    instead of replacing. Markers are matched back by the block's opening line."""

    def section(self, d):
        text = (d / "README.md").read_text()
        return text.split("<critical_warnings>")[1].split("</critical_warnings>")[0]

    def test_marker_survives_a_section_rewrite(self, kb_env):
        d = make_feature(
            kb_env,
            "myproj",
            "my-feature",
            readme="<overview>x</overview>\n<critical_warnings>\n"
            "**[gotcha] kept title**\nbody\n<!-- from:aaa111 -->\n</critical_warnings>",
        )
        server.update_readme(
            slug="my-feature",
            section="critical_warnings",
            content="**[gotcha] kept title**\nbody, lightly edited",
            project="myproj",
            confirm=True,
        )
        assert_marker(self.section(d), "aaa111")

    def test_marker_dropped_when_the_block_is_reworded(self, kb_env):
        """A reworded block is a hand edit: the user owns it now, so no marker —
        which also means a later supersede appends rather than clobbering it."""
        d = make_feature(
            kb_env,
            "myproj",
            "my-feature",
            readme="<overview>x</overview>\n<critical_warnings>\n"
            "**[gotcha] kept title**\nbody\n<!-- from:aaa111 -->\n</critical_warnings>",
        )
        server.update_readme(
            slug="my-feature",
            section="critical_warnings",
            content="**[gotcha] a title I rewrote myself**\nbody",
            project="myproj",
            confirm=True,
        )
        assert "from:aaa111" not in self.section(d)

    def test_carry_forward_is_idempotent(self, kb_env):
        old = "**[gotcha] t**\nbody\n<!-- from:aaa111 -->"
        once = server._carry_promoted_markers(old, "**[gotcha] t**\nbody")
        twice = server._carry_promoted_markers(old, once)
        assert once == twice
        assert twice.count("from:aaa111") == 1

    def test_ambiguous_duplicate_titles_are_not_guessed(self):
        old = "**[gotcha] same**\na\n<!-- from:aaa111 -->\n\n**[gotcha] same**\nb\n<!-- from:bbb222 -->"
        out = server._carry_promoted_markers(
            old, "**[gotcha] same**\na\n\n**[gotcha] same**\nb"
        )
        assert "from:" not in out


class TestProvenanceRecognition:
    """update_readme is the only writer of six of the nine sections, and
    `/recall:tidy` rewrites the three that promotion also targets — so a block
    that duplicates a memory regularly arrives through it from the hand, with no
    `from:` marker. Such a block used to be an orphan: a later `[supersedes:X]`
    appended a second, contradictory block instead of replacing it.

    Recognition re-derives the marker from the block's own content, which is what
    lets the rule "never hand-promote via update_readme" be dropped: a verbatim
    copy is now tracked automatically. It is verbatim on purpose — a marker means
    a future supersede replaces the block wholesale, which is only safe while the
    text is byte-identical to the memory's body."""

    README = "<overview>x</overview>\n<critical_warnings>\n</critical_warnings>"
    BODY = "**[gotcha] pooled conns are per-process**\nWhat: w"
    MEMORY = "- **2026-01-01** [id:aaa111]: " + BODY

    @pytest.fixture
    def fixed_id(self, monkeypatch):
        monkeypatch.setattr(server, "_resolve_username", lambda confirmed="": "alice")
        monkeypatch.setattr(server.secrets, "token_hex", lambda n: "aaa111")
        return "aaa111"

    def section(self, d, name="critical_warnings"):
        text = (d / "README.md").read_text()
        return text.split(f"<{name}>")[1].split(f"</{name}>")[0]

    def test_a_markerless_orphan_is_adopted(self, kb_env, fixed_id):
        """Carry-forward cannot help here — the old text holds no marker to carry
        — so recognition is the only mechanism that can make this supersedeable."""
        d = make_feature(
            kb_env,
            "myproj",
            "my-feature",
            readme="<overview>x</overview>\n<critical_warnings>\n"
            + self.BODY
            + "\n</critical_warnings>",
            memories={"alice": self.MEMORY},
        )
        result = server.update_readme(
            slug="my-feature",
            section="critical_warnings",
            content=self.BODY,
            project="myproj",
            confirm=True,
        )
        assert_marker(self.section(d), "aaa111")
        assert "recognised as promoted" in result

    def test_supersede_replaces_the_adopted_block(self, kb_env, fixed_id, monkeypatch):
        """The acceptance case: without the adopted marker this save appends a
        second, contradictory constraint to an always-injected section."""
        d = make_feature(
            kb_env,
            "myproj",
            "my-feature",
            readme="<overview>x</overview>\n<critical_warnings>\n"
            + self.BODY
            + "\n</critical_warnings>",
            memories={"alice": self.MEMORY},
        )
        server.update_readme(
            slug="my-feature",
            section="critical_warnings",
            content=self.BODY,
            project="myproj",
            confirm=True,
        )
        monkeypatch.setattr(server.secrets, "token_hex", lambda n: "bbb222")
        server.save_memory(
            slug="my-feature",
            content=(
                "**[gotcha][supersedes:aaa111] pooled conns are global**\nWhat: new"
            ),
            project="myproj",
        )
        body = self.section(d)
        assert "pooled conns are global" in body
        assert "What: w" not in body
        assert body.count("**[gotcha]") == 1

    def test_a_paraphrase_gets_no_marker(self, kb_env, fixed_id):
        """A reworded copy is a hand edit: the memory stays live so its Why/Apply
        detail keeps rendering, and no marker claims a block that a supersede
        would then overwrite."""
        d = make_feature(
            kb_env,
            "myproj",
            "my-feature",
            readme=self.README,
            memories={"alice": self.MEMORY},
        )
        server.update_readme(
            slug="my-feature",
            section="critical_warnings",
            content="**[gotcha] pooled conns, reworded**\nWhat: w",
            project="myproj",
            confirm=True,
        )
        assert "from:" not in self.section(d)

    def test_a_non_promotable_section_is_never_recognised(self, kb_env, fixed_id):
        d = make_feature(
            kb_env,
            "myproj",
            "my-feature",
            readme="<overview>\n</overview>\n<critical_warnings>\n</critical_warnings>",
            memories={"alice": self.MEMORY},
        )
        server.update_readme(
            slug="my-feature",
            section="overview",
            content=self.BODY,
            project="myproj",
            confirm=True,
        )
        overview = (
            (d / "README.md").read_text().split("<overview>")[1].split("</overview>")[0]
        )
        assert "from:" not in overview

    def test_a_duplicate_block_is_surfaced_not_double_marked(self):
        """Two blocks sharing one body is the duplicate-constraint shape. Only the
        first may claim the id — keying the map by opening line would mark both,
        and a later supersede would then leave the copy behind silently."""
        entries = [("2026-01-01", self.MEMORY)]
        content = self.BODY + "\n\n" + self.BODY
        recognized, ambiguous, duplicated = server._recognized_sources(entries, content)
        assert len(recognized) == 1
        assert ambiguous == []
        assert duplicated == ["**[gotcha] pooled conns are per-process**"]

    def test_two_memories_sharing_a_body_are_not_guessed(self):
        entries = [
            ("2026-01-01", self.MEMORY),
            ("2026-01-02", "- **2026-01-02** [id:bbb222]: " + self.BODY),
        ]
        recognized, ambiguous, duplicated = server._recognized_sources(
            entries, self.BODY
        )
        assert recognized == {}
        assert duplicated == []
        assert len(ambiguous) == 1

    def test_capture_and_recognition_share_one_normalization(self):
        """Read side (`_captured_ids`) and write side (`_recognized_sources`)
        answer the same question about the same text, so they must agree — a
        drift between them would attach or drop markers with nothing showing."""
        entries = [("2026-01-01", self.MEMORY)]
        readme = (
            "<critical_warnings>\n"
            + self.BODY
            + "\n<!-- from:aaa111 -->\n</critical_warnings>"
        )
        assert server._captured_ids(readme, entries) == {"aaa111"}
        body = readme.split("<critical_warnings>")[1].split("</critical_warnings>")[0]
        recognized, _, _ = server._recognized_sources(entries, body.strip())
        assert list(recognized.values()) == ["aaa111"]


class TestPromotedMarkerDate:
    """The `from:` marker carries the source entry's date.

    The date is the one field `_entry_content` strips on the way into the README
    and nothing else re-adds, so before this a promoted block lost when the
    knowledge was learned. It is optional in the marker shape, because every
    README written before 2026-10-03 has dateless markers and those files are not
    rewritten by this change.

    Three separate regexes parse a marker, and all three must agree on the dated
    shape. `_PROMOTED_MARKER_RE` is the only one that reads it; the other two are
    lookalikes that fail *silently* when they drift:
      - `_NON_MARKER_COMMENT_RE` — a dated marker it does not exclude is stripped
        as an ordinary HTML comment, orphaning the block.
      - the `_normalized_block_body` path — a side that strips the date while the
        other does not makes a captured memory look edited, so it silently
        returns to the render and its block stops being supersedeable.
    """

    README = "<overview>x</overview>\n<critical_warnings>\n</critical_warnings>"
    BODY = "**[gotcha] pooled conns are per-process**\nWhat: w"
    DATED = "<!-- from:aaa111 2026-01-01 -->"
    MEMORY = "- **2026-01-01** [id:aaa111]: " + BODY

    @pytest.fixture
    def fixed_id(self, monkeypatch):
        monkeypatch.setattr(server, "_resolve_username", lambda confirmed="": "alice")
        monkeypatch.setattr(server.secrets, "token_hex", lambda n: "aaa111")
        return "aaa111"

    def section(self, d, name="critical_warnings"):
        text = (d / "README.md").read_text()
        return text.split(f"<{name}>")[1].split(f"</{name}>")[0]

    def _readme_with(self, marker):
        return "<critical_warnings>\n" + self.BODY + f"\n{marker}\n</critical_warnings>"

    def test_marker_shape_parses_with_and_without_a_date(self):
        """One pattern for both, and the id group is unaffected by the date
        group being present — every id caller reads `group(1)`."""
        for marker, expected in (
            ("<!-- from:aaa111 -->", None),
            ("<!-- from:aaa111 2026-01-01 -->", "2026-01-01"),
            ("<!--   from:aaa111   2026-01-01   -->", "2026-01-01"),
        ):
            m = server._PROMOTED_MARKER_RE.search(marker)
            assert m is not None, marker
            assert m.group(1) == "aaa111"
            assert m.group(2) == expected
            assert server._marked_source_ids(marker) == {"aaa111"}
            assert server._marker_id(marker) == "aaa111"

    def test_a_malformed_date_never_costs_the_id(self):
        """The id is what makes a block supersedeable, so a hand-mangled date
        must cost the date and never the id. Pinning a date shape here would
        make the marker stop parsing entirely — provenance lost silently."""
        for marker, expected in (
            ("<!-- from:aaa111 01-01-2026 -->", "01-01-2026"),
            ("<!-- from:aaa111 2026-1-1 -->", "2026-1-1"),
            ("<!-- from:aaa111 unknown -->", "unknown"),
        ):
            m = server._PROMOTED_MARKER_RE.search(marker)
            assert m is not None, marker
            assert m.group(1) == "aaa111"
            assert m.group(2) == expected
            assert server._marker_id(marker) == "aaa111"
        # A hands-mangled date is also still preserved (not stripped) in context,
        # so it degrades to a dateless-but-working marker rather than a dead one.
        kept = server._strip_readme_for_context(
            self._readme_with("<!-- from:aaa111 01-01-2026 -->"), keep_markers=True
        )
        assert server._marked_source_ids(kept) == {"aaa111"}

    def test_a_dated_marker_is_kept_in_the_model_facing_render(self):
        """The regression that would be invisible: a dated marker stripped as a
        plain HTML comment costs no budget and so shows up nowhere until a later
        supersede appends a duplicate instead of replacing the block."""
        kept = server._strip_readme_for_context(
            self._readme_with(self.DATED), keep_markers=True
        )
        assert self.DATED in kept
        assert server._marked_source_ids(kept) == {"aaa111"}
        # ...while an ordinary comment is still stripped in the same pass.
        kept = server._strip_readme_for_context(
            self._readme_with(self.DATED + "\n<!-- template note -->"),
            keep_markers=True,
        )
        assert self.DATED in kept
        assert "template note" not in kept

    def test_a_dated_marker_is_stripped_unconditionally_without_keep_markers(self):
        stripped = server._strip_readme_for_context(self._readme_with(self.DATED))
        assert "from:aaa111" not in stripped

    def test_both_normalization_sides_agree_on_a_dated_marker(self):
        """The read side and the write side must strip the date identically.

        Same text, same question: "is this block verbatim the memory's body?"
        A one-character disagreement attaches or drops a `from:` marker with
        nothing showing, so both are asserted against the same dated block.
        """
        entries = [("2026-01-01", self.MEMORY)]
        readme = self._readme_with(self.DATED)
        body = readme.split("<critical_warnings>")[1].split("</critical_warnings>")[0]

        # Read side: the dated block still counts as captured.
        assert server._captured_ids(readme, entries) == {"aaa111"}
        # Write side: the same text is still recognized as that memory's body.
        recognized, ambiguous, duplicated = server._recognized_sources(
            entries, body.strip()
        )
        assert list(recognized.values()) == ["aaa111"]
        assert ambiguous == [] and duplicated == []

        # And the two normalizations are literally the same string.
        assert server._normalized_block_body(
            self._readme_with(self.DATED)
        ) == server._normalized_block_body(self._readme_with(""))

    def test_a_dateless_marker_is_still_captured_and_recognized(self):
        """Backward compatibility: 2026-10-03 predates no README, so every
        existing KB has dateless markers and must keep working untouched."""
        entries = [("2026-01-01", self.MEMORY)]
        readme = self._readme_with("<!-- from:aaa111 -->")
        assert server._captured_ids(readme, entries) == {"aaa111"}
        body = readme.split("<critical_warnings>")[1].split("</critical_warnings>")[0]
        recognized, _, _ = server._recognized_sources(entries, body.strip())
        assert list(recognized.values()) == ["aaa111"]

    def test_promotion_writes_the_entry_date_into_the_marker(
        self, kb_env, fixed_id, monkeypatch
    ):
        """The marker's date is the entry's OWN date, not the day it got
        promoted — the point of carrying it is to keep when the knowledge was
        learned, which the entry header is the record of."""

        class _FrozenDate(date):
            @classmethod
            def today(cls):
                return cls(2026, 1, 1)

        monkeypatch.setattr(server, "date", _FrozenDate)
        d = make_feature(kb_env, "myproj", "my-feature", readme=self.README)
        server.save_memory(slug="my-feature", content=self.BODY, project="myproj")

        assert self.section(d).strip().endswith("<!-- from:aaa111 2026-01-01 -->")
        assert "- **2026-01-01** [id:aaa111]" in (d / "memories-alice.md").read_text()

    def test_capture_uses_the_entrys_own_date_not_the_readme_marker_date(
        self, kb_env, fixed_id
    ):
        """The date is provenance, never part of the identity comparison: a
        marker whose date drifts must not break capture and resurrect the
        memory into every subsequent render."""
        entries = [("2026-01-01", self.MEMORY)]
        stale = self._readme_with("<!-- from:aaa111 1999-12-31 -->")
        assert server._captured_ids(stale, entries) == {"aaa111"}

    def test_carry_forward_preserves_the_date_across_a_section_rewrite(
        self, kb_env, fixed_id
    ):
        """`update_readme` rewrites the section wholesale, so a marker rebuilt
        from the id alone would quietly drop the date the first time a user
        edited the section around it."""
        make_feature(
            kb_env,
            "myproj",
            "my-feature",
            readme=self._readme_with(self.DATED),
        )
        server.update_readme(
            slug="my-feature",
            section="critical_warnings",
            content=self.BODY + "\n\n" + "**[gotcha] unrelated addition**\nWhat: w",
            project="myproj",
            confirm=True,
        )
        assert self.DATED in self.section(kb_env / "myproj" / "my-feature")


class TestPreviewShowsTheWrittenContent:
    """The preview's contract is "a REAL diff, shown verbatim" — so it must be
    computed on the text that will be written, markers included.

    It used to diff comment-stripped current text against the model's raw
    content, while only the write path ran marker policy. A marker could
    therefore appear or vanish between the approval and the write with nothing
    showing it — the two most consequential events in a section rewrite."""

    README = (
        "<overview>x</overview>\n<critical_warnings>\n"
        "**[gotcha] t**\nbody\n<!-- from:aaa111 -->\n</critical_warnings>"
    )

    def section(self, d):
        text = (d / "README.md").read_text()
        return text.split("<critical_warnings>")[1].split("</critical_warnings>")[0]

    def test_a_dropped_marker_is_visible_in_the_preview(self, kb_env):
        make_feature(kb_env, "myproj", "my-feature", readme=self.README)
        preview = server.update_readme(
            slug="my-feature",
            section="critical_warnings",
            content="**[gotcha] a title I rewrote**\nbody",
            project="myproj",
        )
        assert "-<!-- from:aaa111 -->" in preview
        assert "marker(s) dropped (aaa111)" in preview
        assert "returns to the next load" in preview

    def test_recognition_is_visible_in_the_preview(self, kb_env, monkeypatch):
        """The reviewer must see the marker being attached, since it is what
        future corrections depend on."""
        make_feature(
            kb_env,
            "myproj",
            "my-feature",
            readme="<overview>x</overview>\n<critical_warnings>\n"
            "**[gotcha] t**\nWhat: w\n</critical_warnings>",
            memories={"alice": "- **2026-01-01** [id:aaa111]: **[gotcha] t**\nWhat: w"},
        )
        preview = server.update_readme(
            slug="my-feature",
            section="critical_warnings",
            content="**[gotcha] t**\nWhat: w",
            project="myproj",
        )
        assert "+<!-- from:aaa111 2026-01-01 -->" in preview
        assert "recognised as promoted" in preview

    def test_preview_and_write_agree_on_provenance(self, kb_env):
        """Both calls run the same finalize, so the provenance conclusion the
        user approved is the one that lands."""
        make_feature(kb_env, "myproj", "my-feature", readme=self.README)
        kwargs = {
            "slug": "my-feature",
            "section": "critical_warnings",
            "content": "**[gotcha] a title I rewrote**\nbody",
            "project": "myproj",
        }
        preview = server.update_readme(**kwargs)
        written = server.update_readme(**kwargs, confirm=True)
        pnote = [line for line in preview.splitlines() if "marker(s) dropped" in line]
        wnote = [line for line in written.splitlines() if "marker(s) dropped" in line]
        assert pnote and pnote == wnote

    def test_omitting_a_marker_still_reports_no_changes(self, kb_env):
        """A model rewriting an unchanged block from its loaded copy may not echo
        the marker. Nothing actually changes, so the preview must not invent a
        diff — and no marker may be lost either."""
        d = make_feature(kb_env, "myproj", "my-feature", readme=self.README)
        result = server.update_readme(
            slug="my-feature",
            section="critical_warnings",
            content="**[gotcha] t**\nbody",
            project="myproj",
        )
        assert "No changes" in result
        assert_marker((d / "README.md").read_text(), "aaa111")


class TestReadmeWriteSizeGate:
    """README is injected whole on every load, and `save_memory`'s gates only ever
    price *entries* — so before this, `update_readme` was the one writer that could
    grow the KB past a ceiling with nothing objecting. Growth-only: a section the
    user is shrinking must never be refused, or the remedy would be blocked by the
    same gate that created the need for it."""

    @pytest.fixture
    def tight_body_ceiling(self, monkeypatch):
        monkeypatch.setattr(server, "KB_FULL_BODY_GROWTH_LIMIT_CHARS", 200)
        return 200

    def readme_with(self, body):
        return (
            f"<overview>x</overview>\n<critical_warnings>\n{body}\n</critical_warnings>"
        )

    def test_readme_growth_past_the_ceiling_is_blocked(
        self, kb_env, tight_body_ceiling
    ):
        d = make_feature(
            kb_env, "myproj", "my-feature", readme=self.readme_with("seed")
        )
        before = (d / "README.md").read_text()
        result = server.update_readme(
            slug="my-feature",
            section="critical_warnings",
            content="y" * 500,
            project="myproj",
            confirm=True,
        )
        assert "Update blocked" in result
        assert "Nothing was written" in result
        assert (d / "README.md").read_text() == before

    def test_a_shrinking_write_is_never_blocked(self, kb_env, tight_body_ceiling):
        """The KB is already over the ceiling; only a shrink can fix that, so the
        gate must let it through rather than demanding a shrink it refuses. The
        shrink here is deliberately small — large enough that the post-write total
        alone would fit would hide the bug, since the KB is what's oversized, not
        this write."""
        d = make_feature(
            kb_env, "myproj", "my-feature", readme=self.readme_with("y" * 500)
        )
        result = server.update_readme(
            slug="my-feature",
            section="critical_warnings",
            content="y" * 450,
            project="myproj",
            confirm=True,
        )
        assert "Update blocked" not in result
        # Compare length, not substring: "y"*450 is inside "y"*500, so `in` would
        # pass even if the write never happened.
        body = (d / "README.md").read_text().split("<critical_warnings>")[1]
        assert len(body.split("</critical_warnings>")[0].strip()) == 450

    def test_the_preview_warns_before_the_write_refuses(
        self, kb_env, tight_body_ceiling
    ):
        """Otherwise the user approves a diff that the confirmed call then refuses
        — the failure has to be visible while it is still just a proposal."""
        d = make_feature(
            kb_env, "myproj", "my-feature", readme=self.readme_with("seed")
        )
        preview = server.update_readme(
            slug="my-feature",
            section="critical_warnings",
            content="y" * 500,
            project="myproj",
        )
        assert "Diff preview" in preview
        assert "Update blocked" in preview
        assert "nothing written yet" in preview
        assert (d / "README.md").read_text() == self.readme_with("seed")


class TestCapturedAutoHide:
    """README is the source of truth: a memory whose promoted block is still a
    verbatim copy is hidden on load (auto-[resolved]), and its id stays visible
    via the `from:` marker so the block can still be superseded."""

    @pytest.fixture
    def alice_id(self, monkeypatch):
        monkeypatch.setattr(server, "_resolve_username", lambda confirmed="": "alice")
        monkeypatch.setattr(server.secrets, "token_hex", lambda n: "aaa111")
        return "aaa111"

    def test_captured_ids_verbatim(self):
        entries = [
            ("2026-01-01", "- **2026-01-01** [id:aaaa]: **[gotcha] t**\nWhat: w")
        ]
        readme = (
            "<critical_warnings>\n**[gotcha] t**\nWhat: w\n<!-- from:aaaa -->\n"
            "</critical_warnings>"
        )
        assert server._captured_ids(readme, entries) == {"aaaa"}

    def test_captured_ids_trimmed_body_not_captured(self):
        entries = [
            ("2026-01-01", "- **2026-01-01** [id:aaaa]: **[gotcha] t**\nWhat: w")
        ]
        readme = (
            "<critical_warnings>\n**[gotcha] t**\nWhat: trimmed\n<!-- from:aaaa -->\n"
            "</critical_warnings>"
        )
        assert server._captured_ids(readme, entries) == set()

    def test_captured_ids_reworded_no_marker_not_captured(self):
        entries = [
            ("2026-01-01", "- **2026-01-01** [id:aaaa]: **[gotcha] t**\nWhat: w")
        ]
        readme = "<critical_warnings>\n**[gotcha] t**\nWhat: w\n</critical_warnings>"
        assert server._captured_ids(readme, entries) == set()

    def test_promoted_memory_is_auto_hidden_on_load(self, kb_env, alice_id):
        make_feature(
            kb_env,
            "myproj",
            "my-feature",
            readme="<overview>x</overview>\n<critical_warnings>\n</critical_warnings>",
        )
        server.save_memory(
            slug="my-feature",
            content="**[gotcha] Config must not cache**\nWhat: w",
            project="myproj",
        )
        out = server.load_feature_context(slug="my-feature", project="myproj")
        assert_marker(out, "aaa111")  # id is visible for supersede
        assert out.count("Config must not cache") == 1  # block only, memory hidden

    def test_trimmed_block_keeps_memory_live(self, kb_env, alice_id):
        make_feature(
            kb_env,
            "myproj",
            "my-feature",
            readme="<overview>x</overview>\n<critical_warnings>\n</critical_warnings>",
        )
        server.save_memory(
            slug="my-feature",
            content="**[gotcha] Config must not cache**\nWhat: full detail",
            project="myproj",
        )
        server.update_readme(
            slug="my-feature",
            section="critical_warnings",
            content="**[gotcha] Config must not cache**\nWhat: trimmed",
            project="myproj",
            confirm=True,
        )
        out = server.load_feature_context(slug="my-feature", project="myproj")
        assert "full detail" in out  # the trimmed block no longer captures it


class TestEntryIdLength:
    """4-hex IDs collided in real usage (KB `recall-mcp`, id:b068) — widened to 6-hex.
    Both generators must produce IDs the merge-time regexes ({4,6}) still recognize."""

    def test_save_memory_generates_6char_id(self, kb_env, monkeypatch):
        monkeypatch.setattr(server, "_resolve_username", lambda confirmed="": "alice")
        d = make_feature(
            kb_env, "myproj", "my-feature", readme="<overview>x</overview>"
        )
        server.save_memory(
            slug="my-feature",
            content="[gotcha] a sufficiently long test entry",
            project="myproj",
        )
        text = (d / "memories-alice.md").read_text()
        m = server._entry_id(text.strip().split("\n\n")[-1])
        assert len(m) == 6

    def test_report_miss_generates_6char_id(self, kb_env, monkeypatch):
        monkeypatch.setattr(server, "_resolve_username", lambda confirmed="": "alice")
        d = make_feature(
            kb_env, "myproj", "my-feature", readme="<overview>x</overview>"
        )
        server.report_miss(
            slug="my-feature", description="missed this", project="myproj"
        )
        text = (d / "memories-alice.md").read_text()
        m = server._entry_id(text.strip().split("\n\n")[-1])
        assert len(m) == 6


# ---------------------------------------------------------------------------
# _resolve_slug / _not_found_msg
# ---------------------------------------------------------------------------


class TestResolveSlug:
    def test_exact_match(self, kb_env):
        make_feature(kb_env, "myproj", "payment-gateway")
        d, slug, was_fuzzy = server._resolve_slug("payment-gateway", [Path("myproj")])
        assert d == kb_env / "myproj" / "payment-gateway"
        assert slug == "payment-gateway"
        assert was_fuzzy is False

    def test_no_match_returns_none_dir(self, kb_env):
        d, _slug, _ = server._resolve_slug("nonexistent", [Path("myproj")])
        assert d is None

    def test_fuzzy_match_close_typo(self, kb_env):
        make_feature(kb_env, "myproj", "payment-gateway")
        _d, slug, was_fuzzy = server._resolve_slug("payment-gatewy", [Path("myproj")])
        assert slug == "payment-gateway"
        assert was_fuzzy is True

    def test_ambiguous_across_projects_returns_error_string(self, kb_env):
        make_feature(kb_env, "proj-a", "shared-slug")
        make_feature(kb_env, "proj-b", "shared-slug")
        result = server._resolve_slug("shared-slug", [Path("proj-a"), Path("proj-b")])
        assert isinstance(result, str)
        assert "multiple projects" in result


class TestNotFoundMsg:
    def test_no_suggestion_when_nothing_close(self, kb_env):
        make_feature(kb_env, "myproj", "totally-unrelated")
        msg = server._not_found_msg("xyz-nothing-like-it", [Path("myproj")])
        assert "Did you mean" not in msg

    def test_suggests_close_match(self, kb_env):
        make_feature(kb_env, "myproj", "payment-gateway")
        msg = server._not_found_msg("paiment-gateway", [Path("myproj")])
        assert "payment-gateway" in msg

    def test_only_searches_within_given_projects(self, kb_env):
        # Known gap: a slug that exists VERBATIM in a DIFFERENT
        # project than the one passed in still gets no "did you mean" pointer
        # to it. This test documents the current (buggy) behavior so a future
        # fix updates it deliberately.
        make_feature(kb_env, "other-proj", "payment-gateway")
        msg = server._not_found_msg("payment-gateway", [Path("myproj")])
        assert "Did you mean" not in msg


# ---------------------------------------------------------------------------
# init_feature
# ---------------------------------------------------------------------------


class TestInitFeature:
    def test_survives_stray_placeholders_in_template(
        self, kb_env, monkeypatch, tmp_path
    ):
        # Templates are hand-edited docs that may legitimately contain their own
        # illustrative `{...}` examples (e.g. `{project-name}/{slug}/README.md`
        # in a key_files example -- already used verbatim in this project's own
        # KB) and even stray `$something` text. Real placeholders use $name/
        # $slug/$summary/$first_row (string.Template.safe_substitute) precisely
        # so neither kind of stray text is mistaken for a real substitution slot.
        templates_dir = tmp_path / "templates"
        templates_dir.mkdir()
        (templates_dir / "feature-README.md").write_text(
            "# $name\n<key_files>\n"
            "<!-- e.g. `{project-name}/{slug}/README.md`, or $unknown_var -->\n"
            "</key_files>\n"
        )
        (templates_dir / "feature-memories.md").write_text("# $name\n")
        (templates_dir / "features-index.md").write_text("$first_row\n")
        (templates_dir / "claude-md-snippet.md").write_text(
            "recall-mcp setup snippet\n"
        )
        monkeypatch.setattr(server, "TEMPLATES_DIR", templates_dir)
        monkeypatch.setattr(server, "_resolve_username", lambda confirmed="": "alice")

        result = server.init_feature(
            name="My Feature",
            slug="my-feature",
            summary="does a thing",
            project="myproj",
        )
        assert "Created feature KB" in result
        readme = (kb_env / "myproj" / "my-feature" / "README.md").read_text()
        # untouched -- not mistaken for a real placeholder, even though it
        # contains the literal word "slug" inside curly braces
        assert "{project-name}/{slug}/README.md" in readme
        assert "$unknown_var" in readme  # unrecognized $-token left as-is, no crash
        assert "# My Feature" in readme


# ---------------------------------------------------------------------------
# load_feature_context (end-to-end)
# ---------------------------------------------------------------------------


class TestLoadFeatureContext:
    def test_loads_readme_and_memories(self, kb_env):
        make_feature(
            kb_env,
            "myproj",
            "my-feature",
            readme="<overview>\nDoes a thing.\n</overview>",
            memories={"alice": "- **2026-01-01** [id:aaaa]: **[gotcha] insight**\n"},
        )
        result = server.load_feature_context(slug="my-feature", project="myproj")
        assert "Does a thing." in result
        assert "insight" in result
        assert "Feature context: myproj/my-feature" in result

    def test_not_found_returns_message(self, kb_env):
        result = server.load_feature_context(slug="does-not-exist", project="myproj")
        assert "not found" in result.lower()

    def test_output_is_byte_for_byte_stable(self, kb_env):
        """Pin the exact rendered output — the contract the Copilot adapter must keep.

        The Copilot hook calls this same function to build `additionalContext`,
        so any change here silently changes what Copilot sees. The substring
        assertions used elsewhere in this class would not catch reordering,
        spacing changes, or footer rewording — all of which do alter the
        injected payload.

        If this fails and the change is intentional, update the literal below in
        the same commit. If it is not intentional, revert the change.
        """
        make_feature(
            kb_env,
            "myproj",
            "my-feature",
            readme=(
                "# my-feature\n\n"
                "<overview>\nDoes a thing.\n</overview>\n\n"
                "<key_files>\n- `src/thing.py` — core\n</key_files>\n"
            ),
            memories={"alice": "- **2026-01-01** [id:aaaa]: **[gotcha] insight**\n"},
        )
        expected = (
            "# Feature context: myproj/my-feature "
            "(~37 tokens: README ~25 + memories ~12)\n"
            "\n## README.md\n"
            "\n# my-feature\n"
            "\n<overview>\nDoes a thing.\n</overview>\n"
            "\n<key_files>\n- `src/thing.py` — core\n</key_files>\n"
            "\n## memories\n"
            "\n- **2026-01-01** [id:aaaa]: **[gotcha] insight**\n"
            "\n---\n"
            "**Apply immediately:** critical_warnings, business_rules, and "
            "architecture above constrain every decision this session — apply "
            "them before acting, don't just skim past them.\n"
            "**Stale-check:** if a memory above carries [supersedes:XXXX] and the "
            "replaced claim still sits verbatim in critical_warnings, "
            "business_rules, architecture, or open_items, that section is stale — "
            "fix it now via update_readme, don't wait for /recall:save. A "
            "[resolved:XXXX] memory is the opposite case: its block in README is "
            "the live copy, so leave it alone.\n"
            "**Inline save rule for this session:** the moment you discover a "
            "bug root cause, non-obvious constraint, gotcha, or rejected "
            "approach — call `save_memory(slug='my-feature', ...)` immediately "
            "at that point. Do not defer to end of session — deferred saves "
            "are forgotten.\n"
            "**Cross-feature uncertainty:** when unsure whether a "
            "related/mapped feature KB actually applies to the current task, "
            "load it anyway — missing cross-feature context costs more than "
            "one extra `load_feature_context` call. "
            "Exception: a KB you are considering LINKING the current unmapped "
            "branch to is not 'related' — linking is the user's call, so ask "
            "before loading it."
        )
        result = server.load_feature_context(slug="my-feature", project="myproj")
        assert result == expected

    def test_oversized_kb_returns_short_error_not_full_dump(self, kb_env):
        huge = "x" * (server.KB_FULL_RENDER_LIMIT_CHARS + 1000)
        make_feature(
            kb_env, "myproj", "huge-feature", readme=f"<overview>\n{huge}\n</overview>"
        )
        result = server.load_feature_context(slug="huge-feature", project="myproj")
        assert "too large to load safely" in result
        assert len(result) < 2000  # short error, nowhere near the huge dump itself

    def test_oversized_kb_does_not_suggest_reading_raw_memories(self, kb_env):
        huge = "x" * (server.KB_FULL_RENDER_LIMIT_CHARS + 1000)
        make_feature(
            kb_env, "myproj", "huge-feature", readme=f"<overview>\n{huge}\n</overview>"
        )
        result = server.load_feature_context(slug="huge-feature", project="myproj")
        # raw memories files bypass _merge_memories' [supersedes]/[resolved]
        # filtering, so retired entries would look current -- reading them must
        # be actively discouraged, not suggested as a generic workaround.
        assert "Do NOT read" in result and "memories-*.md" in result
        assert "/recall:compact" in result and "/recall:tidy" in result

    def test_logs_memories_date_parse_failures(self, kb_env):
        make_feature(
            kb_env,
            "myproj",
            "my-feature",
            readme="<overview>x</overview>",
            memories={"alice": "- **[gotcha] no date, hand-edited**\n"},
        )
        server.load_feature_context(slug="my-feature", project="myproj")
        entries = [
            json.loads(line)
            for line in (kb_env / "usage.jsonl").read_text().splitlines()
        ]
        last = entries[-1]
        assert last["tool"] == "load_feature_context"
        assert last["memories_date_parse_failures"] == 1

    def test_hub_key_files_path_shown_as_weak_not_dropped(self, kb_env):
        # OI-1 regression test: previously a path shared by >=3 slugs vanished
        # from the footer entirely instead of surfacing as low-confidence.
        make_feature(kb_env, "myproj", "a", readme=_kf_readme("- `hub.py` — core"))
        make_feature(kb_env, "myproj", "b", readme=_kf_readme("- `hub.py` — core"))
        make_feature(kb_env, "myproj", "c", readme=_kf_readme("- `hub.py` — core"))
        result = server.load_feature_context(slug="a", project="myproj")
        assert "hub.py" in result
        assert "weak signal" in result


class TestBudgetedRender:
    """`max_chars` / `sections` — the budget path the Copilot adapter drives.

    The default path (both None) is pinned byte-for-byte by
    `test_output_is_byte_for_byte_stable` above; these cover only what the budget
    adds, so Claude Code's render stays free to change independently.
    """

    @staticmethod
    def _kb(kb_env, cw="- a warning", arch="Short arch."):
        return make_feature(
            kb_env,
            "myproj",
            "my-feature",
            readme=(
                "# my-feature\n\n"
                "<overview>\nShort overview.\n</overview>\n\n"
                f"<critical_warnings>\n{cw}\n</critical_warnings>\n\n"
                f"<architecture>\n{arch}\n</architecture>\n"
            ),
        )

    def test_under_budget_includes_everything_and_adds_no_directive(self, kb_env):
        self._kb(kb_env)
        result = server.load_feature_context(
            slug="my-feature", project="myproj", max_chars=9000
        )
        assert "a warning" in result and "Short arch." in result
        assert "⚠️" not in result

    def test_over_budget_drops_lowest_priority_whole_and_directs_from_the_top(
        self, kb_env
    ):
        self._kb(kb_env, cw="- keep this warning", arch="filler " * 3000)
        result = server.load_feature_context(
            slug="my-feature", project="myproj", max_chars=2000
        )

        assert len(result) <= 2000
        assert "keep this warning" in result  # priority 4 survives
        assert "filler " not in result  # priority 2 dropped WHOLE, never mid-entry
        # `overview` (priority 1, tiny) goes with it: the render stops at the first
        # unit that does not fit, so a lower-priority unit is never rescued by
        # being smaller. See test_priority_beats_size... below.
        assert "Omitted: architecture, overview" in result
        assert "sections=['architecture', 'overview']" in result
        # Above the body, not a trailing note: the reader must know the KB is
        # partial *before* reading it, not at the point it stops reading.
        assert result.index("⚠️") < result.index("## README.md")
        assert result.startswith("# Feature context: myproj/my-feature")

    def test_priority_beats_size_when_a_high_priority_section_is_too_big(self, kb_env):
        """Regression (2026-09-26): the loop used to *skip* a unit that did not fit
        and carry on with later ones, so SIZE beat priority. Measured on a real KB:
        at `max_chars=8900` `business_rules` (priority 3, 756 chars) was dropped
        while `overview` + `related_tickets` (priority 1) were kept, and below
        ~8200 `critical_warnings` (priority 4) was dropped the same way — the
        silent-loss mode MISS `2fedf3` recorded. The admitted set must be a PREFIX
        of SECTION_PRIORITY order."""
        make_feature(
            kb_env,
            "myproj",
            "sized",
            readme=(
                "<critical_warnings>\n- keep this warning\n</critical_warnings>\n\n"
                f"<business_rules>\n{'x' * 4000}\n</business_rules>\n\n"
                "<overview>\nA tiny, cheap overview.\n</overview>\n"
            ),
        )
        result = server.load_feature_context(
            slug="sized", project="myproj", max_chars=2500
        )

        assert "keep this warning" in result  # priority 4 stays
        assert "xxxx" not in result  # priority 3 too big — dropped whole
        assert "A tiny, cheap overview." not in result  # priority 1 not rescued
        assert "Omitted: business_rules, overview" in result

    def test_an_impossible_budget_still_renders_the_fixed_frame(self, kb_env):
        """The directive cannot be honoured below the fixed overhead (header +
        hints + directive + index + footer), so the floor is that frame — not an
        empty string and not a mid-entry cut. Copilot's 9k budget is ~9x the
        frame, so this only matters as a floor, not as a real path."""
        self._kb(kb_env, arch="filler " * 3000)
        result = server.load_feature_context(
            slug="my-feature", project="myproj", max_chars=1
        )

        assert result.startswith("# Feature context: myproj/my-feature")
        assert "Omitted: critical_warnings, architecture" in result
        assert "filler " not in result

    def test_sections_allow_list_renders_only_those_and_omits_memories(self, kb_env):
        d = self._kb(kb_env, cw="- only me", arch="arch text")
        (d / "memories-alice.md").write_text(
            "- **2026-01-01** [id:aaaa]: **[gotcha] uniquememorytoken**\n"
        )
        result = server.load_feature_context(
            slug="my-feature", project="myproj", sections=["critical_warnings"]
        )

        assert "only me" in result
        assert "arch text" not in result
        assert "uniquememorytoken" not in result  # a filter never carries memories

    def test_omitted_memories_direct_to_a_full_render(self, kb_env):
        d = self._kb(kb_env)
        (d / "memories-alice.md").write_text(
            "".join(
                f"- **2026-01-{i:02d}** [id:{i:04x}]: **[gotcha] memory {i}**\n"
                for i in range(1, 40)
            )
        )
        result = server.load_feature_context(
            slug="my-feature", project="myproj", max_chars=2500
        )

        assert "Omitted: memories" in result
        # Memories have no allow-list equivalent, so the only route back is the
        # budget-free default render — the directive must say so, not point at
        # `sections=`.
        assert "load_feature_context('my-feature')" in result
        assert "sections=" not in result

    def test_memories_split_per_entry_admits_newest_prefix(self, kb_env):
        """Regression (2026-09-26): memories were admitted as ONE atomic unit, so
        a budget below the whole block dropped ALL memories — an oversized KB could
        never load a single one. Splitting per entry lets the admit loop take a
        PREFIX of the newest entries instead, so the newest load-bearing memories
        still land even when the full tier cannot."""
        d = self._kb(kb_env)
        (d / "memories-alice.md").write_text(
            "".join(
                f"- **2026-01-{i:02d}** [id:{i:04x}]: **[gotcha] memory token {i}**\n"
                for i in range(1, 31)
            )
        )
        result = server.load_feature_context(
            slug="my-feature", project="myproj", max_chars=2500
        )

        # Newest entries admitted first, oldest dropped — a prefix, never a
        # whole-tier drop. Assert on the unique entry ids, not the title (a bare
        # "token 1" would also match "token 19").
        assert "[id:001e]" in result  # i=30 -> 0x1e, the newest entry
        assert "[id:0001]" not in result  # i=1 -> 0x0001, the oldest entry
        assert "Omitted: memories" in result
        assert "load_feature_context('my-feature')" in result

    def test_expand_ids_returns_body_even_below_the_frame_budget(self, kb_env):
        """Regression (2026-09-26): `expand_ids` is the index's escape hatch — the
        directive tells the caller to fetch a title-only entry's full body by id.
        But the requested body was thrown into the same budget-limited units as
        everything else, so at a budget below the fixed frame (header + hints +
        directive + index + footer) the render admitted ZERO units and the body was
        lost. Expanded bodies must be reserved (always shown), not budget-limited.
        """
        d = self._kb(kb_env)
        (d / "memories-alice.md").write_text(
            "- **2026-01-01** [id:aaaa]: **[pattern] a title-only pattern**\n"
            "What: the full pattern body, only reachable via expand_ids.\n"
            "Why: it is a pattern, kept title-only by default.\n"
            "Apply: fetch it when the title looks relevant.\n"
        )
        result = server.load_feature_context(
            slug="my-feature", project="myproj", max_chars=100, expand_ids=["aaaa"]
        )

        assert "the full pattern body, only reachable via expand_ids" in result
        assert "[id:aaaa]" in result

    def test_expand_ids_body_survives_even_when_other_content_is_dropped(self, kb_env):
        """The expanded body is reserved and shown even though the budget drops the
        README sections and regular memories — an explicit request beats the prefix
        admission."""
        d = self._kb(kb_env, cw="- keep this warning", arch="filler " * 3000)
        (d / "memories-alice.md").write_text(
            "- **2026-01-01** [id:aaaa]: **[gotcha] an old gating memory**\n"
            "What: a requested body that must survive.\n"
            "Why: caller asked for it by id.\n"
            "Apply: show it.\n"
        )
        result = server.load_feature_context(
            slug="my-feature", project="myproj", max_chars=500, expand_ids=["aaaa"]
        )

        assert "a requested body that must survive" in result

    def test_index_all_demotes_memories_to_titles_when_over_hard_limit(self, kb_env):
        """Index-all threshold: a KB whose full-body memories exceed the hard limit
        is no longer a dead end — non-requested entries render title-only, so the
        KB stays loadable (bodies via expand_ids). Small KBs keep eager full-body.
        """
        d = self._kb(kb_env)
        # Many gating entries whose full bodies push the KB over the full-render
        # limit (Claude's default-path threshold).
        (d / "memories-alice.md").write_text(
            "".join(
                f"- **2026-01-{i:02d}** [id:{i:04x}]: **[gotcha] invariant {i}**\n"
                f"What: body for invariant {i}, " + "x" * 2000 + "\n"
                for i in range(1, 30)
            )
        )
        result = server.load_feature_context(slug="my-feature", project="myproj")

        # No "too large" error — the KB renders instead, title-only.
        assert "too large to load safely" not in result
        # Titles are present, full bodies are NOT.
        assert "invariant 29" in result
        assert "body for invariant 29" not in result
        # The index is labeled as all-memories, and points at expand_ids.
        assert "all memories" in result
        assert "expand_ids" in result

    def test_index_all_small_kb_keeps_full_body(self, kb_env):
        """A KB under the hard limit keeps the eager full-body behavior — index-all
        only kicks in past the threshold."""
        d = self._kb(kb_env)
        (d / "memories-alice.md").write_text(
            "- **2026-01-01** [id:aaaa]: **[gotcha] a small insight**\n"
            "What: the full body stays visible.\n"
            "Why: the KB is small.\n"
            "Apply: keep eager.\n"
        )
        result = server.load_feature_context(slug="my-feature", project="myproj")
        assert "the full body stays visible" in result

    def test_budgeted_render_demotes_to_titles_above_the_budget(self, kb_env):
        """Copilot (a budgeted call) demotes memories to titles as soon as full
        body exceeds its budget — far lower than Claude's full-render limit. This
        is the platform split: the same KB stays full-body for Claude."""
        d = self._kb(kb_env)
        (d / "memories-alice.md").write_text(
            "".join(
                f"- **2026-01-{i:02d}** [id:{i:04x}]: **[gotcha] invariant {i}**\n"
                f"What: body for invariant {i}, " + "x" * 300 + "\n"
                for i in range(1, 15)
            )
        )
        result = server.load_feature_context(
            slug="my-feature", project="myproj", max_chars=1500
        )
        assert "too large to load safely" not in result
        assert "invariant 14" in result  # title present
        assert "body for invariant 14" not in result  # full body demoted to title
        assert "all memories" in result

    def test_index_titles_are_capped_with_a_search_pointer(self, kb_env):
        """The title index is reserved in the render's fixed frame; without a cap
        it grows with entry count and starves README sections of room. Many titles
        get capped, with a pointer to search_features for the dropped ones."""
        d = self._kb(kb_env)
        (d / "memories-alice.md").write_text(
            "".join(
                f"- **2026-01-{i:02d}** [id:{i:04x}]: "
                f"**[gotcha] a long descriptive title number {i} that eats budget**\n"
                f"What: body " + "x" * 500 + "\n"
                for i in range(1, 40)
            )
        )
        result = server.load_feature_context(
            slug="my-feature", project="myproj", max_chars=4000
        )
        assert "older titles omitted" in result
        assert "search_features" in result

    def test_a_targeted_render_rescues_a_kb_over_the_hard_limit(self, kb_env):
        """The oversize gate is a dead end only for the budget-free render. A
        section filter is small by construction, so refusing it too would leave
        the KB unloadable until maintenance ran."""
        huge = "x" * (server.KB_FULL_RENDER_LIMIT_CHARS + 1000)
        make_feature(
            kb_env,
            "myproj",
            "huge",
            readme=(
                "<critical_warnings>\n- keep this\n</critical_warnings>\n\n"
                f"<overview>\n{huge}\n</overview>"
            ),
        )
        error = server.load_feature_context(slug="huge", project="myproj")
        assert "too large" in error
        # The error must name the way out — it is no longer a dead end, and a
        # "do not retry this call" that stops there would strand the caller.
        assert "sections=['critical_warnings']" in error
        assert "max_chars" in error

        result = server.load_feature_context(
            slug="huge", project="myproj", sections=["critical_warnings"]
        )
        assert "keep this" in result
        assert "too large" not in result

    def test_unknown_sections_are_rejected_by_name(self, kb_env):
        self._kb(kb_env)
        result = server.load_feature_context(
            slug="my-feature", project="myproj", sections=["critical_warning"]
        )
        assert "Unknown section" in result
        assert "critical_warning" in result

    @pytest.mark.parametrize("bad", [0, -1])
    def test_non_positive_max_chars_is_rejected(self, kb_env, bad):
        self._kb(kb_env)
        result = server.load_feature_context(
            slug="my-feature", project="myproj", max_chars=bad
        )
        assert "Invalid max_chars" in result


class TestLoadArgTypeGuards:
    """Wrong-*type* args on `load_feature_context`'s three union optionals.

    `expand_ids` / `max_chars` / `sections` are declared `list[str] | None` and
    `int | None`, so MCP emits `anyOf: [{type: array|integer}, {type: null}]` —
    a shape a client or provider may flatten, leaving a small model free to emit
    a bare string. Before the guards, each degraded differently and only one of
    the three said so out loud. Coercion is deliberately limited to the shapes
    with exactly one correct reading; the rest get a directive to re-issue.
    """

    @staticmethod
    def _kb(kb_env, cw="- a warning", arch="Short arch."):
        return make_feature(
            kb_env,
            "myproj",
            "my-feature",
            readme=(
                "# my-feature\n\n"
                "<overview>\nShort overview.\n</overview>\n\n"
                f"<critical_warnings>\n{cw}\n</critical_warnings>\n\n"
                f"<architecture>\n{arch}\n</architecture>\n"
            ),
        )

    def test_max_chars_as_numeric_string_is_coerced(self, kb_env):
        """`"9000"` has one correct reading, so it is accepted — but note this is
        not evidence the schema reached the model intact; only the harness sees
        that, by inspecting the raw argument types."""
        self._kb(kb_env)
        result = server.load_feature_context(
            slug="my-feature", project="myproj", max_chars="9000"
        )
        assert "a warning" in result
        assert "Invalid max_chars" not in result

    @pytest.mark.parametrize("bad", ["lots", True])
    def test_uncoercible_max_chars_is_rejected_instead_of_crashing(self, kb_env, bad):
        """Regression (2026-10-01): `max_chars="9000"` raised
        `TypeError: '<=' not supported between instances of 'str' and 'int'` from
        the value guard itself, so the "Invalid max_chars" message was never
        reachable. `True` is the second case because `isinstance(True, int)` is
        True — without an explicit bool check it sailed through as `max_chars=1`.
        """
        self._kb(kb_env)
        result = server.load_feature_context(
            slug="my-feature", project="myproj", max_chars=bad
        )
        assert "Invalid max_chars" in result
        assert "Re-issue" in result

    def test_sections_as_string_is_coerced_not_iterated_char_by_char(self, kb_env):
        """Regression (2026-10-01): a bare string iterated character-by-character
        and reported `Unknown section(s): 'c', 'r', 'i', 't', ...`."""
        self._kb(kb_env)
        result = server.load_feature_context(
            slug="my-feature", project="myproj", sections="critical_warnings"
        )
        assert "a warning" in result
        assert "Unknown section" not in result

    def test_sections_as_comma_string_is_coerced_to_a_list(self, kb_env):
        self._kb(kb_env)
        result = server.load_feature_context(
            slug="my-feature", project="myproj", sections="critical_warnings,overview"
        )
        assert "a warning" in result and "Short overview." in result

    def test_sections_of_the_wrong_type_is_rejected(self, kb_env):
        self._kb(kb_env)
        result = server.load_feature_context(
            slug="my-feature", project="myproj", sections=123
        )
        assert "Invalid sections" in result
        assert "got int" in result

    def test_expand_ids_as_string_is_coerced_not_silently_ignored(self, kb_env):
        """Regression (2026-10-01) — the worst of the three: `expand_ids="aaaa"`
        iterated character-by-character, matched no id, and returned a NORMAL
        successful render with the requested body silently absent. Nothing in the
        output signalled the failure, so there was nothing to self-correct from.
        """
        d = self._kb(kb_env)
        (d / "memories-alice.md").write_text(
            "- **2026-01-01** [id:aaaa]: **[pattern] a title-only pattern**\n"
            "What: the body that must actually be fetched.\n"
            "Why: it is a pattern, kept title-only by default.\n"
            "Apply: fetch it when the title looks relevant.\n"
        )
        result = server.load_feature_context(
            slug="my-feature", project="myproj", max_chars=100, expand_ids="aaaa"
        )
        assert "the body that must actually be fetched" in result
        assert "matched no live entry" not in result

    def test_unknown_expand_id_is_reported_not_silently_ignored(self, kb_env):
        """The type guard above fixes the wrong-*type* path; this fixes the
        wrong-*value* path, which was equally silent. An id that matches nothing
        produced a normal, successful render minus the requested body — the one
        failure shape with no signal to self-correct from. A mistyped id and a
        retired id look identical from the server, so it names the id rather than
        guessing which it was.
        """
        d = self._kb(kb_env)
        (d / "memories-alice.md").write_text(
            "- **2026-01-01** [id:aaaa]: **[pattern] a title-only pattern**\n"
            "What: a body.\nWhy: x.\nApply: y.\n"
        )
        result = server.load_feature_context(
            slug="my-feature", project="myproj", max_chars=100, expand_ids="zzzzzz"
        )
        assert "matched no live entry" in result
        assert "zzzzzz" in result
        assert "NOT included" in result

    def test_expand_ids_of_the_wrong_type_is_rejected(self, kb_env):
        """A list whose elements are not strings has no correct reading, so it is
        rejected rather than `str()`-coerced into ids the caller never meant."""
        self._kb(kb_env)
        result = server.load_feature_context(
            slug="my-feature", project="myproj", expand_ids=[1, 2]
        )
        assert "Invalid expand_ids" in result
        assert "got list" in result


class TestNonStringTextGuards:
    """`content` / `description` are declared `str`, so their schema is clean —
    but a model can still emit a number, and both tools then answered with a bare
    `TypeError` raised *outside* their own `try` block (from `re.search`/`len`),
    so the whole tool result was an unactionable interpreter message.
    """

    @pytest.mark.parametrize("bad", [123, ["a"], {"a": 1}])
    def test_save_memory_rejects_non_string_content(self, kb_env, bad):
        make_feature(kb_env, "myproj", "my-feature", readme="# f\n")
        result = server.save_memory(slug="my-feature", project="myproj", content=bad)
        assert "Invalid content" in result
        assert "Re-issue" in result

    def test_report_miss_rejects_non_string_description(self, kb_env):
        make_feature(kb_env, "myproj", "my-feature", readme="# f\n")
        result = server.report_miss(
            slug="my-feature", project="myproj", description=123
        )
        assert "Invalid description" in result
        assert "got int" in result


class TestReportMissEntrySizeGuard:
    """report_miss had no write gate at all — the three largest entries in the
    live store are all MISSes. It shares save_memory's per-entry ceiling."""

    def test_miss_over_hard_limit_rejected(self, kb_env, monkeypatch):
        monkeypatch.setattr(server, "_resolve_username", lambda confirmed="": "alice")
        d = make_feature(kb_env, "myproj", "my-feature", readme="# f\n")
        result = server.report_miss(
            slug="my-feature",
            project="myproj",
            description="x" * (server.KB_MEMORY_ENTRY_HARD_LIMIT_CHARS + 100),
        )
        assert "Rejected" in result
        assert "per-entry" in result
        assert not (d / "memories-alice.md").exists()

    def test_miss_over_soft_limit_gets_note(self, kb_env, monkeypatch):
        monkeypatch.setattr(server, "_resolve_username", lambda confirmed="": "alice")
        d = make_feature(kb_env, "myproj", "my-feature", readme="# f\n")
        result = server.report_miss(
            slug="my-feature",
            project="myproj",
            description="y" * (server.KB_MEMORY_ENTRY_SOFT_LIMIT_CHARS + 100),
        )
        assert "soft limit" in result
        assert (d / "memories-alice.md").exists()

    def test_miss_under_soft_limit_no_note(self, kb_env, monkeypatch):
        monkeypatch.setattr(server, "_resolve_username", lambda confirmed="": "alice")
        d = make_feature(kb_env, "myproj", "my-feature", readme="# f\n")
        result = server.report_miss(
            slug="my-feature",
            project="myproj",
            description="A short miss description.",
        )
        assert "soft limit" not in result
        assert (d / "memories-alice.md").exists()


class TestReportMissGrowthGate:
    """report_miss shares the save-side growth gates — until 2026-10-03 it had
    none, so a MISS could push an already-large KB past the load limit."""

    def test_miss_refused_when_write_would_make_kb_unloadable(
        self, kb_env, monkeypatch
    ):
        monkeypatch.setattr(server, "_resolve_username", lambda confirmed="": "alice")
        big_readme = "x" * (server.KB_FULL_RENDER_LIMIT_CHARS - 500)
        d = make_feature(
            kb_env, "myproj", "my-feature", readme=f"<overview>{big_readme}</overview>"
        )
        result = server.report_miss(
            slug="my-feature", project="myproj", description="y" * 1000
        )

        assert "Save blocked" in result
        assert "NOT written" in result
        assert not (d / "memories-alice.md").exists()

    def test_miss_refused_when_full_body_exceeds_ceiling(self, kb_env, monkeypatch):
        monkeypatch.setattr(server, "_resolve_username", lambda confirmed="": "alice")
        huge_body = "y" * (server.KB_FULL_BODY_GROWTH_LIMIT_CHARS + 100)
        d = make_feature(
            kb_env,
            "myproj",
            "my-feature",
            readme="<overview>x</overview>",
            memories={
                "alice": f"- **2026-01-01** [id:aaaa]: **[gotcha] big**\n{huge_body}\n"
            },
        )
        result = server.report_miss(
            slug="my-feature", project="myproj", description="z" * 500
        )

        assert "growth ceiling" in result
        assert "NOT written" in result
        # Refused before the write — the MISS never reached the file.
        assert "zzz" not in (d / "memories-alice.md").read_text()

    def test_miss_allowed_under_ceiling(self, kb_env, monkeypatch):
        monkeypatch.setattr(server, "_resolve_username", lambda confirmed="": "alice")
        d = make_feature(
            kb_env, "myproj", "my-feature", readme="<overview>x</overview>"
        )
        result = server.report_miss(
            slug="my-feature", project="myproj", description="A normal miss."
        )
        assert "Save blocked" not in result
        assert (d / "memories-alice.md").exists()


# ---------------------------------------------------------------------------
# search_features
# ---------------------------------------------------------------------------


class TestSearchFeatures:
    def test_basic_or_match_hit(self, kb_env):
        make_feature(
            kb_env,
            "myproj",
            "my-feature",
            readme="<overview>\nSomething about myuniquekeyword here.\n</overview>",
        )
        result = server.search_features(query="myuniquekeyword", project="myproj")
        assert "my-feature" in result
        assert "1 hit(s)" in result

    def test_stopwords_dropped_from_matching(self, kb_env):
        # "not" alone would otherwise match the noise feature's line even
        # though it has nothing to do with the real, distinctive keyword.
        make_feature(
            kb_env,
            "myproj",
            "noise-feature",
            readme="<overview>\nThis is not a big deal.\n</overview>",
        )
        make_feature(
            kb_env,
            "myproj",
            "real-feature",
            readme="<overview>\nFound myuniquekeyword here.\n</overview>",
        )
        result = server.search_features(query="not myuniquekeyword", project="myproj")
        assert "real-feature" in result
        assert "noise-feature" not in result
        assert "ignored common word(s): not" in result

    def test_stopwords_only_query_falls_back_instead_of_erroring(self, kb_env):
        make_feature(
            kb_env,
            "myproj",
            "my-feature",
            readme="<overview>\nThis is a test.\n</overview>",
        )
        result = server.search_features(query="not a", project="myproj")
        assert "my-feature" in result
        assert "ignored common word(s)" not in result

    def test_relevance_ranking_survives_truncation_over_alphabetical(
        self, kb_env, monkeypatch
    ):
        # "aaa-slug" sorts first alphabetically but only matches 1 of the 2
        # keywords; "zzz-slug" matches both. Relevance ranking must put
        # zzz-slug first so it survives a tight MAX_SEARCH_RESULTS truncation,
        # instead of the old alphabetical-only order hiding the better match.
        make_feature(
            kb_env,
            "myproj",
            "aaa-slug",
            readme="<overview>\nalpha only here.\n</overview>",
        )
        make_feature(
            kb_env,
            "myproj",
            "zzz-slug",
            readme="<overview>\nalpha and beta both here.\n</overview>",
        )
        monkeypatch.setattr(server, "MAX_SEARCH_RESULTS", 1)
        result = server.search_features(query="alpha beta", project="myproj")
        assert "zzz-slug" in result
        assert "aaa-slug" not in result
        assert "truncated at 1 of 2" in result

    def test_hit_annotated_with_matched_keyword_count(self, kb_env):
        make_feature(
            kb_env,
            "myproj",
            "partial-slug",
            readme="<overview>\nalpha only here.\n</overview>",
        )
        make_feature(
            kb_env,
            "myproj",
            "full-slug",
            readme="<overview>\nalpha and beta both here.\n</overview>",
        )
        result = server.search_features(query="alpha beta", project="myproj")
        assert "(1/2 kw)" in result  # partial-slug: only "alpha"
        assert "(2/2 kw)" in result  # full-slug: both keywords


# ---------------------------------------------------------------------------
# update_feature_index
# ---------------------------------------------------------------------------


class _FixedDate:
    @classmethod
    def today(cls):
        return date(2026, 1, 15)


def _write_features_md(kb_root, project_name, rows):
    """rows: list of (name, slug, ticket, branch, summary, last_updated) tuples."""
    header = (
        "# Feature Knowledge Base Index\n\n"
        "| Feature | Slug | Ticket(s) | Branch(es) | Summary | Last Updated |\n"
        "|---|---|---|---|---|---|\n"
    )
    body = "\n".join("| " + " | ".join(r) + " |" for r in rows)
    (kb_root / project_name / "features.md").write_text(header + body + "\n")


class TestUpdateFeatureIndex:
    def test_unknown_field_returns_error(self, kb_env):
        make_feature(kb_env, "myproj", "payment-gateway")
        result = server.update_feature_index(
            slug="payment-gateway", field="bogus", value="x", project="myproj"
        )
        assert "Unknown field" in result

    def test_slug_not_found_returns_not_found_msg(self, kb_env):
        result = server.update_feature_index(
            slug="nope", field="branch", value="feat/x", project="myproj"
        )
        assert "not found" in result.lower() or "No index row" in result

    def test_missing_features_md_returns_message(self, kb_env):
        make_feature(kb_env, "myproj", "payment-gateway")
        result = server.update_feature_index(
            slug="payment-gateway", field="branch", value="feat/x", project="myproj"
        )
        assert "No features.md index found" in result

    def test_confirm_false_returns_diff_and_writes_nothing(self, kb_env, monkeypatch):
        make_feature(kb_env, "myproj", "payment-gateway")
        _write_features_md(
            kb_env,
            "myproj",
            [
                (
                    "Payment Gateway",
                    "payment-gateway",
                    "",
                    "feat/v1",
                    "old summary",
                    "2026-01-01",
                )
            ],
        )
        monkeypatch.setattr(server, "date", _FixedDate)
        before = (kb_env / "myproj" / "features.md").read_text()

        result = server.update_feature_index(
            slug="payment-gateway", field="branch", value="feat/v2", project="myproj"
        )

        assert "Diff preview" in result
        assert "confirm=True" in result
        assert (kb_env / "myproj" / "features.md").read_text() == before

    def test_confirm_true_replaces_field_and_bumps_date(self, kb_env, monkeypatch):
        make_feature(kb_env, "myproj", "payment-gateway")
        _write_features_md(
            kb_env,
            "myproj",
            [
                (
                    "Payment Gateway",
                    "payment-gateway",
                    "",
                    "feat/v1",
                    "old summary",
                    "2026-01-01",
                )
            ],
        )
        monkeypatch.setattr(server, "date", _FixedDate)

        result = server.update_feature_index(
            slug="payment-gateway",
            field="branch",
            value="feat/v2",
            project="myproj",
            confirm=True,
        )

        assert "Updated features.md index row" in result
        text = (kb_env / "myproj" / "features.md").read_text()
        assert (
            "| Payment Gateway | payment-gateway |  | feat/v2 | old summary | 2026-01-15 |"
            in text
        )
        assert "feat/v1" not in text

    def test_append_true_keeps_old_value_and_adds_new(self, kb_env, monkeypatch):
        make_feature(kb_env, "myproj", "payment-gateway")
        _write_features_md(
            kb_env,
            "myproj",
            [
                (
                    "Payment Gateway",
                    "payment-gateway",
                    "",
                    "feat/v1",
                    "old summary",
                    "2026-01-01",
                )
            ],
        )
        monkeypatch.setattr(server, "date", _FixedDate)

        result = server.update_feature_index(
            slug="payment-gateway",
            field="branch",
            value="feat/v2",
            project="myproj",
            append=True,
            confirm=True,
        )

        assert "Updated features.md index row" in result
        text = (kb_env / "myproj" / "features.md").read_text()
        assert "feat/v1, feat/v2" in text

    def test_no_changes_when_value_and_date_both_already_current(
        self, kb_env, monkeypatch
    ):
        # "No changes" compares the WHOLE reconstructed row, including the
        # Last Updated cell (always rewritten to today) -- so it only fires
        # when the row's date is already today's date too, not merely when
        # the field value is unchanged.
        make_feature(kb_env, "myproj", "payment-gateway")
        _write_features_md(
            kb_env,
            "myproj",
            [
                (
                    "Payment Gateway",
                    "payment-gateway",
                    "",
                    "feat/v1",
                    "old summary",
                    "2026-01-15",
                )
            ],
        )
        monkeypatch.setattr(server, "date", _FixedDate)

        result = server.update_feature_index(
            slug="payment-gateway",
            field="branch",
            value="feat/v1",
            project="myproj",
            confirm=True,
        )

        assert "No changes" in result

    def test_date_bumped_even_when_field_value_unchanged(self, kb_env, monkeypatch):
        make_feature(kb_env, "myproj", "payment-gateway")
        _write_features_md(
            kb_env,
            "myproj",
            [
                (
                    "Payment Gateway",
                    "payment-gateway",
                    "",
                    "feat/v1",
                    "old summary",
                    "2026-01-01",
                )
            ],
        )
        monkeypatch.setattr(server, "date", _FixedDate)

        result = server.update_feature_index(
            slug="payment-gateway",
            field="branch",
            value="feat/v1",
            project="myproj",
            confirm=True,
        )

        assert "Updated features.md index row" in result
        text = (kb_env / "myproj" / "features.md").read_text()
        assert "2026-01-15" in text
        assert "2026-01-01" not in text

    def test_summary_field_maps_to_summary_column(self, kb_env, monkeypatch):
        make_feature(kb_env, "myproj", "payment-gateway")
        _write_features_md(
            kb_env,
            "myproj",
            [
                (
                    "Payment Gateway",
                    "payment-gateway",
                    "",
                    "feat/v1",
                    "old summary",
                    "2026-01-01",
                )
            ],
        )
        monkeypatch.setattr(server, "date", _FixedDate)

        result = server.update_feature_index(
            slug="payment-gateway",
            field="summary",
            value="new summary",
            project="myproj",
            confirm=True,
        )

        assert "Updated features.md index row" in result
        text = (kb_env / "myproj" / "features.md").read_text()
        assert "old summary" not in text
        assert "new summary" in text
        assert "feat/v1" in text  # branch untouched


# ---------------------------------------------------------------------------
# E — log status: rejected (guard) vs error (crash) vs ok
# ---------------------------------------------------------------------------


class TestLogStatus:
    """A guard rejection is 'rejected' (correct behaviour), a crash is 'error',
    success is 'ok'. Before, both rejection and crash logged as 'error', so the
    effectiveness metric counted correct rejections as failures."""

    def _last_record(self, kb_env):
        lines = (kb_env / "usage.jsonl").read_text().strip().splitlines()
        return json.loads(lines[-1])

    def test_guard_rejection_logs_rejected_with_reason(self, kb_env):
        make_feature(kb_env, "myproj", "my-feature", readme="<overview>x</overview>")
        server.load_feature_context(slug="my-feature", project="myproj", max_chars=0)
        rec = self._last_record(kb_env)
        assert rec["status"] == "rejected"
        assert rec["message"]  # short reason is recorded, not left blank

    def test_success_logs_ok(self, kb_env):
        make_feature(kb_env, "myproj", "my-feature", readme="<overview>x</overview>")
        server.load_feature_context(slug="my-feature", project="myproj")
        assert self._last_record(kb_env)["status"] == "ok"

    def test_crash_logs_error(self, kb_env, monkeypatch):
        make_feature(kb_env, "myproj", "my-feature", readme="<overview>x</overview>")

        def boom(*a, **k):
            raise RuntimeError("boom")

        monkeypatch.setattr(server, "_resolve_slug", boom)
        with pytest.raises(RuntimeError):
            server.load_feature_context(slug="my-feature", project="myproj")
        assert self._last_record(kb_env)["status"] == "error"


# ---------------------------------------------------------------------------
# C — "English only" language guard (tolerance, not ≥1 char)
# ---------------------------------------------------------------------------


class TestLanguageGuard:
    def test_vietnamese_content_is_rejected(self):
        err = server._validate_memory_content(
            "**[gotcha] lỗi nghiêm trọng trong luồng thanh toán**\n"
            "What: nội dung này viết hoàn toàn bằng tiếng Việt để kiểm tra gate."
        )
        assert "English" in err

    def test_cjk_content_is_rejected(self):
        err = server._validate_memory_content(
            "**[gotcha] 重大缺陷**\n"
            "What: 这是一个用中文写的内容，用来测试语言检查门禁是否生效。"
        )
        assert "English" in err

    def test_a_proper_noun_with_diacritics_passes(self):
        # Tolerance: a proper noun carrying a couple diacritics must not trip the
        # gate — the failure mode is a whole entry in another language, not a name.
        assert (
            server._validate_memory_content(
                "**[gotcha] Hồ Chí Minh's parser fails on long names**\n"
                "What: the parser mishandles long diacritic names in the input."
            )
            == ""
        )

    def test_plain_english_passes(self):
        assert (
            server._validate_memory_content(
                "**[gotcha] the pool is per-process**\n"
                "What: each worker opens its own pool so limits are not shared."
            )
            == ""
        )


# ---------------------------------------------------------------------------
# D — atomic write
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    def test_writes_content_and_leaves_no_temp_file(self, tmp_path):
        target = tmp_path / "file.md"
        server._atomic_write(target, "hello\nworld\n")
        assert target.read_text() == "hello\nworld\n"
        assert list(tmp_path.glob("*.tmp-*")) == []

    def test_overwrites_an_existing_file(self, tmp_path):
        target = tmp_path / "file.md"
        target.write_text("old")
        server._atomic_write(target, "new")
        assert target.read_text() == "new"
