package openai

import (
	"encoding/json"
	"testing"

	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/protocol"
)

func TestEncodeChatResponseIncludesToolCallsAndDetailedUsage(t *testing.T) {
	arguments := json.RawMessage(`{"city":"Tokyo"}`)
	response := &protocol.CanonicalResponse{
		ResponseID:   "chatcmpl_test",
		Model:        "model-a",
		CreatedAt:    123,
		FinishReason: "tool_calls",
		Messages: []protocol.Message{{
			Role: protocol.RoleAssistant,
			Content: []protocol.ContentPart{{
				Type: "tool_call",
				ToolCall: &protocol.ToolCall{
					ID:        "call_1",
					Type:      "function",
					Name:      "weather",
					Arguments: arguments,
				},
			}},
		}},
		Usage: &protocol.Usage{InputTokens: 10, OutputTokens: 4, CachedInputTokens: 3, ReasoningTokens: 2, TotalTokens: 14},
	}
	body, err := EncodeChatResponse(response)
	if err != nil {
		t.Fatalf("EncodeChatResponse() error = %v", err)
	}
	var decoded map[string]json.RawMessage
	if err := json.Unmarshal(body, &decoded); err != nil {
		t.Fatalf("json.Unmarshal() error = %v", err)
	}
	if _, ok := decoded["choices"]; !ok {
		t.Fatal("choices missing")
	}
	if _, ok := decoded["usage"]; !ok {
		t.Fatal("usage missing")
	}
}

func TestChatStreamEncoderRelaysWireChunkOncePerSourceSequence(t *testing.T) {
	encoder := NewChatStreamEncoder()
	raw := json.RawMessage(`{"id":"chatcmpl_1","choices":[{"delta":{"content":"hello"}}],"vendor":{"keep":true}}`)
	first, err := encoder.Encode(protocol.StreamEvent{Type: protocol.StreamEventWireChunk, SourceSequence: 1, Raw: raw})
	if err != nil {
		t.Fatalf("Encode(first) error = %v", err)
	}
	second, err := encoder.Encode(protocol.StreamEvent{Type: protocol.StreamEventWireChunk, SourceSequence: 1, Raw: raw})
	if err != nil {
		t.Fatalf("Encode(second) error = %v", err)
	}
	if len(first) != 1 || len(second) != 0 {
		t.Fatalf("frames first=%d second=%d", len(first), len(second))
	}
	if got := string(first[0]); got != "data: "+string(raw)+"\n\n" {
		t.Fatalf("frame = %q", got)
	}
}

func TestChatStreamEncoderSynthesizesCrossProtocolEvents(t *testing.T) {
	encoder := NewChatStreamEncoder()
	_, _ = encoder.Encode(protocol.StreamEvent{Type: protocol.StreamEventResponseStart, ResponseID: "resp_1", Model: "model-a"})
	text := "hello"
	frames, err := encoder.Encode(protocol.StreamEvent{Type: protocol.StreamEventTextDelta, TextDelta: &text})
	if err != nil {
		t.Fatalf("Encode(text) error = %v", err)
	}
	if len(frames) != 1 {
		t.Fatalf("text frames = %d", len(frames))
	}
	done, err := encoder.Encode(protocol.StreamEvent{Type: protocol.StreamEventResponseEnd})
	if err != nil {
		t.Fatalf("Encode(done) error = %v", err)
	}
	if len(done) != 1 || string(done[0]) != "data: [DONE]\n\n" {
		t.Fatalf("done = %q", done)
	}
}
