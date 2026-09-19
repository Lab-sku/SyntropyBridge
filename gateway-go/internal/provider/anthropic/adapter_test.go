package anthropic

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"

	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/protocol"
	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/provider"
)

func text(value string) *string { return &value }

func TestBuildRequestConvertsCanonicalFields(t *testing.T) {
	t.Parallel()

	maxTokens := 4096
	thinkingBudget := 2048
	effort := "high"
	strict := true
	toolCallID := "call_1"
	request := &protocol.CanonicalRequest{
		Protocol:  protocol.ProtocolOpenAIChat,
		Operation: protocol.OperationChat,
		Model:     "smart-chat",
		Messages: []protocol.Message{
			{Role: protocol.RoleSystem, Content: []protocol.ContentPart{{Type: "text", Text: text("You are precise.")}}},
			{Role: protocol.RoleUser, Content: []protocol.ContentPart{
				{Type: "image_url", ImageURL: &protocol.ImageURL{URL: "https://example.com/image.png"}},
				{Type: "text", Text: text("Describe it")},
			}},
			{Role: protocol.RoleAssistant, Content: []protocol.ContentPart{{Type: "tool_call", ToolCall: &protocol.ToolCall{
				ID: "call_1", Type: "function", Name: "lookup", Arguments: json.RawMessage(`{"q":"tokyo"}`),
			}}}},
			{Role: protocol.RoleTool, ToolCallID: &toolCallID, Content: []protocol.ContentPart{{Type: "text", Text: text("sunny")}}},
		},
		Tools: []protocol.ToolDefinition{{
			Name: "lookup", Description: "Lookup weather", InputSchema: json.RawMessage(`{"type":"object","properties":{"q":{"type":"string"}}}`), Strict: &strict,
		}},
		ToolChoice:     json.RawMessage(`{"type":"function","function":{"name":"lookup"}}`),
		Reasoning:      &protocol.ReasoningConfig{Effort: &effort, MaxTokens: &thinkingBudget},
		ResponseFormat: json.RawMessage(`{"type":"json_schema","json_schema":{"name":"answer","schema":{"type":"object","properties":{"answer":{"type":"string"}},"required":["answer"]}}}`),
		Parameters:     protocol.GenerationParameters{MaxOutputTokens: &maxTokens},
		Stream:         true,
		Metadata:       map[string]string{"user_id": "user_opaque_123"},
		Extensions: map[string]json.RawMessage{
			"parallel_tool_calls": json.RawMessage(`false`),
			"service_tier":        json.RawMessage(`"auto"`),
		},
	}
	adapter := New()
	upstream, err := adapter.BuildRequest(context.Background(), request, provider.Deployment{
		BaseURL:  "https://api.anthropic.com/v1",
		Metadata: map[string]string{"upstream_model": "claude-opus-5", "supports_reasoning": "true", "supports_structured_output": "true"},
	}, provider.Credential{Secret: "secret"})
	if err != nil {
		t.Fatalf("BuildRequest: %v", err)
	}
	if upstream.URL.String() != "https://api.anthropic.com/v1/messages" {
		t.Fatalf("unexpected URL: %s", upstream.URL)
	}
	if got := upstream.Header.Get("x-api-key"); got != "secret" {
		t.Fatalf("unexpected x-api-key: %q", got)
	}
	if got := upstream.Header.Get("anthropic-version"); got != defaultAPIVersion {
		t.Fatalf("unexpected version: %q", got)
	}
	if got := upstream.Header.Get("Accept"); got != "text/event-stream" {
		t.Fatalf("unexpected accept: %q", got)
	}

	body, err := io.ReadAll(upstream.Body)
	if err != nil {
		t.Fatal(err)
	}
	var payload map[string]any
	if err := json.Unmarshal(body, &payload); err != nil {
		t.Fatalf("decode payload: %v", err)
	}
	if payload["model"] != "claude-opus-5" || payload["max_tokens"].(float64) != 4096 {
		t.Fatalf("routing output missing: %s", body)
	}
	if payload["system"] != "You are precise." {
		t.Fatalf("system prompt not separated: %#v", payload["system"])
	}
	messages := payload["messages"].([]any)
	if len(messages) != 3 {
		t.Fatalf("unexpected messages: %#v", messages)
	}
	toolChoice := payload["tool_choice"].(map[string]any)
	if toolChoice["type"] != "tool" || toolChoice["name"] != "lookup" || toolChoice["disable_parallel_tool_use"] != true {
		t.Fatalf("tool choice not converted: %#v", toolChoice)
	}
	outputConfig := payload["output_config"].(map[string]any)
	if outputConfig["effort"] != "high" {
		t.Fatalf("effort not converted: %#v", outputConfig)
	}
	if _, ok := outputConfig["format"].(map[string]any); !ok {
		t.Fatalf("structured output not converted: %#v", outputConfig)
	}
	thinking := payload["thinking"].(map[string]any)
	if thinking["budget_tokens"].(float64) != 2048 {
		t.Fatalf("thinking not converted: %#v", thinking)
	}
	if payload["service_tier"] != "auto" {
		t.Fatalf("service tier not preserved: %#v", payload["service_tier"])
	}
}

