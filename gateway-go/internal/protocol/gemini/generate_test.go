package gemini

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/protocol"
)

func TestDecodeGenerateContentRequestConvertsFunctionResponse(t *testing.T) {
	body := []byte("{\"systemInstruction\":{\"parts\":[{\"text\":\"be concise\"}]},\"contents\":[{\"role\":\"model\",\"parts\":[{\"functionCall\":{\"id\":\"call_1\",\"name\":\"weather\",\"args\":{\"city\":\"Paris\"}}}]},{\"role\":\"user\",\"parts\":[{\"functionResponse\":{\"id\":\"call_1\",\"name\":\"weather\",\"response\":{\"result\":\"sunny\"}}},{\"text\":\"thanks\"}]}],\"tools\":[{\"functionDeclarations\":[{\"name\":\"weather\",\"parameters\":{\"type\":\"object\"}}]}]}")
	req, err := DecodeGenerateContentRequest(body, "public", "req_test", false)
	if err != nil {
		t.Fatalf("DecodeGenerateContentRequest() error = %v", err)
	}
	if req.Protocol != protocol.ProtocolGemini || len(req.Messages) != 4 {
		t.Fatalf("request = %#v", req)
	}
	if req.Messages[2].Role != protocol.RoleTool || req.Messages[2].ToolCallID == nil || *req.Messages[2].ToolCallID != "call_1" {
		t.Fatalf("tool message = %#v", req.Messages[2])
	}
	if len(req.Tools) != 1 || req.Tools[0].Name != "weather" {
		t.Fatalf("tools = %#v", req.Tools)
	}
}

func TestDecodeGenerateContentRequestMapsGenerationConfig(t *testing.T) {
	body := []byte("{\"contents\":[{\"role\":\"user\",\"parts\":[{\"text\":\"hi\"}]}],\"generationConfig\":{\"maxOutputTokens\":256,\"temperature\":0.2,\"responseMimeType\":\"application/json\",\"responseJsonSchema\":{\"type\":\"object\"},\"thinkingConfig\":{\"thinkingBudget\":1024,\"includeThoughts\":true}}}")
	req, err := DecodeGenerateContentRequest(body, "public", "req_test", false)
	if err != nil {
		t.Fatalf("DecodeGenerateContentRequest() error = %v", err)
	}
	if req.Parameters.MaxOutputTokens == nil || *req.Parameters.MaxOutputTokens != 256 {
		t.Fatalf("max output tokens = %#v", req.Parameters.MaxOutputTokens)
	}
	if req.Reasoning == nil || req.Reasoning.MaxTokens == nil || *req.Reasoning.MaxTokens != 1024 || !req.Reasoning.IncludeInOut {
		t.Fatalf("reasoning = %#v", req.Reasoning)
	}
	if !strings.Contains(string(req.ResponseFormat), "json_schema") {
		t.Fatalf("response format = %s", req.ResponseFormat)
	}
}

func TestEncodeGenerateContentResponseUsesPublicModel(t *testing.T) {
	text := "hello"
	response := &protocol.CanonicalResponse{
		ResponseID: "resp_1",
		Messages: []protocol.Message{{
			Role:    protocol.RoleAssistant,
			Content: []protocol.ContentPart{{Type: "text", Text: &text}},
		}},
		FinishReason: "stop",
		Usage:        &protocol.Usage{InputTokens: 2, OutputTokens: 1, TotalTokens: 3},
	}
	body, err := EncodeGenerateContentResponse(response, "public")
	if err != nil {
		t.Fatalf("EncodeGenerateContentResponse() error = %v", err)
	}
	var object map[string]any
	if err := json.Unmarshal(body, &object); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	if object["modelVersion"] != "public" || !strings.Contains(string(body), "\"finishReason\":\"STOP\"") {
		t.Fatalf("response = %s", body)
	}
}

func TestStreamEncoderBuffersFunctionArgumentsUntilValidJSON(t *testing.T) {
	encoder := NewStreamEncoder("resp_1", "public")
	finish := "tool_calls"
	events := []protocol.StreamEvent{
		{
			Type: protocol.StreamEventToolCallStart,
			ToolCallDelta: &protocol.ToolCallDelta{
				Index: 0, ID: "call_1", Name: "weather", ArgumentsFragment: "{\"city\":",
			},
		},
		{
			Type: protocol.StreamEventToolCallDelta,
			ToolCallDelta: &protocol.ToolCallDelta{
				Index: 0, ArgumentsFragment: "\"Paris\"}",
			},
		},
		{Type: protocol.StreamEventMessageEnd, FinishReason: &finish},
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
	if !strings.Contains(stream, "\"functionCall\"") || !strings.Contains(stream, "\"Paris\"") {
		t.Fatalf("stream = %s", stream)
	}
	if strings.Contains(stream, "[DONE]") {
		t.Fatalf("Gemini stream must not use OpenAI [DONE]: %s", stream)
	}
}

func TestStreamEncoderIgnoresForeignWireChunks(t *testing.T) {
	encoder := NewStreamEncoder("resp_1", "public")
	frames, err := encoder.Encode(protocol.StreamEvent{
		Type: protocol.StreamEventWireChunk,
		Raw:  json.RawMessage("{\"object\":\"chat.completion.chunk\"}"),
	})
	if err != nil {
		t.Fatalf("Encode() error = %v", err)
	}
	if len(frames) != 0 {
		t.Fatalf("foreign wire chunk leaked: %q", frames)
	}
}
