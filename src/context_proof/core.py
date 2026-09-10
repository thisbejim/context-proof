"""Core loading, normalization, and deterministic compaction checks.

The checker intentionally reasons about declared identities and provenance rather than
trying to judge whether a free-form summary is semantically good.  A summary can be
excellent and still omit an exact safety instruction; conversely, a summary can mention
an item without actually proving that it retained the original value.  The input format
lets an agent runtime mark load-bearing items and attach explicit ``preserves`` metadata
to the compacted context so those decisions are inspectable in CI.
"""

from __future__ import annotations

import hashlib
import json
import sys
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

SCHEMA_VERSION = 1
DEFAULT_CHARS_PER_TOKEN = 4.0
DEFAULT_WARN_RATIO = 0.9

_META_KEYS = {
    "id",
    "item_id",
    "message_id",
    "critical",
    "preserves",
    "preserve_status",
    "preserve_hashes",
    "preserved_from",
    "phase",
    "token_count",
    "estimated_tokens",
}
_TERMINAL_STATUSES = {
    "cancelled",
    "canceled",
    "complete",
    "completed",
    "denied",
    "error",
    "failed",
    "failure",
    "ok",
    "success",
    "succeeded",
}
_PENDING_APPROVAL_STATUSES = {"", "awaiting", "pending", "requested", "waiting"}
_RESULT_KINDS = {
    "function_call_output",
    "tool_result",
    "tool_response",
    "tool_output",
    "toolresult",
}
_CALL_KINDS = {"function_call", "tool_call", "tool_use", "toolcall"}
_SUMMARY_KINDS = {"compaction", "compacted", "summary", "context_summary", "handoff"}


class CaseError(ValueError):
    """Raised when an input document cannot be interpreted as a case."""


@dataclass(frozen=True)
class Finding:
    """A deterministic, machine-readable check result."""

    code: str
    severity: str
    message: str
    path: str | None = None
    item_id: str | None = None
    evidence: tuple[str, ...] = ()
    remediation: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
        }
        if self.path is not None:
            value["path"] = self.path
        if self.item_id is not None:
            value["item_id"] = self.item_id
        if self.evidence:
            value["evidence"] = list(self.evidence)
        if self.remediation is not None:
            value["remediation"] = self.remediation
        return value


@dataclass
class Report:
    """The result of checking one context transition."""

    name: str
    profile: str
    findings: list[Finding] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def errors(self) -> list[Finding]:
        return [finding for finding in self.findings if finding.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [finding for finding in self.findings if finding.severity == "warning"]

    @property
    def infos(self) -> list[Finding]:
        return [finding for finding in self.findings if finding.severity == "info"]

    def passed(self, *, strict: bool = False) -> bool:
        if self.errors:
            return False
        return not strict or not self.warnings

    def exit_code(self, *, strict: bool = False) -> int:
        return 0 if self.passed(strict=strict) else 1

    def to_dict(self, *, strict: bool = False) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "name": self.name,
            "profile": self.profile,
            "passed": self.passed(strict=strict),
            "strict": strict,
            "summary": {
                "errors": len(self.errors),
                "warnings": len(self.warnings),
                "infos": len(self.infos),
            },
            "stats": self.stats,
            "findings": [finding.to_dict() for finding in self.findings],
        }


@dataclass(frozen=True)
class _Item:
    phase: str
    index: int
    raw: Mapping[str, Any]
    item_id: str | None
    kind: str
    role: str
    status: str
    call_ids: tuple[str, ...]
    critical_mode: str | None
    fingerprint: str
    path: str

    @property
    def is_call(self) -> bool:
        return self.kind in _CALL_KINDS or bool(self.call_ids and self.kind == "message")

    @property
    def is_result(self) -> bool:
        return self.kind in _RESULT_KINDS or self.role == "tool"

    @property
    def is_approval(self) -> bool:
        return self.kind == "approval"