func TestBuildRequestPreservesNativeUnknownFields(t *testing.T) {
	t.Parallel()

	request := &protocol.CanonicalRequest{
		Protocol:  protocol.ProtocolAnthropic,
		Operation: protocol.OperationChat,
		Model:     "public-alias",
		Stream:    true,
		RawBody:   json.RawMessage(`{"model":"public-alias","max_tokens":256,"messages":[{"role":"user","content":"hello"}],"future_field":{"enabled":true}}`),
	}
	upstream, err := New().BuildRequest(context.Background(), request, provider.Deployment{
		BaseURL:  "https://api.anthropic.com",
		Metadata: map[string]string{"upstream_model": "claude-opus-5"},
	}, provider.Credential{})
	if err != nil {
		t.Fatalf("BuildRequest: %v", err)
	}
	body, _ := io.ReadAll(upstream.Body)
	var payload map[string]any
	if err := json.Unmarshal(body, &payload); err != nil {
		t.Fatal(err)
	}
	if payload["model"] != "claude-opus-5" || payload["stream"] != true {
		t.Fatalf("model/stream not replaced: %s", body)
	}
	if _, ok := payload["future_field"]; !ok {
		t.Fatalf("unknown native field was lost: %s", body)
	}
}

func TestDecodeResponsePreservesBlocksAndUsage(t *testing.T) {
	t.Parallel()

	response := &http.Response{StatusCode: http.StatusOK, Body: io.NopCloser(strings.NewReader(`{
		"id":"msg_1","type":"message","role":"assistant","model":"claude-opus-5",
		"content":[
			{"type":"thinking","thinking":"considering","signature":"sig_1"},
			{"type":"text","text":"I will check."},
			{"type":"tool_use","id":"toolu_1","name":"lookup","input":{"q":"tokyo"}}
		],
		"stop_reason":"tool_use","stop_sequence":null,
		"usage":{"input_tokens":12,"output_tokens":7,"cache_read_input_tokens":5}
	}`))}
	decoded, err := New().DecodeResponse(context.Background(), response)
	if err != nil {
		t.Fatalf("DecodeResponse: %v", err)
	}
	if decoded.FinishReason != "tool_calls" {
		t.Fatalf("unexpected finish reason: %q", decoded.FinishReason)
	}
	if decoded.Usage.InputTokens != 12 || decoded.Usage.OutputTokens != 7 || decoded.Usage.CachedInputTokens != 5 || decoded.Usage.TotalTokens != 19 {
		t.Fatalf("usage lost: %#v", decoded.Usage)
	}
	parts := decoded.Messages[0].Content
	if len(parts) != 3 || parts[0].Reasoning == nil || parts[1].Text == nil || parts[2].ToolCall == nil {
		t.Fatalf("content blocks lost: %#v", parts)
	}
	if string(parts[2].ToolCall.Arguments) != `{"q":"tokyo"}` {
		t.Fatalf("tool input lost: %s", parts[2].ToolCall.Arguments)
	}
}

