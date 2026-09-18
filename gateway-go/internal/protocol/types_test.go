package protocol

import (
	"encoding/json"
	"testing"
)

func TestCanonicalRequestPreservesHeterogeneousContentAndExtensions(t *testing.T) {
	text := "describe this image"
	rawExtension := json.RawMessage(`{"vendor":true}`)
	req := CanonicalRequest{
		RequestID: "req_test",
		Protocol:  ProtocolOpenAIResponses,
		Operation: OperationChat,
		Model:     "smart-model",
		Messages: []Message{{
			Role: RoleUser,
			Content: []ContentPart{
				{Type: "text", Text: &text},
				{Type: "image_url", ImageURL: &ImageURL{URL: "https://example.invalid/image.png"}},
			},
		}},
		Extensions: map[string]json.RawMessage{"vendor_option": rawExtension},
	}

	encoded, err := json.Marshal(req)
	if err != nil {
		t.Fatalf("json.Marshal() error = %v", err)
	}

	var decoded CanonicalRequest
	if err := json.Unmarshal(encoded, &decoded); err != nil {
		t.Fatalf("json.Unmarshal() error = %v", err)
	}
	if len(decoded.Messages) != 1 || len(decoded.Messages[0].Content) != 2 {
		t.Fatalf("content was flattened or lost: %#v", decoded.Messages)
	}
	if got := string(decoded.Extensions["vendor_option"]); got != string(rawExtension) {
		t.Fatalf("extension = %s, want %s", got, rawExtension)
	}
}

func TestStreamEventStartsOutput(t *testing.T) {
	if (StreamEvent{Type: StreamEventResponseStart}).StartsOutput() {
		t.Fatal("response.start must not mark user-visible output")
	}
	if !(StreamEvent{Type: StreamEventToolCallDelta}).StartsOutput() {
		t.Fatal("tool_call.delta must mark user-visible output")
	}
}
