# ADR-0001: Rebuild the gateway data plane in Go

- **Status:** Accepted
- **Date:** 2026-09-18
- **Decision owners:** SyntropyBridge maintainers

## Context

SyntropyBridge v1 combines control-plane concerns (users, plans, wallets, payments,
provider configuration) with gateway data-plane concerns (protocol parsing, provider
request construction, streaming, retries, routing, usage metering and settlement).
The current Python implementation also contains two partially overlapping provider
paths: provider-specific adapters and a generic proxy path. This makes protocol
correctness difficult to verify and allows provider-specific requirements to be lost
in the generic path.

The project needs reliable support for long-lived streaming connections, request
cancellation, tool-call deltas, reasoning events, multimodal content, provider-native
protocols, per-deployment health, retries before the first response byte, and a durable
request ledger.

## Decision

SyntropyBridge v2 will introduce a new Go data plane under `gateway-go/`.

The Go gateway will own:

1. public inference endpoints;
2. client-protocol parsing and response rendering;
3. a loss-aware canonical request/event representation;
4. provider adapter execution;
5. deployment selection, retries, fallback and circuit-breaking;
6. streaming lifecycle and cancellation propagation;
7. usage collection and request-attempt tracing.

The existing Python application remains the legacy control plane during migration.
It will continue to own existing user, administration and commerce workflows until
those capabilities are deliberately migrated. The Python proxy path will not be
translated line-by-line.

## Architecture principles

- **Protocol-native first.** OpenAI Chat Completions, OpenAI Responses, Anthropic
  Messages and Gemini GenerateContent remain first-class protocols.
- **No silent field loss.** Known fields are typed; unrecognised provider/client fields
  can be preserved as raw JSON extensions where safe.
- **Provider is not deployment.** A provider describes a protocol family; a deployment
  is a concrete base URL/region/configuration; a credential belongs to a deployment.
- **Failure isolation is deployment-scoped.** One unhealthy endpoint or credential must
  not disable an entire provider family.
- **Retry only when semantically safe.** Cross-deployment retry is allowed before a
  response begins; after a meaningful stream event, the request is not replayed by
  default.
- **Explainable routing.** Every routing decision records considered candidates,
  exclusions and the reason for selection.
- **Contract tests before provider count.** A provider is supported only when its
  declared capabilities pass the corresponding non-streaming and streaming tests.

## Why Go

Go is selected for the data plane because its standard HTTP stack, goroutines,
`context.Context`, static types and single-binary deployment model fit the gateway's
concurrency and lifecycle requirements. This is not a claim that Python cannot power
an AI gateway; it is a decision to reduce operational and protocol-state complexity in
this project.

## Consequences

### Positive

- clear boundary between control plane and request hot path;
- typed protocol and streaming state models;
- lower-cost concurrency for long-lived connections;
- easier request cancellation and timeout propagation;
- one deployable gateway binary;
- independent rollout and rollback from the legacy application.

### Negative

- temporary dual-stack operation;
- duplicated configuration models during migration;
- additional engineering and observability work;
- existing Python business logic cannot be reused mechanically.

## Non-goals for the first milestone

- rewriting payments, plans, coupons or order workflows;
- matching every provider listed by mature gateways;
- replacing PostgreSQL/Redis with custom infrastructure;
- copying AGPL-licensed implementations into this repository.

## Migration guardrail

The Go gateway may receive production inference traffic only after protocol contract
suites cover the enabled capabilities and the legacy route can be restored without a
database migration.