def _canonical(value: Any) -> str:
    """Return stable JSON for JSON-compatible values."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    return _canonical(value)


def _lower(value: Any) -> str:
    return _as_text(value).strip().lower()


def _explicit_id(raw: Mapping[str, Any]) -> str | None:
    for key in ("id", "item_id", "message_id"):
        value = raw.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value):
            return str(value)
    return None


def _call_id_from_mapping(value: Mapping[str, Any]) -> str | None:
    for key in ("call_id", "tool_call_id", "id"):
        candidate = value.get(key)
        if isinstance(candidate, (str, int)) and not isinstance(candidate, bool) and str(candidate):
            return str(candidate)
    return None


def _kind(raw: Mapping[str, Any]) -> str:
    explicit = _lower(raw.get("kind", raw.get("type", "")))
    role = _lower(raw.get("role"))
    if explicit in _RESULT_KINDS or "function_call_output" in explicit or "tool_result" in explicit:
        return "function_call_output" if "function_call_output" in explicit else "tool_result"
    if explicit in _CALL_KINDS or "function_call" in explicit or "tool_call" in explicit:
        return "function_call" if "function_call" in explicit else "tool_call"
    if "approval" in explicit or "approval_id" in raw or "approval" in raw:
        return "approval"
    if explicit in _SUMMARY_KINDS or "compaction" in explicit or "summary" in explicit:
        return "compaction" if "compaction" in explicit else "summary"
    if role in {"system", "developer"} or explicit in {"instruction", "instructions"}:
        return "instruction"
    if role == "tool":
        return "tool_result"
    if isinstance(raw.get("tool_calls"), list) or isinstance(raw.get("function_call"), Mapping):
        return "tool_call"
    if explicit in {"message", "user", "assistant", "system", "developer", "tool"}:
        return "message"
    return explicit or "message"


def _role(raw: Mapping[str, Any]) -> str:
    value = _lower(raw.get("role"))
    return value if value else ""


def _status(raw: Mapping[str, Any]) -> str:
    for value in (
        raw.get("status"),
        raw.get("state", {}).get("status") if isinstance(raw.get("state"), Mapping) else None,
        raw.get("approval", {}).get("status") if isinstance(raw.get("approval"), Mapping) else None,
    ):
        text = _lower(value)
        if text:
            return text
    return ""


def _nested_call_ids(raw: Mapping[str, Any]) -> list[str]:
    result: list[str] = []
    candidates: list[Any] = []
    if isinstance(raw.get("tool_calls"), list):
        candidates.extend(raw["tool_calls"])
    function_call = raw.get("function_call")
    if isinstance(function_call, Mapping):
        candidates.append(function_call)
    for candidate in candidates:
        if isinstance(candidate, Mapping):
            call_id = _call_id_from_mapping(candidate)
            if call_id is not None:
                result.append(call_id)
    return result


def _call_ids(raw: Mapping[str, Any], kind: str, item_id: str | None) -> tuple[str, ...]:
    values: list[str] = []
    if kind in _CALL_KINDS or kind == "tool_call":
        # Chat Completions puts a message id beside nested ``tool_calls``.  That
        # message id is not itself a call id, so prefer explicit call_id fields
        # and nested call ids whenever they exist.
        for key in ("call_id", "tool_call_id"):
            candidate = raw.get(key)
            if (
                isinstance(candidate, (str, int))
                and not isinstance(candidate, bool)
                and str(candidate)
            ):
                values.append(str(candidate))
        values.extend(_nested_call_ids(raw))
        if not values:
            top = _call_id_from_mapping(raw)
            if top is not None:
                values.append(top)
    elif kind in _RESULT_KINDS or kind == "tool_result" or _role(raw) == "tool":
        for key in ("call_id", "tool_call_id"):
            candidate = raw.get(key)
            if (
                isinstance(candidate, (str, int))
                and not isinstance(candidate, bool)
                and str(candidate)
            ):
                values.append(str(candidate))
    if not values and item_id is not None and kind in _CALL_KINDS:
        values.append(item_id)
    return tuple(dict.fromkeys(values))


def _semantic_payload(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in raw.items() if key not in _META_KEYS}


def _critical_mode(value: Any) -> str | None:
    if value is True:
        return "presence"
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"presence", "exact"}:
            return lowered
    return None


def _critical_value_is_valid(value: Any) -> bool:
    return value is False or value is True or _critical_mode(value) is not None


def _build_item(phase: str, index: int, raw: Mapping[str, Any]) -> _Item:
    item_id = _explicit_id(raw)
    kind = _kind(raw)
    role = _role(raw)
    status = _status(raw)
    call_ids = _call_ids(raw, kind, item_id)
    return _Item(
        phase=phase,
        index=index,
        raw=raw,
        item_id=item_id,
        kind=kind,
        role=role,
        status=status,
        call_ids=call_ids,
        critical_mode=_critical_mode(raw.get("critical")),
        fingerprint=_sha256(_semantic_payload(raw)),
        path=f"{phase}[{index}]",
    )


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CaseError(f"{label} must be a JSON object")
    return value


def _require_items(value: Any, label: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        raise CaseError(f"{label} must be a JSON array")
    items: list[Mapping[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise CaseError(f"{label}[{index}] must be a JSON object")
        items.append(item)
    return items


def _parse_json(text: str, source: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise CaseError(
            f"{source}: invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc


def _parse_jsonl(text: str, source: str) -> dict[str, Any]:
    before: list[Mapping[str, Any]] = []
    after: list[Mapping[str, Any]] = []
    metadata: dict[str, Any] = {"schema_version": SCHEMA_VERSION}
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        value = _parse_json(line, f"{source}:{line_number}")
        if not isinstance(value, Mapping):
            raise CaseError(f"{source}:{line_number}: JSONL records must be objects")
        phase = _lower(value.get("phase"))
        if phase not in {"before", "after"}:
            raise CaseError(f"{source}:{line_number}: record phase must be 'before' or 'after'")
        item_value = value.get("item")
        if item_value is None:
            item = {key: item_value for key, item_value in value.items() if key != "phase"}
        else:
            if not isinstance(item_value, Mapping):
                raise CaseError(f"{source}:{line_number}: item must be a JSON object")
            item = dict(item_value)
        if phase == "before":
            before.append(item)
        else:
            after.append(item)
        for key in ("name", "profile", "limits", "requirements"):
            if key in value and key not in metadata:
                metadata[key] = value[key]
    metadata["before"] = before
    metadata["after"] = after
    return metadata


def load_case(source: str | Path | TextIO) -> dict[str, Any]:
    """Load a JSON case or phase-tagged JSONL case.

    ``source`` may be a path, ``-`` for stdin, or an already-open text stream.
    JSONL records have the shape ``{"phase": "before"|"after", ...item fields}``
    (or ``{"phase": ..., "item": {...}}``).
    """

    source_name = "<stream>"
    if hasattr(source, "read"):
        text = source.read()
    else:
        source_text = str(source)
        if source_text == "-":
            source_name = "<stdin>"
            text = sys.stdin.read()
        else:
            path = Path(source_text)
            source_name = str(path)
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise CaseError(f"{path}: {exc.strerror or str(exc)}") from exc
    if not text.strip():
        raise CaseError(f"{source_name}: input is empty")
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if first.startswith("{") and "\n" in text:
        # A regular case is always one JSON value.  If it contains more than one
        # non-empty line, try JSON first; JSONL is the fallback for a parse error.
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            value = _parse_jsonl(text, source_name)
    else:
        value = _parse_json(text, source_name)
        if (
            isinstance(value, Mapping)
            and "phase" in value
            and "before" not in value
            and "after" not in value
        ):
            value = _parse_jsonl(text, source_name)
    if not isinstance(value, Mapping):
        raise CaseError(f"{source_name}: the case root must be a JSON object")
    return dict(value)


def _preserve_refs(item: _Item) -> tuple[str, ...]:
    values = item.raw.get("preserves", [])
    if isinstance(values, str):
        return (values,)
    if not isinstance(values, list):
        return ()
    return tuple(
        str(value)
        for value in values
        if isinstance(value, (str, int)) and not isinstance(value, bool)
    )


def _preserve_status(item: _Item, item_id: str) -> str:
    raw = item.raw.get("preserve_status")
    if not isinstance(raw, Mapping):
        return ""
    return _lower(raw.get(item_id))


def _same_identity_family(source: _Item, target: _Item) -> bool:
    """Avoid treating a result or summary that stole an id as the source item."""

    return (
        source.is_call == target.is_call
        and source.is_result == target.is_result
        and source.is_approval == target.is_approval
    )


def _index_ids(items: Sequence[_Item], report: Report) -> dict[str, _Item]:
    result: dict[str, _Item] = {}
    for item in items:
        # A result's ``call_id`` is a reference, not a second definition of the
        # call.  Index call ids only on call-bearing items so a normal call/result
        # pair does not look like a duplicate identity.
        ids = list(item.call_ids) if item.is_call else []
        if item.item_id is not None:
            ids.insert(0, item.item_id)
        for identity in dict.fromkeys(ids):
            previous = result.get(identity)
            if previous is not None:
                report.findings.append(
                    Finding(
                        code="CP010",
                        severity="error",
                        message=f"duplicate identity '{identity}' appears in {previous.path} and {item.path}",
                        path=item.path,
                        item_id=identity,
                        evidence=(previous.path, item.path),
                        remediation="Assign each message, tool call, and result a stable unique id.",
                    )
                )
            else:
                result[identity] = item
    return result


def _findings_for_requirements(
    case: Mapping[str, Any],
    before: Sequence[_Item],
    after: Sequence[_Item],
    before_index: Mapping[str, _Item],
    after_index: Mapping[str, _Item],
    preserved: set[str],
    report: Report,
) -> None:
    requirements: list[tuple[str, str, str]] = []
    for item in before:
        if item.critical_mode is not None:
            if item.item_id is None:
                report.findings.append(
                    Finding(
                        code="CP001",
                        severity="error",
                        message=f"critical item at {item.path} has no stable id",
                        path=item.path,
                        evidence=("critical items need an id so compaction can prove lineage",),
                        remediation="Add an explicit id to every item marked critical.",
                    )
                )
            else:
                requirements.append((item.item_id, item.critical_mode, item.path))
    raw_requirements = case.get("requirements", [])
    if raw_requirements is None:
        raw_requirements = []
    if not isinstance(raw_requirements, list):
        report.findings.append(
            Finding(
                code="CP002",
                severity="error",
                message="requirements must be a JSON array",
                path="requirements",
                remediation='Use [{"id": "...", "mode": "presence"|"exact"}].',
            )
        )
    else:
        for index, requirement in enumerate(raw_requirements):
            if not isinstance(requirement, Mapping):
                report.findings.append(
                    Finding(
                        code="CP002",
                        severity="error",
                        message=f"requirements[{index}] must be an object",
                        path=f"requirements[{index}]",
                    )
                )
                continue
            identity = requirement.get("id")
            mode = _critical_mode(requirement.get("mode", "presence"))
            if (
                not isinstance(identity, (str, int))
                or isinstance(identity, bool)
                or not str(identity)
            ):
                report.findings.append(
                    Finding(
                        code="CP002",
                        severity="error",
                        message=f"requirements[{index}] needs a non-empty id",
                        path=f"requirements[{index}]",
                    )
                )
                continue
            if mode is None:
                report.findings.append(
                    Finding(
                        code="CP002",
                        severity="error",
                        message=f"requirements[{index}] mode must be 'presence' or 'exact'",
                        path=f"requirements[{index}]",
                    )
                )
                continue
            requirements.append((str(identity), mode, f"requirements[{index}]"))

    for identity, mode, source_path in dict.fromkeys(requirements):
        source = before_index.get(identity)
        if source is None:
            report.findings.append(
                Finding(
                    code="CP003",
                    severity="error",
                    message=f"required item '{identity}' is not present in before context",
                    path=source_path,
                    item_id=identity,
                    remediation="Point the requirement at an id in before, or remove the stale requirement.",
                )
            )
            continue
        target = after_index.get(identity)
        retained = target is not None and _same_identity_family(source, target)
        referenced = identity in preserved
        if not retained and not referenced:
            report.findings.append(
                Finding(
                    code="CP100",
                    severity="error",
                    message=f"critical item '{identity}' was dropped by compaction",
                    path=source.path,
                    item_id=identity,
                    evidence=(
                        f"before: {source.path}",
                        "after: no matching id or preserves reference",
                    ),
                    remediation="Retain the item verbatim or add an explicit preserves reference in the compacted artifact.",
                )
            )
        elif mode == "exact":
            if referenced and not retained:
                report.findings.append(
                    Finding(
                        code="CP102",
                        severity="error",
                        message=f"exact item '{identity}' was summarized instead of retained verbatim",
                        path=source.path,
                        item_id=identity,
                        evidence=(
                            "mode: exact",
                            "lineage reference is not proof of byte-level preservation",
                        ),
                        remediation="Carry the exact item into after, or downgrade the requirement to presence.",
                    )
                )
            elif retained:
                target = after_index[identity]
                if source.fingerprint != target.fingerprint:
                    report.findings.append(
                        Finding(
                            code="CP101",
                            severity="error",
                            message=f"exact item '{identity}' changed during compaction",
                            path=target.path,
                            item_id=identity,
                            evidence=(
                                f"before sha256: {source.fingerprint}",
                                f"after sha256: {target.fingerprint}",
                            ),
                            remediation="Keep safety instructions, schemas, and other exact anchors unchanged.",
                        )
                    )


def _pending_calls(before: Sequence[_Item]) -> dict[str, _Item]:
    calls: dict[str, _Item] = {}
    results: set[str] = set()
    for item in before:
        if item.is_result:
            results.update(item.call_ids)
        if item.is_call:
            for call_id in item.call_ids:
                calls.setdefault(call_id, item)
    return {call_id: item for call_id, item in calls.items() if call_id not in results}


def _pending_approvals(before: Sequence[_Item]) -> list[_Item]:
    result: list[_Item] = []
    for item in before:
        if not item.is_approval:
            continue
        if (
            item.status in {"approved", "authorised", "authorized"}
            or item.status in _PENDING_APPROVAL_STATUSES
        ):
            result.append(item)
    return result


def _check_pending_state(
    before: Sequence[_Item],
    after: Sequence[_Item],
    after_index: Mapping[str, _Item],
    preserved: set[str],
    report: Report,
) -> None:
    for call_id, source in _pending_calls(before).items():
        target = after_index.get(call_id)
        if target is not None and not target.is_call:
            target = None
        if target is None and call_id not in preserved:
            report.findings.append(
                Finding(
                    code="CP110",
                    severity="error",
                    message=f"pending tool call '{call_id}' disappeared across compaction",
                    path=source.path,
                    item_id=call_id,
                    evidence=(
                        "before: call has no matching result",
                        "after: no call or preserves reference",
                    ),
                    remediation="Preserve the pending call and its id, or record an explicit unknown/settled state before compacting.",
                )
            )
        elif target is not None and target.status in _TERMINAL_STATUSES:
            report.findings.append(
                Finding(
                    code="CP112",
                    severity="error",
                    message=f"pending tool call '{call_id}' is marked '{target.status}' without a result",
                    path=target.path,
                    item_id=call_id,
                    evidence=("before: unresolved call", f"after: status={target.status}"),
                    remediation="Keep the call pending/unknown until an observed result is attached.",
                )
            )
        elif target is None:
            state = next(
                (
                    _preserve_status(item, call_id)
                    for item in after
                    if call_id in _preserve_refs(item)
                ),
                "",
            )
            if not state:
                report.findings.append(
                    Finding(
                        code="CP113",
                        severity="warning",
                        message=f"pending tool call '{call_id}' has lineage but no preserved state",
                        path=source.path,
                        item_id=call_id,
                        evidence=(
                            "preserves proves identity only",
                            "pending vs unknown cannot be inferred from prose",
                        ),
                        remediation=f"Add preserve_status.{call_id} = pending|unknown to the compacted item.",
                    )
                )

    for source in _pending_approvals(before):
        identity = source.item_id
        if identity is None:
            continue
        target = after_index.get(identity)
        if target is None and identity not in preserved:
            report.findings.append(
                Finding(
                    code="CP120",
                    severity="error",
                    message=f"approval '{identity}' disappeared across compaction",
                    path=source.path,
                    item_id=identity,
                    evidence=(
                        f"before status: {source.status or 'pending'}",
                        "after: no matching id or preserves reference",
                    ),
                    remediation="Carry the approval state and decision-bound call into after.",
                )
            )
        elif target is None:
            state = next(
                (
                    _preserve_status(item, identity)
                    for item in after
                    if identity in _preserve_refs(item)
                ),
                "",
            )
            if not state:
                report.findings.append(
                    Finding(
                        code="CP121",
                        severity="warning",
                        message=f"approval '{identity}' has lineage but no preserved decision state",
                        path=source.path,
                        item_id=identity,
                        remediation=f"Add preserve_status.{identity} = pending|approved|denied to the compacted item.",
                    )
                )


def _check_orphan_results(
    after: Sequence[_Item], after_index: Mapping[str, _Item], preserved: set[str], report: Report
) -> None:
    for item in after:
        if not item.is_result:
            continue
        for call_id in item.call_ids:
            target = after_index.get(call_id)
            if (target is None or not target.is_call) and call_id not in preserved:
                report.findings.append(
                    Finding(
                        code="CP130",
                        severity="error",
                        message=f"tool result at {item.path} has no call '{call_id}' in after context",
                        path=item.path,
                        item_id=call_id,
                        evidence=("after result is present", "after call identity is absent"),
                        remediation="Keep the corresponding call, or omit the result when its call was compacted away.",
                    )
                )


def _check_conflicting_calls(
    before_index: Mapping[str, _Item], after: Sequence[_Item], report: Report
) -> None:
    def call_fingerprint(item: _Item) -> str:
        # Status is observed state, not call identity or arguments.  A pending
        # call may legitimately become completed once a result exists; CP112
        # separately rejects a terminal state with no such result.
        payload = {
            key: value
            for key, value in _semantic_payload(item.raw).items()
            if key not in {"status", "state"}
        }
        return _sha256(payload)

    for item in after:
        if not item.is_call:
            continue
        for call_id in item.call_ids:
            source = before_index.get(call_id)
            if source is not None and call_fingerprint(source) != call_fingerprint(item):
                report.findings.append(
                    Finding(
                        code="CP140",
                        severity="error",
                        message=f"tool call '{call_id}' changed arguments or identity across compaction",
                        path=item.path,
                        item_id=call_id,
                        evidence=(
                            f"before sha256: {source.fingerprint}",
                            f"after sha256: {item.fingerprint}",
                        ),
                        remediation="Preserve the original call payload; a changed call needs a new id and fresh approval.",
                    )
                )


def _estimate_tokens(items: Sequence[_Item], chars_per_token: float) -> float:
    total = 0.0
    for item in items:
        explicit = item.raw.get("token_count", item.raw.get("estimated_tokens"))
        if isinstance(explicit, (int, float)) and not isinstance(explicit, bool) and explicit >= 0:
            total += float(explicit)
        else:
            total += len(_canonical(item.raw)) / chars_per_token
    return total


def _check_budget(
    case: Mapping[str, Any], before: Sequence[_Item], after: Sequence[_Item], report: Report
) -> None:
    raw_limits = case.get("limits", {})
    if raw_limits is None:
        raw_limits = {}
    if not isinstance(raw_limits, Mapping):
        report.findings.append(
            Finding(
                code="CP150",
                severity="error",
                message="limits must be a JSON object",
                path="limits",
            )
        )
        return
    max_tokens = raw_limits.get("max_tokens")
    if max_tokens is None:
        return
    if not isinstance(max_tokens, (int, float)) or isinstance(max_tokens, bool) or max_tokens <= 0:
        report.findings.append(
            Finding(
                code="CP150",
                severity="error",
                message="limits.max_tokens must be a positive number",
                path="limits.max_tokens",
            )
        )
        return
    chars_per_token = raw_limits.get("chars_per_token", DEFAULT_CHARS_PER_TOKEN)
    if (
        not isinstance(chars_per_token, (int, float))
        or isinstance(chars_per_token, bool)
        or chars_per_token <= 0
    ):
        report.findings.append(
            Finding(
                code="CP150",
                severity="error",
                message="limits.chars_per_token must be a positive number",
                path="limits.chars_per_token",
            )
        )
        return
    reserve = raw_limits.get("reserve_tokens", 0)
    if not isinstance(reserve, (int, float)) or isinstance(reserve, bool) or reserve < 0:
        report.findings.append(
            Finding(
                code="CP150",
                severity="error",
                message="limits.reserve_tokens must be a non-negative number",
                path="limits.reserve_tokens",
            )
        )
        return
    before_tokens = _estimate_tokens(before, float(chars_per_token))
    after_tokens = _estimate_tokens(after, float(chars_per_token))
    effective = after_tokens + float(reserve)
    ratio = effective / float(max_tokens)
    report.stats.update(
        {
            "before_estimated_tokens": round(before_tokens, 2),
            "after_estimated_tokens": round(after_tokens, 2),
            "reserved_tokens": reserve,
            "effective_after_tokens": round(effective, 2),
            "max_tokens": max_tokens,
            "budget_ratio": round(ratio, 4),
            "token_estimator": f"canonical-json chars/{chars_per_token:g}",
        }
    )
    if effective > float(max_tokens):
        report.findings.append(
            Finding(
                code="CP151",
                severity="error",
                message=f"compacted context exceeds budget ({effective:.0f} > {float(max_tokens):.0f} tokens including reserve)",
                path="after",
                evidence=(
                    f"estimated after: {after_tokens:.0f}",
                    f"reserved: {float(reserve):.0f}",
                ),
                remediation="Compact earlier, bound tool output, or increase the model budget with measured headroom.",
            )
        )
    warn_ratio = raw_limits.get("warn_at", DEFAULT_WARN_RATIO)
    if (
        isinstance(warn_ratio, (int, float))
        and not isinstance(warn_ratio, bool)
        and 0 < warn_ratio < 1
        and warn_ratio <= ratio <= 1
    ):
        report.findings.append(
            Finding(
                code="CP152",
                severity="warning",
                message=f"compacted context uses {ratio:.0%} of the available budget",
                path="after",
                evidence=(
                    f"effective tokens: {effective:.0f}",
                    f"max tokens: {float(max_tokens):.0f}",
                ),
                remediation="Leave room for the next user turn, tool results, and model output.",
            )
        )


def analyze_case(case: Mapping[str, Any]) -> Report:
    """Check one case and return a deterministic report.

    Structural input errors raise :class:`CaseError`; contract failures are returned as
    findings so callers can render a complete report and still use a meaningful exit code.
    """

    if not isinstance(case, Mapping):
        raise CaseError("case root must be a JSON object")
    version = case.get("schema_version")
    if version != SCHEMA_VERSION:
        raise CaseError(f"schema_version must be {SCHEMA_VERSION}")
    before_raw = _require_items(case.get("before"), "before")
    after_raw = _require_items(case.get("after"), "after")
    name_value = case.get("name", "unnamed")
    name = str(name_value) if isinstance(name_value, (str, int, float)) else "unnamed"
    profile_value = case.get("profile", "generic")
    profile = str(profile_value) if isinstance(profile_value, str) else "generic"
    report = Report(name=name, profile=profile)
    before = [_build_item("before", index, item) for index, item in enumerate(before_raw)]
    after = [_build_item("after", index, item) for index, item in enumerate(after_raw)]
    for item in before:
        if "critical" in item.raw and not _critical_value_is_valid(item.raw.get("critical")):
            report.findings.append(
                Finding(
                    code="CP004",
                    severity="error",
                    message=f"{item.path}.critical must be false, true, 'presence', or 'exact'",
                    path=f"{item.path}.critical",
                    item_id=item.item_id,
                    remediation="Use critical: true for presence or critical: exact for byte-stable anchors.",
                )
            )
    before_index = _index_ids(before, report)
    after_index = _index_ids(after, report)
    preserved: set[str] = set()
    for item in after:
        refs = _preserve_refs(item)
        if item.raw.get("preserves") is not None and not isinstance(
            item.raw.get("preserves"), (list, str)
        ):
            report.findings.append(
                Finding(
                    code="CP020",
                    severity="error",
                    message=f"{item.path}.preserves must be a string or array of ids",
                    path=f"{item.path}.preserves",
                    remediation='Use an array such as ["goal", "approval-1"].',
                )
            )
        preserve_status = item.raw.get("preserve_status")
        if preserve_status is not None and not isinstance(preserve_status, Mapping):
            report.findings.append(
                Finding(
                    code="CP020",
                    severity="error",
                    message=f"{item.path}.preserve_status must be a JSON object",
                    path=f"{item.path}.preserve_status",
                    remediation="Use preserve_status.{id} = pending|unknown|approved|denied.",
                )
            )
        elif isinstance(preserve_status, Mapping):
            for identity in preserve_status:
                if str(identity) not in before_index:
                    report.findings.append(
                        Finding(
                            code="CP021",
                            severity="error",
                            message=f"{item.path}.preserve_status references unknown id '{identity}'",
                            path=f"{item.path}.preserve_status",
                            item_id=str(identity),
                            remediation="Declare state only for an id that exists in before.",
                        )
                    )
        for identity in refs:
            preserved.add(identity)
            if identity not in before_index:
                report.findings.append(
                    Finding(
                        code="CP021",
                        severity="error",
                        message=f"{item.path} preserves unknown id '{identity}'",
                        path=item.path,
                        item_id=identity,
                        remediation="Preserve an id that exists in before, or remove the stale reference.",
                    )
                )
    _findings_for_requirements(case, before, after, before_index, after_index, preserved, report)
    _check_pending_state(before, after, after_index, preserved, report)
    _check_orphan_results(after, after_index, preserved, report)
    _check_conflicting_calls(before_index, after, report)
    _check_budget(case, before, after, report)
    report.stats.update(
        {
            "before_items": len(before),
            "after_items": len(after),
            "critical_items": sum(item.critical_mode is not None for item in before),
            "pending_tool_calls": len(_pending_calls(before)),
            "pending_approvals": len(_pending_approvals(before)),
            "preserved_ids": len(preserved),
        }
    )
    return report


def render_text(report: Report, *, strict: bool = False) -> str:
    """Render a concise terminal report."""

    status = "PASS" if report.passed(strict=strict) else "FAIL"
    lines = [f"Context proof: {status}", f"Case: {report.name} (profile={report.profile})"]
    summary = f"{len(report.errors)} error(s), {len(report.warnings)} warning(s), {len(report.infos)} info"
    if strict and report.warnings and not report.errors:
        summary += " [strict mode fails warnings]"
    lines.append(summary)
    if report.stats:
        stat_order = (
            "before_items",
            "after_items",
            "pending_tool_calls",
            "pending_approvals",
            "after_estimated_tokens",
            "max_tokens",
        )
        compact_stats = [f"{key}={report.stats[key]}" for key in stat_order if key in report.stats]
        if compact_stats:
            lines.append("Stats: " + ", ".join(compact_stats))
    for finding in report.findings:
        location = f" ({finding.path})" if finding.path else ""
        lines.append(f"{finding.severity.upper()} {finding.code}{location}: {finding.message}")
        for evidence in finding.evidence:
            lines.append(f"  evidence: {evidence}")
        if finding.remediation:
            lines.append(f"  fix: {finding.remediation}")
    return "\n".join(lines)


def render_markdown(report: Report, *, strict: bool = False) -> str:
    """Render a stable Markdown report suitable for a CI artifact."""

    status = "PASS" if report.passed(strict=strict) else "FAIL"
    lines = [
        f"# Context proof: {status}",
        "",
        f"- Case: `{report.name}`",
        f"- Profile: `{report.profile}`",
        f"- Errors: **{len(report.errors)}**",
        f"- Warnings: **{len(report.warnings)}**",
        f"- Infos: **{len(report.infos)}**",
    ]
    if report.stats:
        lines.extend(["", "## Stats", ""])
        for key, value in report.stats.items():
            lines.append(f"- `{key}`: `{value}`")
    lines.extend(["", "## Findings", ""])
    if not report.findings:
        lines.append("No findings.")
    else:
        for finding in report.findings:
            location = f" at `{finding.path}`" if finding.path else ""
            lines.append(
                f"- **{finding.severity.upper()} `{finding.code}`**{location}: {finding.message}"
            )
            if finding.evidence:
                lines.append("  - Evidence: " + "; ".join(finding.evidence))
            if finding.remediation:
                lines.append("  - Fix: " + finding.remediation)
    return "\n".join(lines)


def explain_checks() -> str:
    """Return the supported check catalog."""

    return "\n".join(
        [
            "CP004  invalid criticality marker (error)",
            "CP100  critical item dropped (error)",
            "CP101  exact critical item changed (error)",
            "CP102  exact item summarized instead of retained (error)",
            "CP110  pending tool call dropped (error)",
            "CP112  pending call marked terminal without a result (error)",
            "CP113  pending call lineage lacks a declared state (warning)",
            "CP120  pending/approved human decision dropped (error)",
            "CP121  approval lineage lacks a declared state (warning)",
            "CP130  tool result has no call in after context (error)",
            "CP140  tool call arguments changed across compaction (error)",
            "CP151  compacted context exceeds the declared budget (error)",
            "CP152  compacted context is near the declared budget (warning)",
            "CP010/020/021  malformed or duplicate identity/lineage metadata (error)",
        ]
    )


__all__ = [
    "CaseError",
    "Finding",
    "Report",
    "SCHEMA_VERSION",
    "analyze_case",
    "explain_checks",
    "load_case",
    "render_markdown",
    "render_text",
]
