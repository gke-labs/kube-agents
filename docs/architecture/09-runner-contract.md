# Design 09: The Runner Contract

**Status:** ✅ Agreed

> **Specifies the end state, not current behaviour.** The contract, its schemas, the null runner
> and the conformance suite exist today under `runner/`. **No production runner implements it
> yet** — making Hermes pass the conformance suite is a separate, later milestone, and until it
> lands the only conforming implementation is the null runner. See [README.md](README.md) for the
> delta against what ships today.

**Overview:** [README.md](README.md) · **Depends on:** [05](05-system-architecture.md),
[06](06-api-and-data-contracts.md), [08](08-agent-runtime-and-identity.md) ·
**Tier:** Buildable (bridging)

---

## TL;DR

Every agent execution in kube-agents goes through **one interface**:

```
run(principal, profile, task, workspace, budget) -> stream of events
```

A **runner** is anything that implements it. Hermes becomes the first runner rather than the
substrate everything else is written against, which is what makes a second runner — a different
harness, a hosted API, a shell script for a fixture — possible without rewriting the control plane.

The contract is **data, not Python**: two JSON Schemas in `runner/contract/`, a request and an
event. A runner in another language conforms by being wrapped in a subprocess or HTTP shim, not by
being ported. The **conformance suite** (`runner/conformance.py`) is a `unittest` mixin over those
schemas that any candidate runner must pass, and the **null runner** (`runner/null_runner.py`)
exists to prove the suite is satisfiable and to give the control plane something to test against
that never talks to a model.

The event vocabulary is deliberately close to what `/v1/responses` already emits — `tool_call`,
`tool_result`, `message` mirror `function_call`, `function_call_output`, `message` — so the first
adapter is a translation and not a redesign. Where it departs, it departs on purpose, and §4 says
where and why.

---

## 1. What this doc decides

The boundary between the **control plane** (what decides a run should happen, who it is for, and
what it is allowed to spend) and the **execution plane** (what actually runs the agent turn). It
decides the shape of the request across that boundary, the shape of the event stream back, and the
rules a runner must obey to be called conformant.

It does **not** decide which harness runs, how a profile is packaged ([08](08-agent-runtime-and-identity.md)),
or how a run is authorised ([03](03-security-model.md)). Those sit on either side of this line.

## 2. Goals / Non-goals

**Goals**

- One interface for every execution path — chat turn, cron run, delegated sub-task, bench case.
- A contract expressible on a wire, so the runner need not be in-process or in Python.
- Enough structure that the control plane can render progress, enforce a budget, and attribute a
  failure without parsing prose.
- A conformance suite with teeth: a runner that violates the contract must fail a test that names
  the violation.

**Non-goals**

- **Answer quality.** A runner that replies "no" to everything conforms, and should. Quality is the
  bench's job (`bench/`), and the separation is the point: a second runner can be judged conformant
  long before it is judged good.
- **Transport.** JSON dicts in, JSON dicts out. Whether they cross a process boundary as SSE, a
  gRPC stream, or newline-delimited JSON is a deployment decision.
- **Multi-runner scheduling.** Choosing *which* runner serves a request is the control plane's
  problem and is out of scope here.

## 3. The request

`runner/contract/run_request.schema.json`. Seven top-level fields, **all required** — an optional
field here is a field the runner has to guess at, and guessing is what this contract exists to end.
`additionalProperties: false` throughout, so an unrecognised field is a rejection rather than a
silently-ignored intent.

| Field              | Shape                                                                    | Why it is in the contract                                                                                                        |
| ------------------ | ------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------- |
| `contract_version` | const `v1alpha1`                                                         | A runner that meets an unknown version must refuse, not guess (§5).                                                              |
| `run_id`           | string                                                                   | Correlates every event to its run. Without it no valid event envelope can be built at all.                                       |
| `principal`        | `subject`, `issuer`, optional `display_name`, `attributes`               | *Who this run is for*, carried explicitly rather than inferred from ambient credentials. The prerequisite for per-user scoping.  |
| `profile`          | `name`, optional `revision`                                              | Which persona and skill set. `revision` lets a run pin what it ran against.                                                      |
| `task`             | `input`, optional `conversation`, `attachments`                          | The work. `conversation` is the continuity handle for a stateful runner.                                                         |
| `workspace`        | `mode` (`none` / `read-only` / `read-write`), optional `path`, `lease`   | Filesystem authority stated up front instead of discovered when a write fails.                                                   |
| `budget`           | optional `max_tokens`, `max_tool_calls`, `max_turns`, `deadline_seconds` | The object is required; every limit inside it is optional. A caller must decide it has no limits, rather than omit the question. |

## 4. The event stream

`runner/contract/run_event.schema.json`. A `oneOf` over seven event types, each carrying the same
envelope: `contract_version`, `run_id`, `seq`, `type`.

| Event          | Carries                                                        |
| -------------- | -------------------------------------------------------------- |
| `run.started`  | optional `profile` as the runner resolved it                   |
| `checklist`    | `items` — the plan, so progress is renderable without guessing |
| `tool_call`    | `call_id`, `name`, `arguments` (an object, already decoded)    |
| `tool_result`  | `call_id`, `status` (`completed` / `error`), `output`          |
| `message`      | `role`, `text`                                                 |
| `artifact`     | `kind`, `ref`, optional `media_type`, `description`            |
| `run.finished` | `status`, optional `error`, `usage`                            |

`run.finished.status` is one of `completed`, `failed`, `cancelled`, `budget_exceeded`, `refused`.
Distinguishing the last three from `failed` is what lets the control plane tell "the agent hit the
ceiling you set" apart from "the agent broke".

**Where this departs from the Responses shape, and why.**