func TestDecodeStreamPreservesTextThinkingAndToolFragments(t *testing.T) {
	t.Parallel()

	stream := strings.Join([]string{
		"event: message_start", `data: {"type":"message_start","message":{"id":"msg_1","type":"message","role":"assistant","model":"claude-opus-5","content":[],"usage":{"input_tokens":10,"output_tokens":0}}}`, "",
		"event: content_block_start", `data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":"","signature":""}}`, "",
		"event: content_block_delta", `data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"need data"}}`, "",
		"event: content_block_delta", `data: {"type":"content_block_delta","index":0,"delta":{"type":"signature_delta","signature":"sig"}}`, "",
		"event: content_block_stop", `data: {"type":"content_block_stop","index":0}`, "",
		"event: content_block_start", `data: {"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}}`, "",
		"event: content_block_delta", `data: {"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"Checking"}}`, "",
		"event: content_block_stop", `data: {"type":"content_block_stop","index":1}`, "",
		"event: content_block_start", `data: {"type":"content_block_start","index":2,"content_block":{"type":"tool_use","id":"toolu_1","name":"lookup","input":{}}}`, "",
		"event: content_block_delta", `data: {"type":"content_block_delta","index":2,"delta":{"type":"input_json_delta","partial_json":"{\"q\":"}}`, "",
		"event: content_block_delta", `data: {"type":"content_block_delta","index":2,"delta":{"type":"input_json_delta","partial_json":"\"tokyo\"}"}}`, "",
		"event: content_block_stop", `data: {"type":"content_block_stop","index":2}`, "",
		"event: message_delta", `data: {"type":"message_delta","delta":{"stop_reason":"tool_use","stop_sequence":null},"usage":{"output_tokens":8}}`, "",
		"event: message_stop", `data: {"type":"message_stop"}`, "",
	}, "\n")
	response := &http.Response{StatusCode: http.StatusOK, Body: io.NopCloser(strings.NewReader(stream))}
	var events []protocol.StreamEvent
	if err := New().DecodeStream(context.Background(), response, func(event protocol.StreamEvent) error {
		events = append(events, event)
		return nil
	}); err != nil {
		t.Fatalf("DecodeStream: %v", err)
	}
	var args strings.Builder
	var sawThinking, sawText, sawToolStart, sawToolEnd, sawUsage, sawEnd bool
	for index, event := range events {
		if event.Sequence != uint64(index+1) {
			t.Fatalf("non-contiguous sequence at %d: %#v", index, event)
		}
		switch event.Type {
		case protocol.StreamEventReasoningDelta:
			if event.ReasoningDelta != nil && *event.ReasoningDelta != "" {
				sawThinking = true
			}
		case protocol.StreamEventTextDelta:
			sawText = event.TextDelta != nil && *event.TextDelta == "Checking"
		case protocol.StreamEventToolCallStart:
			sawToolStart = true
		case protocol.StreamEventToolCallDelta:
			args.WriteString(event.ToolCallDelta.ArgumentsFragment)
		case protocol.StreamEventToolCallEnd:
			sawToolEnd = true
		case protocol.StreamEventUsage:
			if event.Usage != nil && event.Usage.OutputTokens == 8 {
				sawUsage = true
			}
		case protocol.StreamEventResponseEnd:
			sawEnd = true
		}
	}
	if args.String() != `{"q":"tokyo"}` {
		t.Fatalf("tool fragments not reconstructable: %q", args.String())
	}
	if !sawThinking || !sawText || !sawToolStart || !sawToolEnd || !sawUsage || !sawEnd {
		t.Fatalf("missing semantic events: %#v", events)
	}
}

func TestNormalizeOverloadedErrorIsRetryable(t *testing.T) {
	t.Parallel()

	errorValue := New().NormalizeError(context.Background(), &http.Response{StatusCode: 529}, []byte(`{"type":"error","error":{"type":"overloaded_error","message":"Overloaded"},"request_id":"req_1"}`))
	if !errorValue.Retryable || errorValue.Code != "overloaded_error" || errorValue.Message != "Overloaded" {
		t.Fatalf("unexpected normalized error: %#v", errorValue)
	}
}
