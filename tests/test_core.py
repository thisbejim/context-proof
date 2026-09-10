from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from context_proof import CaseError, analyze_case, load_case

EXAMPLES = Path(__file__).parents[1] / "examples"


def _case(before: list[dict], after: list[dict], **extra: object) -> dict:
    return {"schema_version": 1, "before": before, "after": after, **extra}


def test_safe_example_passes_without_findings() -> None:
    report = analyze_case(load_case(EXAMPLES / "safe_case.json"))

    assert report.passed()
    assert report.findings == []
    assert report.stats["pending_tool_calls"] == 1
    assert report.stats["preserved_ids"] == 3


def test_jsonl_example_loads_and_passes() -> None:
    report = analyze_case(load_case(EXAMPLES / "session.jsonl"))

    assert report.name == "jsonl-session"
    assert report.passed()
    assert report.stats["before_items"] == 3


def test_jsonl_can_be_read_from_a_stream() -> None:
    text = '{"phase":"before","item":{"id":"one","kind":"message"}}\n'
    text += '{"phase":"after","item":{"id":"one","kind":"message"}}\n'

    case = load_case(io.StringIO(text))

    assert case["before"] == [{"id": "one", "kind": "message"}]
    assert case["after"] == [{"id": "one", "kind": "message"}]


def test_single_jsonl_record_is_not_mistaken_for_a_case_root() -> None:
    case = load_case(io.StringIO('{"phase":"before","item":{"id":"one"}}'))

    assert case["before"] == [{"id": "one"}]
    assert case["after"] == []


def test_failing_example_reports_multiple_independent_regressions() -> None:
    report = analyze_case(load_case(EXAMPLES / "failing_case.json"))

    assert not report.passed()
    assert {finding.code for finding in report.errors} >= {
        "CP100",
        "CP101",
        "CP110",
        "CP120",
        "CP130",
        "CP151",
    }


def test_exact_item_must_be_retained_not_just_referenced() -> None:
    case = _case(
        [
            {
                "id": "policy",
                "kind": "instruction",
                "content": "Keep approvals.",
                "critical": "exact",
            }
        ],
        [
            {
                "id": "summary",
                "kind": "compaction",
                "content": "Approvals matter.",
                "preserves": ["policy"],
            }
        ],
    )

    report = analyze_case(case)

    assert [finding.code for finding in report.errors] == ["CP102"]


def test_exact_item_cannot_change() -> None:
    case = _case(
        [
            {
                "id": "policy",
                "kind": "instruction",
                "content": "Keep approvals.",
                "critical": "exact",
            }
        ],
        [{"id": "policy", "kind": "instruction", "content": "Keep all approvals."}],
    )

    report = analyze_case(case)

    assert [finding.code for finding in report.errors] == ["CP101"]


def test_pending_call_needs_a_declared_state_when_summarized() -> None:
    case = _case(
        [{"id": "call-1", "kind": "function_call", "call_id": "call-1", "status": "pending"}],
        [{"id": "summary", "kind": "compaction", "preserves": ["call-1"]}],
    )

    report = analyze_case(case)

    assert [finding.code for finding in report.warnings] == ["CP113"]
    assert report.passed()
    assert not report.passed(strict=True)


def test_pending_call_cannot_become_terminal_without_result() -> None:
    case = _case(
        [{"id": "call-1", "kind": "function_call", "call_id": "call-1"}],
        [{"id": "call-1", "kind": "function_call", "call_id": "call-1", "status": "completed"}],
    )

    report = analyze_case(case)

    assert [finding.code for finding in report.errors] == ["CP112"]


def test_orphan_result_and_changed_call_are_errors() -> None:
    case = _case(
        [{"id": "call-1", "kind": "function_call", "call_id": "call-1", "arguments": {"x": 1}}],
        [
            {"id": "call-1", "kind": "function_call", "call_id": "call-1", "arguments": {"x": 2}},
            {"id": "result-1", "kind": "function_call_output", "call_id": "ghost", "output": "ok"},
        ],
    )

    report = analyze_case(case)

    assert {finding.code for finding in report.errors} == {"CP130", "CP140"}


def test_result_id_cannot_masquerade_as_the_missing_call() -> None:
    case = _case(
        [{"id": "call-1", "kind": "function_call", "call_id": "call-1"}],
        [{"id": "call-1", "kind": "function_call_output", "call_id": "call-1", "output": "ok"}],
    )

    report = analyze_case(case)

    assert {finding.code for finding in report.errors} == {"CP110", "CP130"}


def test_native_tool_calls_and_outputs_are_understood() -> None:
    case = _case(
        [
            {
                "id": "m1",
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call-7",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": "{}"},
                    }
                ],
            }
        ],
        [
            {
                "id": "m1",
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call-7",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call-7", "content": "{}"},
        ],
    )

    report = analyze_case(case)

    assert report.passed()
    assert report.stats["pending_tool_calls"] == 1


def test_duplicate_ids_and_unknown_preserve_references_are_errors() -> None:
    case = _case(
        [{"id": "same", "kind": "message"}, {"id": "same", "kind": "message"}],
        [{"id": "summary", "kind": "compaction", "preserves": ["missing"]}],
    )

    report = analyze_case(case)

    assert {finding.code for finding in report.errors} == {"CP010", "CP021"}


def test_invalid_criticality_and_state_metadata_are_errors() -> None:
    case = _case(
        [{"id": "policy", "kind": "instruction", "content": "Keep this.", "critical": "maybe"}],
        [
            {
                "id": "summary",
                "kind": "compaction",
                "preserves": ["policy"],
                "preserve_status": ["policy"],
            }
        ],
    )

    report = analyze_case(case)

    assert {finding.code for finding in report.errors} == {"CP004", "CP020"}


def test_budget_warning_can_be_promoted_to_failure() -> None:
    case = _case(
        [{"id": "m", "kind": "message", "content": "1234567890"}],
        [{"id": "m", "kind": "message", "content": "1234567890", "estimated_tokens": 9}],
        limits={"max_tokens": 10, "reserve_tokens": 0, "chars_per_token": 4},
    )

    report = analyze_case(case)

    assert [finding.code for finding in report.warnings] == ["CP152"]
    assert report.passed()
    assert not report.passed(strict=True)


def test_structural_errors_raise_case_error() -> None:
    with pytest.raises(CaseError, match="schema_version"):
        analyze_case({"schema_version": 2, "before": [], "after": []})

    with pytest.raises(CaseError, match="invalid JSON"):
        load_case(io.StringIO("not json"))


def test_report_serialization_is_stable_and_json_compatible() -> None:
    report = analyze_case(load_case(EXAMPLES / "safe_case.json"))

    value = report.to_dict()
    assert value["passed"] is True
    assert json.loads(json.dumps(value, sort_keys=True))["schema_version"] == 1
