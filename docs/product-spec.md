# Product specification: context-proof

Status: v0.1.0, open-source release candidate

Audience: developers building long-running, tool-using AI agents and the CI
systems that replay them

## Decision

Build a small, dependency-free verifier for the transition from an agent's
pre-compaction context to its post-compaction context. The verifier consumes a
fixture or runtime-produced case, checks explicit invariants, and emits a
deterministic text, JSON, or Markdown report with a non-zero exit code for
contract failures.

This is not another summarizer. It is a proof boundary around a lossy state
transition.

## Problem and public evidence

Agent frameworks increasingly compact long conversations, but the dangerous
failures are state failures rather than ordinary prompt-quality failures:

1. [OpenAI's Responses API compaction reference](https://developers.openai.com/api/reference/java/resources/responses/methods/compact)
   makes compaction a first-class context item. The [Codex agent-loop
   explanation](https://openai.com/index/unrolling-the-codex-agent-loop/)
   describes automatic compaction as a response to context-window pressure.
2. [Cloudflare Agents issue #1538](https://github.com/cloudflare/agents/issues/1538)
   reports a production run at 1,058,839 tokens where compaction fired zero
   times because system prompts and tools were missing from the estimator and
   the trigger ran too late.
3. [OpenCode issue #43247](https://github.com/anomalyco/opencode/issues/43247)
   reports a compaction prompt not being delivered while the history still
   contained tool calls/results and tool availability was ambiguous.
4. [GitHub Copilot's context-management documentation](https://docs.github.com/en/copilot/concepts/agents/copilot-cli/context-management)
   says long sessions exceed the context window, compaction snapshots the
   conversation into a structured summary, original instructions are
   reattached, and meaningful detail is inevitably lost.

The common engineering need is a reviewable statement of what *must* survive:
exact safety instructions, user goals, unresolved calls, human approvals,
call/result lineage, and enough budget for the next turn.

## Gap and positioning

The adjacent ecosystem has strong pieces but no focused contract at this
boundary:

- [ctxlint](https://github.com/tqakdev/ctxlint) statically profiles context
  files; it does not compare a runtime's before/after compaction artifacts.
- [agent-strace](https://github.com/Siddhant-K-code/agent-trace) is a broad
  trace/replay/monitoring surface; it is not a compact invariant checker that
  can gate a fixture in CI.
- Native provider compaction and framework dashboards perform or observe the
  transition, but they do not give a provider-neutral, offline contract that a
  team can inspect before adopting a runtime upgrade.

`context-proof` deliberately stays narrower than those projects: it validates
declared identities and state, not model-generated meaning. That boundary is
both the differentiation and the honest limit.

## Candidate selection

Scores are 1 (weak) to 5 (strong), judged against the goal of a high-value,
independent, offline tool for frontier-AI developers. A hard gate required at
least 4 for severity, differentiation, independence, and developer experience,
plus a credible build in one focused release.

| Candidate | Severity | Differentiation | Offline | Buildability | DX | Total | Decision |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Prompt-cache diagnostics | 4 | 2 | 5 | 4 | 4 | 19 | Reject: crowded analyzers and cache dashboards |
| OpenAI-compatible conformance probe | 4 | 2 | 4 | 3 | 4 | 17 | Reject: several compatibility testers already cover the path |
| LLM failure-injection harness | 5 | 2 | 3 | 3 | 3 | 16 | Reject: overlaps mock/fuzz/crash-test frameworks |
| Context budget linter | 4 | 3 | 5 | 5 | 4 | 21 | Reject: useful but stops before the lossy transition |
| Tool side-effect/idempotency ledger | 5 | 2 | 3 | 2 | 3 | 15 | Reject: broad ledgers and trace tools already exist |
| **Compaction transition verifier** | **5** | **5** | **5** | **4** | **5** | **24** | **Build** |

The selected idea clears the gate because its input/output contract is small,
its failure modes are consequential, and it complements rather than replaces
provider compaction, tracing, and evaluation systems.

## Users and jobs to be done

- An agent-runtime maintainer wants a regression fixture that fails if a context
  reducer drops a system policy or changes a pending function call.
- An application team wants to pin a compaction adapter to an explicit contract
  before upgrading a framework or model.
- A red-team or reliability engineer wants offline fixtures for orphaned tool
  results, lost approvals, stale call arguments, and budget overflow.
- A reviewer wants a short Markdown artifact that explains exactly what changed
  and how to repair it.

## v0.1 contract

### Input

The root object has `schema_version: 1`, `before`, and `after` arrays. Every
load-bearing item has an explicit stable `id`. Common generic and provider-like
fields are normalized:

- message/instruction records (`kind`, `type`, `role`, `content`);
- tool calls (`function_call`, `tool_call`, nested `tool_calls`, `call_id`);
- results (`function_call_output`, `tool_result`, `tool_call_id`, `role: tool`);
- approvals (`kind: approval`, `approval_id`, `status`);
- summaries (`compaction`, `summary`, `preserves`, `preserve_status`).

Unknown fields remain part of an item's canonical semantic fingerprint. The
checker therefore does not silently ignore an argument or tool name that an
adapter added later.

### Invariants

- `critical: true` means the item must remain present or be explicitly
  referenced by `preserves`.
- `critical: "exact"` means the canonical semantic payload must remain
  unchanged in `after`; a lineage reference alone is rejected.
- Invalid criticality markers, duplicate identities, malformed lineage, and
  unknown preserved ids are errors.
- Pending calls cannot disappear, become terminal without an observed result, or
  be summarized without an explicit `preserve_status` (warning).
- Pending or approved human decisions follow the same lineage/state rule.
- A result cannot outlive its call, and a call's arguments cannot change under
  the same id.
- A declared `max_tokens` budget includes `reserve_tokens`; explicit per-item
  counts win over a documented canonical-JSON estimate.

### Outputs

`Report` is a pure Python value with `findings`, derived statistics, pass/fail
helpers, and JSON serialization. The CLI returns 0 on pass, 1 on a contract
failure (or strict-mode warning), and 2 for malformed input or I/O errors.

## Independence and safety requirements

- Python standard library only at runtime; no provider SDK or mandatory API key.
- No network, telemetry, model call, code execution, or account signup.
- Deterministic canonical JSON and SHA-256 fingerprints for exact checks.
- Findings expose paths, ids, hashes, and counts rather than message contents.
- Fixtures must contain synthetic data; a caller controls any retention or
  redaction policy before writing a case to disk.
- The tool is a verifier, not a semantic judge. Documentation must never imply
  that `preserves` proves a free-form summary contains every fact.

## Developer experience

The happy path is one command:

```console
context-proof check compaction-case.json
```

The CI path is equally small:

```console
context-proof check compaction-case.json --format json > context-proof.json
```

`context-proof explain` keeps the error catalog discoverable. JSONL input makes
it possible to tee a streaming runtime log into a fixture without first
building a second bespoke exporter. The Python API supports in-process replay
tests where spawning a subprocess would obscure the assertion.

## Validation and release acceptance

Before publication, the project must satisfy all of the following:

- fixtures cover safe transitions and each high-impact failure family;
- unit and CLI tests pass on the local supported interpreter;
- Ruff check/format, strict mypy, and wheel build pass;
- GitHub Actions tests Python 3.10–3.14 without credentials;
- README quick start works from a fresh clone and a fresh public-clone copy;
- secret scan finds no credential-shaped strings;
- public GitHub repository and a versioned release are visible after push.

## Skeptic review

The most serious objection is false confidence: a caller could mark nothing
critical, or claim that a summary preserves an id while omitting its meaning.
The product answers by making declarations explicit, retaining a strict exact
mode, refusing to call prose proof, and documenting the boundary prominently.

Other risks and mitigations:

- **Adapter diversity:** accept generic records and common OpenAI-like shapes,
  but keep the core schema deliberately small and fixture-driven.
- **Tokenizer mismatch:** prefer measured counts; label the fallback estimate
  and use it only as a headroom warning/error.
- **Overlapping tools:** keep the product at the compaction contract boundary;
  do not grow into tracing, prompt linting, fuzzing, or model evaluation.
- **Sensitive fixtures:** make all processing local and avoid echoing content in
  findings; teams still own their fixture-retention policy.

Follow-up work, only if users ask for it, could add adapters that *export* cases
from popular runtimes. Such adapters should remain separate from the
dependency-free core and must not make a provider SDK mandatory.
