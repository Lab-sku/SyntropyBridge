# SyntropyBridge v2 protocol capability matrix

This document is the source of truth for claimed protocol support. A capability may
be marked **supported** only when its request conversion, response conversion,
streaming behaviour and error mapping have automated contract coverage.

Legend: `planned`, `in-progress`, `supported`, `blocked`.

| Client protocol | Basic request | Streaming | Tools | Multimodal | Reasoning | Structured output | Status |
|---|---:|---:|---:|---:|---:|---:|---|
| OpenAI Chat Completions | in-progress | in-progress | in-progress | in-progress | in-progress | in-progress | in-progress |
| OpenAI Responses | — | — | — | — | — | — | planned |
| Anthropic Messages | in-progress | in-progress | in-progress | in-progress | in-progress | in-progress | in-progress |
| Gemini GenerateContent | — | — | — | — | — | — | planned |
| Embeddings | — | n/a | n/a | n/a | n/a | n/a | planned |
| Rerank | — | n/a | n/a | n/a | n/a | n/a | planned |
| Images | — | — | n/a | input/output | n/a | n/a | planned |
| Audio | — | — | n/a | audio | n/a | n/a | planned |
| Realtime | — | — | — | — | — | — | planned |

## Current implementation notes

- OpenAI Chat Completions has a public client endpoint and an OpenAI-compatible
  upstream adapter, but production routing, retries, budgets and durable traces remain
  unfinished.
- Anthropic Messages now has a native upstream adapter and automated conversion tests
  for content blocks, images, tools, thinking, structured output, usage, errors and
  named SSE events.
- The public Anthropic-compatible `POST /v1/messages` client endpoint is still pending.
  Therefore no Anthropic row is marked `supported` yet.

## Provider-family implementation order

1. Generic OpenAI-compatible deployments.
2. OpenAI native Chat Completions and Responses.
3. Anthropic native Messages.
4. Gemini native GenerateContent.
5. Azure OpenAI, AWS Bedrock and Vertex AI.
6. Long-tail providers through explicit adapters or an optional sidecar.

## Required contract scenarios

Every capability declaration must have tests for the relevant cases:

- non-streaming text response;
- streaming text deltas and terminal event;
- one and multiple tool calls;
- fragmented streaming tool arguments;
- tool results returned to the model;
- image and mixed content parts;
- structured output/schema fields;
- reasoning/thinking fields where provided;
- usage metadata and missing usage metadata;
- provider error normalization;
- client cancellation;
- upstream timeout before first byte;
- upstream disconnect after stream start;
- retry/fallback eligibility;
- unknown-field preservation policy.

## Claim policy

Provider logos, model catalog entries and successful model discovery do not count as
inference support. A provider is publicly listed only after a real inference route and
its declared contract suite pass.
