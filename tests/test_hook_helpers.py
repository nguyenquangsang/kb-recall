import json


from kb_recall.hooks.hook_helpers import (
    append_turn,
    count_turns,
    find_malformed_rows,
    find_slug_for_branch,
    load_asked,
    mark_asked,
    parse_pipe_table,
    pick_active_slug,
)

SID = "session-abc"
OTHER_SID = "session-xyz"


# ---------------------------------------------------------------------------
# load_asked
# ---------------------------------------------------------------------------


class TestLoadAsked:
    def test_returns_empty_when_file_missing(self, tmp_path):
        assert load_asked(SID, tmp_path / "state") == set()

    def test_returns_empty_for_different_session(self, tmp_path):
        state = tmp_path / "state"
        state.write_text(
            json.dumps({"session_id": OTHER_SID, "slug": "feature-x"}) + "\n"
        )
        assert load_asked(SID, state) == set()

    def test_returns_slugs_for_matching_session(self, tmp_path):
        state = tmp_path / "state"
        state.write_text(
            json.dumps({"session_id": SID, "slug": "feature-a"})
            + "\n"
            + json.dumps({"session_id": SID, "slug": "__index__"})
            + "\n"
            + json.dumps({"session_id": OTHER_SID, "slug": "feature-b"})
            + "\n"
        )
        assert load_asked(SID, state) == {"feature-a", "__index__"}

    def test_skips_malformed_lines(self, tmp_path):
        state = tmp_path / "state"
        state.write_text(
            "not-json\n" + json.dumps({"session_id": SID, "slug": "ok"}) + "\n" + "\n"
        )
        assert load_asked(SID, state) == {"ok"}


# ---------------------------------------------------------------------------
# mark_asked
# ---------------------------------------------------------------------------


class TestMarkAsked:
    def test_creates_file_and_appends(self, tmp_path):
        state = tmp_path / "state"
        mark_asked(SID, "my-slug", state)
        lines = state.read_text().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0]) == {"session_id": SID, "slug": "my-slug"}

    def test_appends_multiple_entries(self, tmp_path):
        state = tmp_path / "state"
        mark_asked(SID, "slug-a", state)
        mark_asked(SID, "slug-b", state)
        slugs = {json.loads(line)["slug"] for line in state.read_text().splitlines()}
        assert slugs == {"slug-a", "slug-b"}

    def test_caps_at_500_lines(self, tmp_path):
        state = tmp_path / "state"
        state.write_text(
            "\n".join(
                json.dumps({"session_id": "old", "slug": f"s{i}"}) for i in range(500)
            )
            + "\n"
        )
        mark_asked(SID, "new-slug", state)
        lines = state.read_text().splitlines()
        assert len(lines) == 500
        assert json.loads(lines[-1])["slug"] == "new-slug"


# ---------------------------------------------------------------------------
# append_turn
# ---------------------------------------------------------------------------


class TestAppendTurn:
    def test_appends_turn_entry(self, tmp_path):
        state = tmp_path / "state"
        append_turn(SID, state)
        entry = json.loads(state.read_text().strip())
        assert entry["session_id"] == SID
        assert entry["slug"] == "__turn__"
        assert "ts" in entry

    def test_multiple_turns_accumulate(self, tmp_path):
        state = tmp_path / "state"
        append_turn(SID, state)
        append_turn(SID, state)
        append_turn(SID, state)
        assert state.read_text().count("__turn__") == 3

    def test_caps_at_500_lines(self, tmp_path):
        state = tmp_path / "state"
        state.write_text(
            "\n".join(
                json.dumps({"session_id": "old", "slug": f"s{i}"}) for i in range(500)
            )
            + "\n"
        )
        append_turn(SID, state)
        assert len(state.read_text().splitlines()) == 500


# ---------------------------------------------------------------------------
# count_turns
# ---------------------------------------------------------------------------


class TestCountTurns:
    def test_returns_zero_when_file_missing(self, tmp_path):
        assert count_turns(SID, tmp_path / "state") == 0

    def test_counts_only_turn_slugs_for_session(self, tmp_path):
        state = tmp_path / "state"
        state.write_text(
            json.dumps({"session_id": SID, "slug": "__turn__"})
            + "\n"
            + json.dumps({"session_id": SID, "slug": "__turn__"})
            + "\n"
            + json.dumps({"session_id": SID, "slug": "__index__"})
            + "\n"
            + json.dumps({"session_id": OTHER_SID, "slug": "__turn__"})
            + "\n"
        )
        assert count_turns(SID, state) == 2

    def test_skips_malformed_lines(self, tmp_path):
        state = tmp_path / "state"
        state.write_text(
            "bad-json\n" + json.dumps({"session_id": SID, "slug": "__turn__"}) + "\n"
        )
        assert count_turns(SID, state) == 1


# ---------------------------------------------------------------------------
# parse_pipe_table
# ---------------------------------------------------------------------------


