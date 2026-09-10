from __future__ import annotations

import json
from pathlib import Path

from context_proof.cli import main

EXAMPLES = Path(__file__).parents[1] / "examples"


def test_check_json_output(capsys) -> None:
    code = main(["check", str(EXAMPLES / "safe_case.json"), "--format", "json"])

    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out)
    assert payload["passed"] is True
    assert payload["summary"] == {"errors": 0, "infos": 0, "warnings": 0}


def test_check_markdown_output_and_output_file(tmp_path: Path, capsys) -> None:
    output = tmp_path / "report.md"
    code = main(
        [
            "check",
            str(EXAMPLES / "failing_case.json"),
            "--format",
            "markdown",
            "--output",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert "# Context proof: FAIL" in output.read_text(encoding="utf-8")
    assert "CP151" in output.read_text(encoding="utf-8")


def test_explain_lists_contract_codes(capsys) -> None:
    code = main(["explain"])

    output = capsys.readouterr().out
    assert code == 0
    assert "CP100" in output
    assert "CP152" in output


def test_missing_input_returns_usage_error(capsys, tmp_path: Path) -> None:
    code = main(["check", str(tmp_path / "missing.json")])

    captured = capsys.readouterr()
    assert code == 2
    assert "context-proof:" in captured.err
