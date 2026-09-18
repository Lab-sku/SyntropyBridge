package openaicompat

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"

	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/protocol"
	openaiwire "github.com/Lab-sku/SyntropyBridge/gateway-go/internal/protocol/openai"
	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/provider"
)

func TestCapabilitiesRequireDeploymentOptIn(t *testing.T) {
	adapter := New()
	defaults := adapter.Capabilities(context.Background(), provider.Deployment{})
	if defaults.Tools || defaults.Reasoning || defaults.MultimodalInput {
		t.Fatalf("default capabilities overclaim support: %#v", defaults)
	}
	explicit := adapter.Capabilities(context.Background(), provider.Deployment{Metadata: map[string]string{
		"supports_tools":     "true",
		"supports_reasoning": "true",
		"supports_streaming": "true",
	}})
	if !explicit.Tools || !explicit.Reasoning || !explicit.Streaming {
		t.Fatalf("explicit capabilities were not enabled: %#v", explicit)
	}
}

func TestBuildRequestPreservesFieldsAndMapsModel(t *testing.T) {
	raw := []byte(`{
		"model":"public-alias",
		"messages":[{"role":"user","content":"hello"}],
		"tools":[{"type":"function","function":{"name":"weather","parameters":{"type":"object"}}}],
		"response_format":{"type":"json_object"},
		"vendor_extension":{"keep":true}
	}`)
	req, err := openaiwire.DecodeChatRequest(raw, "req_1")
	if err != nil {
		t.Fatalf("DecodeChatRequest() error = %v", err)
	}
	req.Stream = true

	httpRequest, err := New().BuildRequest(context.Background(), req, provider.Deployment{
		BaseURL: "https://example.invalid/v1",
		Metadata: map[string]string{
			"upstream_model": "real-model",
		},
		Headers: map[string]string{"X-Tenant": "tenant-a"},
	}, provider.Credential{Secret: "secret-key"})
	if err != nil {
		t.Fatalf("BuildRequest() error = %v", err)
	}
	if httpRequest.URL.String() != "https://example.invalid/v1/chat/completions" {
		t.Fatalf("URL = %q", httpRequest.URL.String())
	}
	if got := httpRequest.Header.Get("Authorization"); got != "Bearer secret-key" {
		t.Fatalf("Authorization = %q", got)
	}
	if got := httpRequest.Header.Get("Accept"); got != "text/event-stream" {
		t.Fatalf("Accept = %q", got)
	}
	body, _ := io.ReadAll(httpRequest.Body)
	var object map[string]json.RawMessage
	if err := json.Unmarshal(body, &object); err != nil {
		t.Fatalf("json.Unmarshal() error = %v", err)
	}
	var model string
	_ = json.Unmarshal(object["model"], &model)
	if model != "real-model" {
		t.Fatalf("model = %q", model)
	}
	if got := string(object["vendor_extension"]); got != `{"keep":true}` {
		t.Fatalf("vendor_extension = %s", got)
	}
	if _, ok := object["tools"]; !ok {
		t.Fatal("tools field was dropped")
	}
	if _, ok := object["response_format"]; !ok {
		t.Fatal("response_format field was dropped")
	}
}

func TestDecodeResponsePreservesToolCallsAndUsage(t *testing.T) {
	body := `{
		"id":"chatcmpl_1","model":"model-a","created":123,
		"choices":[{"index":0,"message":{"role":"assistant","content":null,
			"tool_calls":[{"id":"call_1","type":"function","function":{"name":"weather","arguments":"{\"city\":\"Tokyo\"}"}}]},
			"finish_reason":"tool_calls"}],
		"usage":{"prompt_tokens":10,"completion_tokens":7,"total_tokens":17,
			"prompt_tokens_details":{"cached_tokens":4},
			"completion_tokens_details":{"reasoning_tokens":2}}
	}`
	resp := &http.Response{StatusCode: http.StatusOK, Body: io.NopCloser(strings.NewReader(body))}
	result, err := New().DecodeResponse(context.Background(), resp)
	if err != nil {
		t.Fatalf("DecodeResponse() error = %v", err)
	}
	if result.ResponseID != "chatcmpl_1" || result.FinishReason != "tool_calls" {
		t.Fatalf("response metadata = %#v", result)
	}
	if len(result.Messages) != 1 || len(result.Messages[0].Content) != 1 || result.Messages[0].Content[0].ToolCall == nil {
		t.Fatalf("tool calls = %#v", result.Messages)
	}
	if result.Usage == nil || result.Usage.CachedInputTokens != 4 || result.Usage.ReasoningTokens != 2 {
		t.Fatalf("usage = %#v", result.Usage)
	}
}

func TestDecodeStreamEmitsTextToolReasoningAndUsage(t *testing.T) {
	stream := strings.Join([]string{
		`data: {"id":"chatcmpl_1","model":"model-a","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}`,
		"",
		`data: {"id":"chatcmpl_1","model":"model-a","choices":[{"index":0,"delta":{"reasoning_content":"think"},"finish_reason":null}]}`,
		"",
		`data: {"id":"chatcmpl_1","model":"model-a","choices":[{"index":0,"delta":{"content":"hello"},"finish_reason":null}]}`,
		"",
		`data: {"id":"chatcmpl_1","model":"model-a","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"weather","arguments":"{\"city\":"}}]},"finish_reason":null}]}`,
		"",
		`data: {"id":"chatcmpl_1","model":"model-a","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\"Tokyo\"}"}}]},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":3,"completion_tokens":4,"total_tokens":7}}`,
		"",
		"data: [DONE]",
		"",
	}, "\n")
	resp := &http.Response{StatusCode: http.StatusOK, Body: io.NopCloser(strings.NewReader(stream))}
	var events []protocol.StreamEvent
	err := New().DecodeStream(context.Background(), resp, func(event protocol.StreamEvent) error {
		events = append(events, event)
		return nil
	})
	if err != nil {
		t.Fatalf("DecodeStream() error = %v", err)
	}

	assertEventType(t, events, protocol.StreamEventResponseStart)
	assertEventType(t, events, protocol.StreamEventMessageStart)
	assertEventType(t, events, protocol.StreamEventReasoningDelta)
	assertEventType(t, events, protocol.StreamEventTextDelta)
	assertEventType(t, events, protocol.StreamEventToolCallStart)
	assertEventType(t, events, protocol.StreamEventToolCallDelta)
	assertEventType(t, events, protocol.StreamEventUsage)
	assertEventType(t, events, protocol.StreamEventMessageEnd)
	assertEventType(t, events, protocol.StreamEventResponseEnd)
	for index, event := range events {
		if event.Sequence != uint64(index+1) {
			t.Fatalf("event %d sequence = %d", index, event.Sequence)
		}
	}
}

func TestNormalizeErrorMarksRateLimitRetryable(t *testing.T) {
	result := New().NormalizeError(context.Background(), &http.Response{StatusCode: http.StatusTooManyRequests}, []byte(`{"error":{"message":"slow down","type":"rate_limit_error","code":"rate_limit"}}`))
	if !result.Retryable || result.Code != "rate_limit" || result.Message != "slow down" {
		t.Fatalf("normalized error = %#v", result)
	}
}

func assertEventType(t *testing.T, events []protocol.StreamEvent, expected protocol.StreamEventType) {
	t.Helper()
	for _, event := range events {
		if event.Type == expected {
			return
		}
	}
	t.Fatalf("event type %q not found in %#v", expected, events)
}
