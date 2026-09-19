package gemini

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

func TestBuildRequestConvertsCanonicalChat(t *testing.T) {
	text := "hello"
	arguments := json.RawMessage("{\"city\":\"Paris\"}")
	req := &protocol.CanonicalRequest{
		Protocol:  protocol.ProtocolOpenAIChat,
		Operation: protocol.OperationChat,
		Model:     "public",
		Messages: []protocol.Message{
			{Role: protocol.RoleSystem, Content: []protocol.ContentPart{{Type: "text", Text: &text}}},
			{Role: protocol.RoleUser, Content: []protocol.ContentPart{{Type: "text", Text: &text}}},
			{Role: protocol.RoleAssistant, Content: []protocol.ContentPart{{Type: "tool_call", ToolCall: &protocol.ToolCall{ID: "call_1", Name: "weather", Arguments: arguments}}}},
			{Role: protocol.RoleTool, ToolCallID: stringPtr("call_1"), Content: []protocol.ContentPart{{Type: "text", Text: &text}}},
		},
		Tools: []protocol.ToolDefinition{{Name: "weather", InputSchema: json.RawMessage("{\"type\":\"object\"}")}},
	}
	httpReq, err := New().BuildRequest(context.Background(), req, provider.Deployment{
		BaseURL: "https://generativelanguage.googleapis.com",
		Metadata: map[string]string{"upstream_model": "gemini-test"},
	}, provider.Credential{Secret: "secret"})
	if err != nil {
		t.Fatalf("BuildRequest() error = %v", err)
	}
	if !strings.Contains(httpReq.URL.Path, "/v1beta/models/gemini-test:generateContent") {
		t.Fatalf("path = %q", httpReq.URL.Path)
	}
	if httpReq.Header.Get("x-goog-api-key") != "secret" {
		t.Fatalf("api key header = %q", httpReq.Header.Get("x-goog-api-key"))
	}
	body, _ := io.ReadAll(httpReq.Body)
	for _, want := range []string{"systemInstruction", "functionDeclarations", "functionCall", "functionResponse"} {
		if !strings.Contains(string(body), want) {
			t.Fatalf("body missing %q: %s", want, body)
		}
	}
}

func TestDecodeGenerateContentResponse(t *testing.T) {
	body := []byte("{\"candidates\":[{\"content\":{\"role\":\"model\",\"parts\":[{\"text\":\"hello\"},{\"functionCall\":{\"id\":\"call_1\",\"name\":\"weather\",\"args\":{\"city\":\"Paris\"}}}]},\"finishReason\":\"STOP\"}],\"usageMetadata\":{\"promptTokenCount\":2,\"candidatesTokenCount\":3,\"totalTokenCount\":5},\"modelVersion\":\"gemini-test\",\"responseId\":\"resp_1\"}")
	response, err := decodeGenerateContentResponse(body)
	if err != nil {
		t.Fatalf("decodeGenerateContentResponse() error = %v", err)
	}
	if response.FinishReason != "stop" || response.Usage == nil || response.Usage.TotalTokens != 5 {
		t.Fatalf("response = %#v", response)
	}
	if len(response.Messages) != 1 || len(response.Messages[0].Content) != 2 || response.Messages[0].Content[1].ToolCall == nil {
		t.Fatalf("messages = %#v", response.Messages)
	}
}

func TestDecodeStreamProducesSemanticEvents(t *testing.T) {
	body := "data: {\"responseId\":\"resp_1\",\"modelVersion\":\"gemini-test\",\"candidates\":[{\"content\":{\"role\":\"model\",\"parts\":[{\"text\":\"hello\"}]}}]}\n\n" +
		"data: {\"responseId\":\"resp_1\",\"modelVersion\":\"gemini-test\",\"candidates\":[{\"content\":{\"role\":\"model\",\"parts\":[]},\"finishReason\":\"STOP\"}],\"usageMetadata\":{\"promptTokenCount\":2,\"candidatesTokenCount\":1,\"totalTokenCount\":3}}\n\n"
	resp := &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(body))}
	var events []protocol.StreamEventType
	err := New().DecodeStream(context.Background(), resp, func(event protocol.StreamEvent) error {
		events = append(events, event.Type)
		return nil
	})
	if err != nil {
		t.Fatalf("DecodeStream() error = %v", err)
	}
	joined := fmtEvents(events)
	for _, want := range []string{"response.start", "text.delta", "usage", "message.end", "response.end"} {
		if !strings.Contains(joined, want) {
			t.Fatalf("events missing %q: %s", want, joined)
		}
	}
}

func stringPtr(value string) *string { return &value }

func fmtEvents(events []protocol.StreamEventType) string {
	values := make([]string, 0, len(events))
	for _, event := range events {
		values = append(values, string(event))
	}
	return strings.Join(values, ",")
}
