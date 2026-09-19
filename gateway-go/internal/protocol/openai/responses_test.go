package openai

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/protocol"
)

func TestDecodeResponsesRequestConvertsTypedInput(t *testing.T) {
	body := []byte("{\"model\":\"public\",\"instructions\":\"be concise\",\"input\":[{\"type\":\"message\",\"role\":\"user\",\"content\":[{\"type\":\"input_text\",\"text\":\"hello\"}]},{\"type\":\"function_call_output\",\"call_id\":\"call_1\",\"output\":\"sunny\"}],\"tools\":[{\"type\":\"function\",\"name\":\"weather\",\"parameters\":{\"type\":\"object\"}}],\"text\":{\"format\":{\"type\":\"json_schema\",\"name\":\"answer\",\"schema\":{\"type\":\"object\"}}}}")
	req, err := DecodeResponsesRequest(body, "req_test")
	if err != nil {
		t.Fatalf("DecodeResponsesRequest() error = %v", err)
	}
	if req.Protocol != protocol.ProtocolOpenAIResponses || req.Operation != protocol.OperationChat {
		t.Fatalf("protocol=%q operation=%q", req.Protocol, req.Operation)
	}
	if len(req.Messages) != 3 || req.Messages[0].Role != protocol.RoleDeveloper || req.Messages[2].Role != protocol.RoleTool {
		t.Fatalf("messages = %#v", req.Messages)
	}
	if len(req.Tools) != 1 || req.Tools[0].Name != "weather" {
		t.Fatalf("tools = %#v", req.Tools)
	}
	if !strings.Contains(string(req.ResponseFormat), "json_schema") {
		t.Fatalf("response format = %s", req.ResponseFormat)
	}
}

func TestDecodeResponsesRequestRejectsStatefulCompatibilityFields(t *testing.T) {
	_, err := DecodeResponsesRequest([]byte("{\"model\":\"public\",\"input\":\"hi\",\"previous_response_id\":\"resp_123\"}"), "req_test")
	if err == nil || !strings.Contains(err.Error(), "previous_response_id") {
		t.Fatalf("error = %v", err)
	}
}

func TestEncodeResponsesResponseBuildsTypedOutput(t *testing.T) {
	text := "hello"
	response := &protocol.CanonicalResponse{
		RequestID:  "req_test",
		ResponseID: "resp_test",
		Model:      "public",
		Messages: []protocol.Message{{
			Role: protocol.RoleAssistant,
			Content: []protocol.ContentPart{
				{Type: "text", Text: &text},
				{Type: "tool_call", ToolCall: &protocol.ToolCall{ID: "call_1", Name: "weather", Arguments: json.RawMessage("{\"city\":\"Paris\"}")}},
			},
		}},
		Usage: &protocol.Usage{InputTokens: 2, OutputTokens: 3, TotalTokens: 5},
	}
	body, err := EncodeResponsesResponse(response)
	if err != nil {
		t.Fatalf("EncodeResponsesResponse() error = %v", err)
	}
	var object map[string]any
	if err := json.Unmarshal(body, &object); err != nil {
		t.Fatalf("json.Unmarshal() error = %v", err)
	}
	output, _ := object["output"].([]any)
	if object["object"] != "response" || object["model"] != "public" || len(output) != 2 {
		t.Fatalf("response = %s", body)
	}
	first := output[0].(map[string]any)
	second := output[1].(map[string]any)
	if first["type"] != "message" || second["type"] != "function_call" {
		t.Fatalf("output = %#v", output)
	}
}

func TestResponsesStreamEncoderDoesNotLeakChatWireChunks(t *testing.T) {
	encoder := NewResponsesStreamEncoder("req_test", "public")
	text := "hi"
	events := []protocol.StreamEvent{
		{Type: protocol.StreamEventResponseStart, ResponseID: "chatcmpl_upstream", Model: "real"},
		{Type: protocol.StreamEventWireChunk, Raw: json.RawMessage("{\"object\":\"chat.completion.chunk\"}")},
		{Type: protocol.StreamEventMessageStart},
		{Type: protocol.StreamEventTextDelta, TextDelta: &text},
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
	for _, required := range []string{"event: response.created", "event: response.output_text.delta", "event: response.completed"} {
		if !strings.Contains(stream, required) {
			t.Fatalf("stream missing %q: %s", required, stream)
		}
	}
	if strings.Contains(stream, "chat.completion.chunk") {
		t.Fatalf("chat wire chunk leaked into Responses stream: %s", stream)
	}
}
