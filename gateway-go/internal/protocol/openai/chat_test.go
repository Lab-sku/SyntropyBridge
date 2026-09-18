package openai

import (
	"encoding/json"
	"testing"
)

func TestDecodeChatRequestPreservesUnknownTopLevelFields(t *testing.T) {
	body := []byte(`{
		"model":"public-alias",
		"messages":[{"role":"user","content":[
			{"type":"text","text":"look"},
			{"type":"image_url","image_url":{"url":"https://example.invalid/a.png","detail":"high"}}
		]}],
		"tools":[{"type":"function","function":{"name":"weather","parameters":{"type":"object"},"strict":true}}],
		"response_format":{"type":"json_object"},
		"vendor_extension":{"keep":true}
	}`)

	req, err := DecodeChatRequest(body, "req_1")
	if err != nil {
		t.Fatalf("DecodeChatRequest() error = %v", err)
	}
	if req.Model != "public-alias" || len(req.Messages) != 1 || len(req.Messages[0].Content) != 2 {
		t.Fatalf("decoded request lost core fields: %#v", req)
	}
	if len(req.Tools) != 1 || req.Tools[0].Name != "weather" {
		t.Fatalf("tools = %#v", req.Tools)
	}
	if got := string(req.Extensions["vendor_extension"]); got != `{"keep":true}` {
		t.Fatalf("vendor_extension = %s", got)
	}
	if string(req.RawBody) != string(body) {
		t.Fatal("RawBody was not preserved exactly")
	}
}

func TestReplaceModelAndStreamDoesNotDropFields(t *testing.T) {
	body := []byte(`{"model":"alias","messages":[],"vendor":{"x":1},"stream":false}`)
	updated, err := ReplaceModelAndStream(body, "upstream-model", true)
	if err != nil {
		t.Fatalf("ReplaceModelAndStream() error = %v", err)
	}
	var object map[string]json.RawMessage
	if err := json.Unmarshal(updated, &object); err != nil {
		t.Fatalf("json.Unmarshal() error = %v", err)
	}
	if got := string(object["vendor"]); got != `{"x":1}` {
		t.Fatalf("vendor = %s", got)
	}
	var model string
	_ = json.Unmarshal(object["model"], &model)
	if model != "upstream-model" {
		t.Fatalf("model = %q", model)
	}
	var stream bool
	_ = json.Unmarshal(object["stream"], &stream)
	if !stream {
		t.Fatal("stream was not updated")
	}
}

func TestDecodeAssistantToolCall(t *testing.T) {
	raw := json.RawMessage(`{
		"role":"assistant",
		"content":null,
		"tool_calls":[{"id":"call_1","type":"function","function":{"name":"weather","arguments":"{\"city\":\"Tokyo\"}"}}],
		"refusal":null,
		"annotations":[]
	}`)
	message, err := DecodeMessage(raw)
	if err != nil {
		t.Fatalf("DecodeMessage() error = %v", err)
	}
	if len(message.Content) != 1 || message.Content[0].ToolCall == nil {
		t.Fatalf("tool call lost: %#v", message.Content)
	}
	call := message.Content[0].ToolCall
	if call.Name != "weather" || string(call.Arguments) != `{"city":"Tokyo"}` {
		t.Fatalf("tool call = %#v", call)
	}
}
