# SyntropyBridge Go Gateway

`gateway-go` is the v2 inference data plane. It is intentionally independent from the
legacy Python control plane while the migration is in progress.

## Current milestone

The current vertical slice contains:

- a runnable Go HTTP service with health and readiness endpoints;
- loss-aware canonical request, response and streaming-event types;
- provider adapter and registry contracts;
- an OpenAI-compatible adapter with request-field preservation, response decoding,
  SSE text/reasoning/tool-call events, usage parsing and error normalization;
- a native Anthropic Messages upstream adapter with system/content-block conversion,
  tools, images, thinking, structured output, usage, native SSE events and normalized
  errors;
- exact model-alias resolution and explainable weighted routing;
- authenticated `POST /v1/chat/completions` for non-streaming and streaming requests;
- an explicit single-upstream bootstrap mode for development and migration testing;
- race-tested unit and end-to-end tests in GitHub Actions.

Bootstrap mode is deliberately narrow. It is not the future production control plane,
and it never infers a provider from a model name.

The Anthropic adapter is registered and can be selected by an injected route resolver.
The public Anthropic-compatible `POST /v1/messages` client endpoint is not wired yet,
so Anthropic protocol support remains **in progress** rather than publicly claimed as
complete.

## Build and test

```bash
cd gateway-go
gofmt -w .
go vet ./...
go test -race -count=1 ./...
go build ./cmd/syntropy-gateway
```

With bootstrap mode disabled, the process starts for health inspection but inference
fails closed: `/readyz` returns `503` and `/v1/chat/completions` returns `503`.

## Safe bootstrap mode

Copy the example, replace every placeholder and load it into the process environment:

```bash
cd gateway-go
cp .env.bootstrap.example .env.bootstrap
set -a
. ./.env.bootstrap
set +a
go run ./cmd/syntropy-gateway
```

The minimum required settings are:

```bash
SYNTROPY_BOOTSTRAP_ENABLED=true
SYNTROPY_BOOTSTRAP_CLIENT_TOKEN=replace-with-at-least-16-random-characters
SYNTROPY_BOOTSTRAP_MODEL_ALIAS=smart-chat
SYNTROPY_BOOTSTRAP_UPSTREAM_BASE_URL=https://api.example.com/v1
SYNTROPY_BOOTSTRAP_UPSTREAM_MODEL=provider-model-id
SYNTROPY_BOOTSTRAP_UPSTREAM_API_KEY=replace-with-provider-key
```

Call the public alias rather than the upstream model ID:

```bash
curl http://127.0.0.1:8081/v1/chat/completions \
  -H "Authorization: Bearer $SYNTROPY_BOOTSTRAP_CLIENT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "smart-chat",
    "messages": [{"role": "user", "content": "hello"}]
  }'
```

Streaming uses the same endpoint with `"stream": true`.

## Capability declarations

OpenAI-compatible providers differ. The gateway therefore assumes only basic
non-streaming chat support. Enable a capability only after verifying the upstream:

```bash
SYNTROPY_BOOTSTRAP_SUPPORTS_STREAMING=true
SYNTROPY_BOOTSTRAP_SUPPORTS_TOOLS=true
SYNTROPY_BOOTSTRAP_SUPPORTS_PARALLEL_TOOLS=true
SYNTROPY_BOOTSTRAP_SUPPORTS_MULTIMODAL_INPUT=true
SYNTROPY_BOOTSTRAP_SUPPORTS_REASONING=true
SYNTROPY_BOOTSTRAP_SUPPORTS_STRUCTURED_OUTPUT=true
```

A request using an undeclared capability is rejected before the upstream is contacted.
Parallel tools require tools to be enabled.

For providers using `x-api-key: <secret>` rather than Bearer auth:

```bash
SYNTROPY_BOOTSTRAP_UPSTREAM_AUTH_HEADER=x-api-key
SYNTROPY_BOOTSTRAP_UPSTREAM_AUTH_SCHEME=none
```

The upstream HTTP client does not follow redirects, preventing a credential-bearing
request from being replayed to another origin.

## Process settings

| Variable | Default | Description |
|---|---|---|
| `SYNTROPY_GATEWAY_ADDR` | `:8081` | HTTP listen address |
| `SYNTROPY_GATEWAY_SHUTDOWN_TIMEOUT` | `10s` | graceful shutdown limit |
| `SYNTROPY_GATEWAY_READ_HEADER_TIMEOUT` | `5s` | request-header timeout |
| `SYNTROPY_GATEWAY_IDLE_TIMEOUT` | `120s` | client idle connection timeout |
| `SYNTROPY_UPSTREAM_DIAL_TIMEOUT` | `10s` | upstream connect timeout |
| `SYNTROPY_UPSTREAM_TLS_HANDSHAKE_TIMEOUT` | `10s` | upstream TLS handshake timeout |
| `SYNTROPY_UPSTREAM_RESPONSE_HEADER_TIMEOUT` | `60s` | time to upstream response headers |
| `SYNTROPY_UPSTREAM_IDLE_CONN_TIMEOUT` | `90s` | upstream idle pool timeout |
| `SYNTROPY_UPSTREAM_MAX_IDLE_CONNS` | `100` | total upstream idle connections |
| `SYNTROPY_UPSTREAM_MAX_IDLE_CONNS_PER_HOST` | `20` | idle connections per upstream host |

There is intentionally no global `http.Client.Timeout`; streaming lifetime is governed
by the request context and client disconnect.

## Endpoints

- `GET /healthz` — process is alive;
- `GET /readyz` — inference and client authentication are configured;
- `GET /v1/gateway/capabilities` — registered adapters and readiness state;
- `POST /v1/chat/completions` — authenticated OpenAI-compatible chat endpoint.

## Security notes

- Never commit `.env.bootstrap` or real tokens.
- Bootstrap errors name missing variables but do not echo secret values.
- Base URLs containing user information, query strings or fragments are rejected.
- Provider capability flags default to false rather than overclaiming compatibility.
- Bootstrap mode is for one explicit deployment; multi-user keys, budgets, retries,
  circuit breakers and durable traces remain later v2 milestones.
