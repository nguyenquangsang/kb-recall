"""Unit tests for the smoke-test scorer.

Only the scorer is tested here, never the model. Model behaviour is
non-deterministic and can only be reported as a rate over trials — putting it
in pytest would produce a flaky assertion that teaches people to ignore the
suite. The scoring function, by contrast, is ordinary deterministic code and
is exactly the kind of thing that should break loudly.
"""

from scripts.smoke.harness import (
    called_tools,
    load_usage_file,
    records_since,
    usage_offset,
)
from scripts.smoke.score import is_subsequence, score_run


def usage(*tool_names):
    """Build usage.jsonl-shaped records for the given tool names, in order."""
    return [{"tool": name, "status": "ok"} for name in tool_names]


class TestCalledTools:
    """The log holds slash-command records alongside tool calls; those carry a
    `command` key and no `tool`. They are not calls and must not be counted."""

    def test_skips_records_without_a_tool_name(self):
        records = [
            {"tool": "save_memory"},
            {"command": "recall:save", "slug": "x"},
            {"tool": "load_feature_context"},
        ]
        assert called_tools(records) == ["save_memory", "load_feature_context"]

    def test_slash_command_records_do_not_inflate_min_calls(self):
        records = [{"command": "recall:save"}, {"tool": "list_features"}]
        passed, reasons = score_run(records, {"min_calls": 2})
        assert not passed
        assert "only 1 tool call(s), expected >= 2" in reasons


class TestLoadUsageFile:
    """The loader is the seam that silently broke once: the CLI passes a
    usage.jsonl *file*, while `load_usage` wants a *HOME directory* and appends
    the path itself. Against the wrong one it returned [] — which reads as a
    legitimate "this run made no calls" rather than as a bug, so it scored every
    real run as a failure until the CLI was run by hand."""

    def test_reads_records_in_order(self, tmp_path):
        log = tmp_path / "usage.jsonl"
        log.write_text('{"tool": "a"}\n{"tool": "b"}\n', encoding="utf-8")
        assert called_tools(load_usage_file(log)) == ["a", "b"]

    def test_skips_blank_and_malformed_lines(self, tmp_path):
        log = tmp_path / "usage.jsonl"
        log.write_text('{"tool": "a"}\n\nnot json\n{"tool": "b"}\n', encoding="utf-8")
        assert called_tools(load_usage_file(log)) == ["a", "b"]

    def test_missing_file_is_empty_not_an_error(self, tmp_path):
        assert load_usage_file(tmp_path / "nope.jsonl") == []

    def test_accepts_a_string_path(self, tmp_path):
        """argparse hands over str, not Path — that mismatch was a real bug."""
        log = tmp_path / "usage.jsonl"
        log.write_text('{"tool": "a"}\n', encoding="utf-8")
        assert called_tools(load_usage_file(str(log))) == ["a"]


class TestUsageOffset:
    """The offset/records pair is how the Copilot probe scores a run: it marks
    the log's end before, and reads only what the run appended. The log is
    shared by every client on the machine, so "the last N lines" would straddle
    someone else's session — the reason this is a byte offset and not a count."""

    def test_missing_file_has_offset_zero(self, tmp_path):
        assert usage_offset(tmp_path / "nope.jsonl") == 0

    def test_offset_is_the_current_end(self, tmp_path):
        log = tmp_path / "usage.jsonl"
        log.write_text('{"tool": "a"}\n', encoding="utf-8")
        assert usage_offset(log) == log.stat().st_size

    def test_records_since_returns_only_the_new_tail(self, tmp_path):
        log = tmp_path / "usage.jsonl"
        log.write_text('{"tool": "old"}\n', encoding="utf-8")
        offset = usage_offset(log)
        with log.open("a", encoding="utf-8") as handle:
            handle.write('{"tool": "new"}\n')
        assert called_tools(records_since(log, offset)) == ["new"]

    def test_records_since_zero_reads_everything(self, tmp_path):
        log = tmp_path / "usage.jsonl"
        log.write_text('{"tool": "a"}\n{"tool": "b"}\n', encoding="utf-8")
        assert called_tools(records_since(log, 0)) == ["a", "b"]

    def test_nothing_appended_yields_nothing(self, tmp_path):
        log = tmp_path / "usage.jsonl"
        log.write_text('{"tool": "a"}\n', encoding="utf-8")
        assert records_since(log, usage_offset(log)) == []

    def test_malformed_tail_lines_are_skipped(self, tmp_path):
        log = tmp_path / "usage.jsonl"
        offset = usage_offset(log)
        with log.open("a", encoding="utf-8") as handle:
            handle.write('{"tool": "a"}\n\nnot json\n')
        assert called_tools(records_since(log, offset)) == ["a"]


class TestIsSubsequence:
    def test_exact_match(self):
        assert is_subsequence(["a", "b"], ["a", "b"])

    def test_gaps_are_allowed(self):
        assert is_subsequence(["a", "c"], ["a", "b", "c"])

    def test_order_is_enforced(self):
        assert not is_subsequence(["b", "a"], ["a", "b"])

    def test_missing_element_fails(self):
        assert not is_subsequence(["a", "z"], ["a", "b"])

    def test_empty_needle_matches_anything(self):
        assert is_subsequence([], ["a"])


class TestScoreRun:
    def test_passes_when_required_call_is_present(self):
        passed, reasons = score_run(
            usage("load_feature_context"), {"requires": [["load_feature_context"]]}
        )
        assert passed
        assert reasons == []

    def test_fails_when_required_call_is_absent(self):
        """The case usage.jsonl cannot express on its own: a call that should
        have happened and did not leaves no record, so silence must score as a
        failure rather than being skipped."""
        passed, reasons = score_run(usage(), {"requires": [["load_feature_context"]]})
        assert not passed
        assert "missing sequence: load_feature_context" in reasons

    def test_extra_calls_do_not_fail_the_run(self):
        passed, _ = score_run(
            usage("list_features", "load_feature_context", "search_features"),
            {"requires": [["load_feature_context"]]},
        )
        assert passed

    def test_forbidden_tool_fails_the_run(self):
        passed, reasons = score_run(
            usage("list_features", "search_features", "init_feature"),
            {
                "requires": [["list_features", "init_feature"]],
                "forbids": ["search_features"],
            },
        )
        assert not passed
        assert "called a forbidden tool: search_features" in reasons

    def test_multi_step_sequence_must_be_ordered(self):
        scenario = {"requires": [["list_features", "init_feature"]]}
        assert score_run(usage("list_features", "init_feature"), scenario)[0]
        assert not score_run(usage("init_feature", "list_features"), scenario)[0]

    def test_min_calls_is_enforced(self):
        passed, reasons = score_run(usage("list_features"), {"min_calls": 2})
        assert not passed
        assert "only 1 tool call(s), expected >= 2" in reasons

    def test_reasons_accumulate_across_all_violations(self):
        passed, reasons = score_run(
            usage("search_features"),
            {"requires": [["load_feature_context"]], "forbids": ["search_features"]},
        )
        assert not passed
        assert len(reasons) == 2

    def test_scenario_with_no_constraints_passes(self):
        assert score_run(usage(), {})[0]