1. **`tool_result.status` is explicit.** A Responses `function_call_output` carries no status, so
   today `bench/kube_agents_bench/parsing.py` infers failure by scanning the output text. That
   heuristic is wrong in both directions — a grep hit containing "error" reads as a failure, a
   silent non-zero exit does not. The contract makes the producer say. The heuristic still exists,
   quarantined in `runner/responses_adapter.py` as `sniff_failure()`, named so no reader mistakes
   it for something the producer reported.
2. **`arguments` is an object.** On the wire it is a JSON *string*; every consumer therefore
   re-implements the decode, including the failure case.
3. **`seq` is dense and starts at zero.** Gap-free ordering is checkable; wall-clock timestamps
   from a distributed producer are not.
4. **`checklist` has no Responses equivalent.** Progress is currently reconstructed by pattern-
   matching tool names. A plan the runner states is not a plan a consumer has to infer.

## 5. The rules a runner must obey

Encoded as tests in `runner/conformance.py`; this section is the prose, that file is the authority.

1. Every event validates against the event schema.
2. The stream is non-empty; it opens with exactly one `run.started` and closes with exactly one
   `run.finished`. Nothing follows the terminal event. **Two terminal events tell the control plane
   two different things about the same run**, so the count is exact, not a minimum.
3. `seq` is `0, 1, 2, …` with no gaps; every event carries the request's `run_id` and the
   contract version.
4. Every `tool_result` answers an earlier `tool_call` with the same `call_id`, and `call_id`s are
   unique within a run. A result the runner cannot attribute must not be emitted — an orphan is
   what forces the recovery code this contract removes.
5. Every event is JSON-serialisable. In every real deployment the stream crosses a process
   boundary, so an event holding a live object passes in-process and fails on the wire.
6. A terminal status other than `completed` carries an `error` explaining it. A bare failure is the
   defect this contract exists to prevent.
7. An unknown `contract_version` is **refused**, not attempted.
8. A malformed request is refused, not crashed — and a refused run does no work: no `tool_call` may
   appear in its stream.
9. Two runs off one runner instance do not share state. A sequence counter hoisted to instance
   scope is the easy version of this bug, and it only shows up on the second run.
10. `run()` is lazy. A runner that returns a fully-materialised list cannot stream progress, and
    every consumer written against it hangs the first time it meets one that does.

## 6. What exists today

| Path                            | What it is                                                                                     |
| ------------------------------- | ---------------------------------------------------------------------------------------------- |
| `runner/contract/*.schema.json` | The contract itself. Machine-readable, language-neutral.                                       |
| `runner/schema.py`              | Constants, validation helpers, a `new_request()` builder.                                      |
| `runner/jsonschema_mini.py`     | A stdlib validator for exactly the keywords the schemas use — **raises** on any other keyword. |
| `runner/conformance.py`         | The suite, as a mixin. Pair with `unittest.TestCase` and implement `make_runner()`.            |
| `runner/null_runner.py`         | Echo and scripted modes. The first conforming runner.                                          |
| `runner/responses_adapter.py`   | Recorded Responses payload → contract events. Evidence the shapes are derivable, not invented. |

`jsonschema_mini` is a deliberate choice: the suite must run on any machine with a Python 3
interpreter and no install step, and a validator that silently ignores a keyword it does not
implement would report unenforced schemas as satisfied. Raising is what makes the omission safe —
the schemas cannot grow a keyword without someone teaching the validator first.

`responses_adapter.py` is **not** the Hermes runner. It translates a *recorded* payload, and it
earns its place by keeping §4 honest: the claim that the event shapes are modelled on what
`/v1/responses` already emits is either demonstrated by a working translation or it is an
assertion.

## 7. Verification

- `make test-python` runs the suite (`runner/` is in `PYTHON_TEST_DIRS`). The null runner passes
  the conformance suite in both echo and scripted modes.
- `runner/test_conformance_null_runner.py::ConformanceSuiteHasTeeth` runs nine deliberately-broken
  runners and asserts each fails **the specific test that names its defect** — plus a control that
  runs the same test names against the conforming null runner, so a typo'd name or a broken dynamic
  subclass cannot make the teeth pass vacuously.
- The suite's teeth have been confirmed by mutation: weakening
  `test_run_finished_occurs_exactly_once` to a tautology makes the corresponding teeth-test fail by
  name.
- `runner/test_schema.py` checks the schemas parse, use only keywords the validator enforces, and
  that the Python constants match the schema (every declared event type has a branch; the terminal
  status set matches the enum).

## 8. Open at v1alpha1

1. **Cancellation** has a terminal status but no inbound channel. A runner cannot currently be told
   to stop; it can only report that it did.
2. **Budget enforcement is unlocated.** The contract carries the numbers and a
   `budget_exceeded` status, but does not say whether the runner or the control plane counts.
3. **`workspace.lease`** is a string with no lifecycle attached — reserved for the leasing model,
   not yet spent.
4. **Nested runs.** Delegation is a `tool_call` today. Whether a sub-run gets its own `run_id` and
   stream, or stays opaque inside the parent's tool result, is undecided.
5. **`artifact.ref`** is unqualified. Whether it is a workspace-relative path, a URI, or a content
   hash is left to the first real producer.

## 9. Hermes conformance is not part of this

Stated plainly because the temptation to read a contract as a description of the running system is
strong: **nothing in the shipping path implements this yet.** The Hermes runner — a real
implementation of `run()` that this suite is pointed at, with today's build-time patches,
`sitecustomize` monkey patches, and plugin-API reach-ins collapsed behind it — is a later
milestone. Until it lands, the honest summary is that kube-agents has a runner contract and one
null implementation of it, and the Hermes coupling audit under `docs/designs/` still describes how
agents actually execute.
