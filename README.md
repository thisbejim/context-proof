# context-proof

Deterministic CI checks for the things an AI-agent context compactor must not lose.

Long-running tool-using agents eventually compact their conversation. A shorter
context is useful only if the transition keeps the instructions, goals, pending
work, approvals, and call lineage that still matter. `context-proof` lets an
agent runtime declare those invariants and checks a `before`/`after` transition
locally, without a model, provider account, or network connection.

[![CI](https://github.com/thisbejim/context-proof/actions/workflows/ci.yml/badge.svg)](https://github.com/thisbejim/context-proof/actions/workflows/ci.yml)

## Quick start

```console
$ git clone https://github.com/thisbejim/context-proof.git
$ cd context-proof
$ python -m venv .venv
$ .venv/bin/python -m pip install .
$ .venv/bin/context-proof check examples/safe_case.json
Context proof: PASS
Case: checkout-compaction-safe (profile=generic)
0 error(s), 0 warning(s), 0 info
Stats: before_items=4, after_items=2, pending_tool_calls=1, pending_approvals=1, after_estimated_tokens=102.75, max_tokens=1000
```

The intentionally broken fixture demonstrates a failing CI exit code and
actionable codes:

```console
$ .venv/bin/context-proof check examples/failing_case.json
Context proof: FAIL
Case: checkout-compaction-regression (profile=generic)
...
ERROR CP101 (after[0]): exact item 'guardrail' changed during compaction
ERROR CP110 (before[2]): pending tool call 'call-1' disappeared across compaction
```

Use `--format json` for a machine-readable artifact, `--format markdown` for a
review comment, and `--strict` when warnings should fail the job. Omitting the
path reads a phase-tagged JSONL case from stdin:

```console
cat examples/session.jsonl | .venv/bin/context-proof check --format json
```

## The contract

Cases are ordinary JSON and contain a `before` context, an `after` context, and
optional requirements and limits:

```json
{
  "schema_version": 1,
  "name": "checkout-compaction-safe",
  "before": [
    {
      "id": "guardrail",
      "kind": "instruction",
      "role": "system",
      "content": "Never send a purchase without an explicit approval.",
      "critical": "exact"
    },
    {
      "id": "call-1",
      "kind": "function_call",
      "call_id": "call-1",
      "tool": "reserve_seat",
      "arguments": {"seat": "A1"},
      "status": "pending",
      "critical": true
    }
  ],
  "after": [
    {
      "id": "guardrail",
      "kind": "instruction",
      "role": "system",
      "content": "Never send a purchase without an explicit approval.",
      "critical": "exact"
    },
    {
      "id": "summary-1",
      "kind": "compaction",
      "content": "Seat A1 is requested; reserve_seat is still pending.",
      "preserves": ["call-1"],
      "preserve_status": {"call-1": "pending"}
    }
  ],
  "limits": {"max_tokens": 1000, "reserve_tokens": 200, "chars_per_token": 4}
}
```

Every item that is load-bearing needs a stable `id`. Marking it
`"critical": true` requires presence after compaction. Marking it
`"critical": "exact"` requires the same semantic JSON payload after
compaction; a `preserves` reference alone is deliberately not enough for an
exact anchor. A compaction artifact can use `preserves` to declare lineage for
an item represented by a summary. `preserve_status` makes state such as
`pending`, `unknown`, `approved`, or `denied` explicit when the original item
is summarized. The top-level `requirements` array is an alternative for callers
that keep criticality outside their message records:

```json
"requirements": [
  {"id": "policy", "mode": "exact"},
  {"id": "user-goal", "mode": "presence"}
]
```

The normalizer understands generic `kind` records plus common OpenAI-style
shapes (`type`, `role`, `tool_calls`, `function_call`,
`function_call_output`, and `tool_call_id`). Unknown fields are retained in the
fingerprint, so a changed tool name or argument cannot silently pass as the
same call. For token accounting, supply an explicit `token_count` or
`estimated_tokens` per item when the runtime has measured values. Otherwise
the checker reports a transparent canonical-JSON character estimate; it is a
headroom signal, not a provider tokenizer.

## Checks

| Code | Contract | Default |
| --- | --- | --- |
| `CP010` | Duplicate item/call identity | error |
| `CP004` | Invalid criticality marker | error |
| `CP020` / `CP021` | Malformed or unknown lineage metadata | error |
| `CP100` | Critical item dropped | error |
| `CP101` | Exact critical item changed | error |
| `CP102` | Exact item summarized rather than retained | error |
| `CP110` | Pending tool call dropped | error |
| `CP112` | Pending call marked terminal without an observed result | error |
| `CP113` | Pending-call lineage has no declared state | warning |
| `CP120` | Pending/approved human decision dropped | error |
| `CP121` | Approval lineage has no declared decision state | warning |
| `CP130` | Result has no corresponding call in `after` | error |
| `CP140` | Call arguments or identity changed | error |
| `CP151` | Compacted context plus reserve exceeds budget | error |
| `CP152` | Compacted context is near the budget | warning |

Run `context-proof explain` for the same catalog from the installed CLI.

## Integrate at the compaction boundary

The smallest integration is to serialize the transition your runtime already
has, then fail the deployment or replay test if the report is not acceptable:

```python
from context_proof import analyze_case

report = analyze_case(
    {
        "schema_version": 1,
        "before": before_items,
        "after": compacted_items,
        "requirements": [{"id": "system-policy", "mode": "exact"}],
    }
)
if not report.passed(strict=True):
    raise AssertionError(report.to_dict(strict=True))
```

For a command-line pipeline, write the same object to a fixture and run:

```console
context-proof check compaction-case.json --format json > context-proof.json
```

The package has no runtime dependencies. It performs no HTTP requests, sends no
telemetry, imports no provider SDK, and executes no input content. This makes it
suitable for offline replay suites, pre-merge CI, and red-team fixtures that
must never expose a live conversation to another service. Reports include
paths, ids, hashes, and derived counts—not the original message content.

## Scope and honest limits

`context-proof` verifies declared identity, lineage, state, exact payloads, and
budget arithmetic. It cannot determine whether an arbitrary natural-language
summary truly captures an *unmarked* fact, and it does not replace a compactor,
tokenizer, trace backend, or model evaluation. The explicit declaration is the
point: safety-critical instructions and side-effect state become reviewable
contracts instead of assumptions hidden inside a prompt.

The design is motivated by real failure modes and platform behavior:

- [OpenAI's Responses API compaction reference](https://developers.openai.com/api/reference/java/resources/responses/methods/compact)
  exposes compaction as a distinct context item, while the [agent-loop
  write-up](https://openai.com/index/unrolling-the-codex-agent-loop/) describes
  automatic compaction as long-running loops approach their context limit.
- A [Cloudflare Agents production issue](https://github.com/cloudflare/agents/issues/1538)
  reports a run exceeding one million tokens because tool/system overhead was
  underestimated and compaction did not fire.
- An [OpenCode compaction issue](https://github.com/anomalyco/opencode/issues/43247)
  shows why a summary that leaves tool calls/results or tool availability
  ambiguous can corrupt the next turn.
- [GitHub Copilot's context-management documentation](https://docs.github.com/en/copilot/concepts/agents/copilot-cli/context-management)
  explicitly warns that compaction is lossy and reattaches original
  instructions while summarizing the conversation.

Adjacent tools such as [ctxlint](https://github.com/tqakdev/ctxlint) (static
context-file linting) and [agent-strace](https://github.com/Siddhant-K-code/agent-trace)
(broad tracing and replay) are useful complements. They do not provide this
small, provider-neutral before/after compaction contract.

## Development

```console
python -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy
.venv/bin/python -m build
```

The GitHub Actions matrix runs these checks on Python 3.10 through 3.14.

## License

MIT; see [LICENSE](LICENSE).
