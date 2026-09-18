# SyntropyBridge Go Gateway

`gateway-go` is the v2 inference data plane. It is intentionally independent from the
legacy Python control plane while the migration is in progress.

## Current milestone

The first milestone contains:

- a compilable HTTP service with health/readiness endpoints;
- loss-aware canonical request, response and streaming-event types;
- provider adapter and registry contracts;
- an OpenAI-compatible adapter with loss-aware request forwarding, response decoding,
  SSE text/reasoning/tool-call events, usage parsing and error normalization;
- an explainable weighted router primitive;
- unit tests and CI checks.

The adapter contract is implemented and tested, but the public inference handler and
deployment resolver are not yet wired. The service therefore does **not** claim end-to-end
provider inference readiness.

## Run

```bash
cd gateway-go
go test ./...
go run ./cmd/syntropy-gateway
```

Environment variables:

| Variable | Default | Description |
|---|---|---|
| `SYNTROPY_GATEWAY_ADDR` | `:8081` | HTTP listen address |
| `SYNTROPY_GATEWAY_SHUTDOWN_TIMEOUT` | `10s` | graceful shutdown limit |
| `SYNTROPY_GATEWAY_READ_HEADER_TIMEOUT` | `5s` | request-header timeout |
| `SYNTROPY_GATEWAY_IDLE_TIMEOUT` | `120s` | idle connection timeout |

Endpoints:

- `GET /healthz`
- `GET /readyz`
- `GET /v1/gateway/capabilities`
