package anthropic

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/protocol"
)

func TestDecodeMessagesRequestPreservesToolsAndToolResults(t *testing.T) {
	body := []byte("{\"model\":\"public\",\"max_tokens\":256,\"system\":\"be concise\",\"messages\":[{\"role\":\"assistant\",\"content\":[{\"type\":\"tool_use\",\"id\":\"toolu_1\",\"name\":\"weather\",\"input\":{\"city\":\"Paris\"}}]},{\"role\":\"user\",\"content\":[{\"type\":\"tool_result\",\"tool_use_id\":\"toolu_1\",\"content\":\"sunny\"},{\"type\":\"text\",\"text\":\"thanks\"}]}],\"tools\":[{\"name\":\"weather\",\"description\":\"weather\",\"input_schema\":{\"type\":\"object\"}}],\"tool_choice\":{\"type\":\"tool\",\"name\":\"weather\",\"disable_parallel_tool_use\":true}}")
	req, err := DecodeMessagesRequest(body, "req_test")
	if err != nil {
		t.Fatalf("DecodeMessagesRequest() error = %v", err)
	}
	if req.Protocol != protocol.ProtocolAnthropic || len(req.Messages) != 4 {
		t.Fatalf("request = %#v", req)
	}
	if req.Messages[2].Role != protocol.RoleTool || req.Messages[2].ToolCallID == nil || *req.Messages[2].ToolCallID != "toolu_1" {
		t.Fatalf("tool result message = %#v", req.Messages[2])
	}
	if len(req.Tools) != 1 || req.Tools[0].Name != "weather" {
		t.Fatalf("tools = %#v", req.Tools)
	}
	if !strings.Contains(string(req.ToolChoice), "function") {
		t.Fatalf("tool choice = %s", req.ToolChoice)
	}
	var parallel bool
	if json.Unmarshal(req.Extensions["parallel_tool_calls"], &parallel) != nil || parallel {
		t.Fatalf("parallel_tool_calls = %s", req.Extensions["parallel_tool_calls"])
	}
}

func TestEncodeMessagesResponseUsesPublicModel(t *testing.T) {
	text := "hello"
	response := &protocol.CanonicalResponse{
		ResponseID: "chatcmpl_upstream",
		Model:      "real-model",
		Messages: []protocol.Message{{
			Role:    protocol.RoleAssistant,
			Content: []protocol.ContentPart{{Type: "text", Text: &text}},
		}},
		Usage: &protocol.Usage{InputTokens: 2, OutputTokens: 1, TotalTokens: 3},
	}
	body, err := EncodeMessagesResponse(response, "public-alias")
	if err != nil {
		t.Fatalf("EncodeMessagesResponse() error = %v", err)
	}
	var object map[string]any
	if json.Unmarshal(body, &object) != nil {
		t.Fatalf("response = %s", body)
	}
	if object["type"] != "message" || object["model"] != "public-alias" {
		t.Fatalf("response = %s", body)
	}
}

func TestStreamEncoderConvertsTextAndToolEvents(t *testing.T) {
	encoder := NewStreamEncoder("req_test", "public")
	text := "hello"
	finish := "tool_calls"
	events := []protocol.StreamEvent{
		{Type: protocol.StreamEventResponseStart, ResponseID: "chatcmpl_upstream"},
		{Type: protocol.StreamEventTextDelta, TextDelta: &text},
		{Type: protocol.StreamEventToolCallStart, ToolCallDelta: &protocol.ToolCallDelta{Index: 0, ID: "call_1", Name: "weather", ArgumentsFragment: "{\"city\":"}},
		{Type: protocol.StreamEventToolCallDelta, ToolCallDelta: &protocol.ToolCallDelta{Index: 0, ArgumentsFragment: "\"Paris\"}"}},
		{Type: protocol.StreamEventMessageEnd, FinishReason: &finish},
		{Type: protocol.StreamEventResponseEnd},
	}
	var output strings.Builder
	for _, event := range events {
		frames, err := encoder.Encode(event)
		if err != nil {
			t.Fatalf("Encode() error = %v", err)
		}
		for _, frame := range frames {
			output.Write(frame)
		}
	}
	stream := output.String()
	for _, required := range []string{"event: message_start", "text_delta", "tool_use", "input_json_delta", "\"stop_reason\":\"tool_use\"", "event: message_stop"} {
		if !strings.Contains(stream, required) {
			t.Fatalf("stream missing %q: %s", required, stream)
		}
	}
	if strings.Contains(stream, "chat.completion.chunk") {
		t.Fatalf("OpenAI wire format leaked: %s", stream)
	}
}