class TestParsePipeTable:
    def test_parses_simple_table(self):
        text = (
            "# Title\n\n"
            "| Feature | Slug | Branch(es) |\n"
            "|---|---|---|\n"
            "| Test Feature | test-feat | feat/x |\n"
        )
        rows = parse_pipe_table(text)
        assert rows == [
            {"feature": "Test Feature", "slug": "test-feat", "branch(es)": "feat/x"}
        ]

    def test_returns_empty_list_when_no_table(self):
        assert parse_pipe_table("# Just a title\n\nSome prose, no table here.\n") == []

    def test_skips_separator_row(self):
        text = "| Slug | Branch |\n|---|---|\n| a | b |\n"
        rows = parse_pipe_table(text)
        assert len(rows) == 1

    def test_skips_row_with_fewer_cells_than_header(self):
        text = "| Slug | Branch |\n|---|---|\n| a | b |\n| only-one-cell |\n"
        rows = parse_pipe_table(text)
        assert len(rows) == 1
        assert rows[0]["slug"] == "a"

    def test_lowercases_header_names(self):
        text = "| SLUG | Branch(Es) |\n|---|---|\n| a | b |\n"
        rows = parse_pipe_table(text)
        assert set(rows[0].keys()) == {"slug", "branch(es)"}

    def test_ignores_extra_cells_beyond_header(self):
        text = "| Slug | Branch |\n|---|---|\n| a | b | c | d |\n"
        rows = parse_pipe_table(text)
        assert rows == [{"slug": "a", "branch": "b"}]


# ---------------------------------------------------------------------------
# find_slug_for_branch
# ---------------------------------------------------------------------------


class TestFindSlugForBranch:
    TABLE = (
        "# Feature Knowledge Base Index\n\n"
        "| Feature | Slug | Ticket(s) | Branch(es) | Summary | Last Updated |\n"
        "|---|---|---|---|---|---|\n"
        "| Test Feature | test-feat | | feat/test-feature | A test feature | 2026-06-27 |\n"
        "| Multi Branch | multi-feat | | feat/a, feat/b | Shares a KB | 2026-07-01 |\n"
    )

    def test_finds_matching_slug(self):
        assert find_slug_for_branch(self.TABLE, "feat/test-feature") == "test-feat"

    def test_returns_none_when_branch_not_found(self):
        assert find_slug_for_branch(self.TABLE, "feat/nonexistent") is None

    def test_handles_comma_separated_branches(self):
        assert find_slug_for_branch(self.TABLE, "feat/a") == "multi-feat"
        assert find_slug_for_branch(self.TABLE, "feat/b") == "multi-feat"

    def test_returns_none_for_empty_table(self):
        assert find_slug_for_branch("", "feat/x") is None

    def test_returns_none_when_no_slug_column(self):
        text = "| Feature | Branch(es) |\n|---|---|\n| Test | feat/x |\n"
        assert find_slug_for_branch(text, "feat/x") is None

    def test_returns_none_when_no_branch_column(self):
        text = "| Feature | Slug |\n|---|---|\n| Test | test-feat |\n"
        assert find_slug_for_branch(text, "feat/x") is None

    def test_case_insensitive_column_names(self):
        text = "| SLUG | BRANCH(ES) |\n|---|---|\n| test-feat | feat/x |\n"
        assert find_slug_for_branch(text, "feat/x") == "test-feat"


# ---------------------------------------------------------------------------
# find_malformed_rows
# ---------------------------------------------------------------------------


class TestFindMalformedRows:
    HEADER = "| Feature | Slug | Ticket(s) | Branch(es) | Summary | Last Updated |\n"
    SEP = "|---|---|---|---|---|---|\n"

    def test_no_malformed_rows_in_well_formed_table(self):
        text = (
            self.HEADER
            + self.SEP
            + (
                "| Test Feature | test-feat | | feat/test-feature | A test feature | 2026-06-27 |\n"
            )
        )
        assert find_malformed_rows(text) == []

    def test_detects_row_with_missing_cell(self):
        # Ticket(s) cell's `| |` dropped — one cell short of the header.
        bad_row = "| Test Feature | test-feat | feat/test-feature | A test feature | 2026-06-27 |"
        text = self.HEADER + self.SEP + bad_row + "\n"
        assert find_malformed_rows(text) == [bad_row]

    def test_detects_row_with_extra_cell(self):
        bad_row = "| Test Feature | test-feat | | feat/x | extra | A test feature | 2026-06-27 |"
        text = self.HEADER + self.SEP + bad_row + "\n"
        assert find_malformed_rows(text) == [bad_row]

    def test_ignores_separator_row(self):
        text = self.HEADER + self.SEP
        assert find_malformed_rows(text) == []

    def test_empty_text_has_no_malformed_rows(self):
        assert find_malformed_rows("") == []

    def test_reports_only_the_malformed_rows_not_the_good_ones(self):
        bad_row = "| Bad Feature | bad-feat | feat/bad |"
        good_row = (
            "| Good Feature | good-feat | | feat/good | A good feature | 2026-06-27 |"
        )
        text = self.HEADER + self.SEP + bad_row + "\n" + good_row + "\n"
        assert find_malformed_rows(text) == [bad_row]


# ---------------------------------------------------------------------------
# pick_active_slug
# ---------------------------------------------------------------------------


class TestPickActiveSlug:
    def test_returns_none_when_empty(self):
        assert pick_active_slug(set()) is None

    def test_returns_none_when_only_dunder_keys(self):
        assert pick_active_slug({"__index__", "__turn__"}) is None

    def test_returns_the_single_real_slug(self):
        assert pick_active_slug({"__index__", "test-feat"}) == "test-feat"

    def test_deterministic_across_repeated_calls_with_multiple_slugs(self):
        asked = {"zzz-feat", "aaa-feat", "__index__", "mmm-feat"}
        results = {pick_active_slug(asked) for _ in range(20)}
        assert results == {"aaa-feat"}, "must always pick the same slug, never flip"
