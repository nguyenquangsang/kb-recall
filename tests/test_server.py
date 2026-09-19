import json

import pytest
from datetime import date
from pathlib import Path

from kb_recall import server


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


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
        content = "\n".join(
            [
                "- src/foo.py — `SomeClass`",
                "- src/bar.py — `OtherClass`",
                "- `src/good.py` — this one is fine",
            ]
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
        # since architecture is prose by design (TODO #41) and rarely uses tags
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
        d, slug, _ = server._resolve_slug("nonexistent", [Path("myproj")])
        assert d is None

    def test_fuzzy_match_close_typo(self, kb_env):
        make_feature(kb_env, "myproj", "payment-gateway")
        d, slug, was_fuzzy = server._resolve_slug("payment-gatewy", [Path("myproj")])
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
        # Known gap (TODO #46): a slug that exists VERBATIM in a DIFFERENT
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

    def test_oversized_kb_returns_short_error_not_full_dump(self, kb_env):
        huge = "x" * (server.KB_CONTEXT_HARD_LIMIT_CHARS + 1000)
        make_feature(
            kb_env, "myproj", "huge-feature", readme=f"<overview>\n{huge}\n</overview>"
        )
        result = server.load_feature_context(slug="huge-feature", project="myproj")
        assert "too large to load safely" in result
        assert len(result) < 2000  # short error, nowhere near the huge dump itself

    def test_oversized_kb_does_not_suggest_reading_raw_memories(self, kb_env):
        huge = "x" * (server.KB_CONTEXT_HARD_LIMIT_CHARS + 1000)
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
            [("Payment Gateway", "payment-gateway", "", "feat/v1", "old summary", "2026-01-01")],
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
            [("Payment Gateway", "payment-gateway", "", "feat/v1", "old summary", "2026-01-01")],
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
        assert "| Payment Gateway | payment-gateway |  | feat/v2 | old summary | 2026-01-15 |" in text
        assert "feat/v1" not in text

    def test_append_true_keeps_old_value_and_adds_new(self, kb_env, monkeypatch):
        make_feature(kb_env, "myproj", "payment-gateway")
        _write_features_md(
            kb_env,
            "myproj",
            [("Payment Gateway", "payment-gateway", "", "feat/v1", "old summary", "2026-01-01")],
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

    def test_no_changes_when_value_and_date_both_already_current(self, kb_env, monkeypatch):
        # "No changes" compares the WHOLE reconstructed row, including the
        # Last Updated cell (always rewritten to today) -- so it only fires
        # when the row's date is already today's date too, not merely when
        # the field value is unchanged.
        make_feature(kb_env, "myproj", "payment-gateway")
        _write_features_md(
            kb_env,
            "myproj",
            [("Payment Gateway", "payment-gateway", "", "feat/v1", "old summary", "2026-01-15")],
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
            [("Payment Gateway", "payment-gateway", "", "feat/v1", "old summary", "2026-01-01")],
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
            [("Payment Gateway", "payment-gateway", "", "feat/v1", "old summary", "2026-01-01")],
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
