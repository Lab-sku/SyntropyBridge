# SyntropyBridge Go Gateway

`gateway-go` is the v2 inference data plane. It is intentionally independent from the
legacy Python control plane while the migration is in progress.

## Current milestone

The first milestone contains:

- a compilable HTTP service with health/readiness endpoints;
- loss-aware canonical request, response and streaming-event types;
- provider adapter and registry contracts;
- an explainable weighted router primitive;
- unit tests and CI checks.

It does **not** yet claim provider inference compatibility.

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
