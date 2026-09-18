# SyntropyBridge v2 migration plan

## Target

Move inference traffic from the legacy Python proxy to a Go gateway without a flag-day
rewrite of user, administration or commerce functions.

## Phase 0 — repository and contract foundation

- create `gateway-go/` as an independent Go module;
- establish canonical protocol and stream-event types;
- establish provider and router interfaces;
- add Go formatting, vet and test CI;
- keep `main` unchanged; develop on `v2-reboot`.

**Exit:** `go test ./...` and `go vet ./...` pass in CI.

## Phase 1 — protocol core

Implement parsers/renderers for:

- OpenAI Chat Completions;
- OpenAI Responses;
- Anthropic Messages;
- Gemini GenerateContent.

Build golden fixtures for text, tools, multimodal, reasoning and streaming events.

**Exit:** protocol round-trip tests prove that supported fields are not silently lost.

## Phase 2 — provider execution

Implement native adapters for OpenAI-compatible, OpenAI, Anthropic and Gemini.
Introduce deployment- and credential-scoped health state.

**Exit:** adapters pass shared contract suites against deterministic fake upstreams.

## Phase 3 — routing and reliability

Add model aliases, route policies, weighted/priority/latency strategies, retry budgets,
fallback rules, circuit breakers and request cancellation.

**Exit:** every request produces an explainable routing decision and attempt timeline.

## Phase 4 — ledger and observability

Add immutable request/attempt records, usage reconciliation, integer micro-credit
accounting, OpenTelemetry spans and an operator trace endpoint.

**Exit:** a completed or failed request can be reconstructed from trace records without
reading application logs.

## Phase 5 — shadow and cutover

- mirror safe non-streaming requests to the Go gateway without returning its output;
- compare normalized results and usage metadata;
- canary selected API keys and model aliases;
- route all inference endpoints to Go with immediate rollback available.

**Exit:** the Python inference path is disabled but retained for one release.

## Phase 6 — control-plane migration

Move provider/deployment configuration, virtual keys, budgets and eventually user and
commerce services to Go. Remove Python only after data migration and rollback drills.

## Explicitly deferred

Stripe, crypto payments, coupons, auto recharge and consumer chat history remain
outside the early v2 critical path.
