package gateway

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestAnthropicMessagesEndToEndThroughOpenAICompatibleUpstream(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		body, _ := io.ReadAll(request.Body)
		var object map[string]json.RawMessage
		if json.Unmarshal(body, &object) != nil {
			t.Fatalf("invalid upstream JSON: %s", body)
		}
		var model string
		_ = json.Unmarshal(object["model"], &model)
		if model != "real-model" || object["messages"] == nil {
			t.Fatalf("upstream body = %s", body)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, "{\"id\":\"chatcmpl_a\",\"model\":\"real-model\",\"choices\":[{\"index\":0,\"message\":{\"role\":\"assistant\",\"content\":\"hello\"},\"finish_reason\":\"stop\"}],\"usage\":{\"prompt_tokens\":2,\"completion_tokens\":1,\"total_tokens\":3}}")
	}))
	defer upstream.Close()

	gatewayServer := newConfiguredTestGateway(t, upstream, nil)
	defer gatewayServer.Close()

	response := doAnthropicRequest(t, gatewayServer.URL, "{\"model\":\"public-alias\",\"max_tokens\":64,\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}", false)
	defer response.Body.Close()
	payload, _ := io.ReadAll(response.Body)
	if response.StatusCode != http.StatusOK {
		t.Fatalf("status=%d body=%s", response.StatusCode, payload)
	}
	var object map[string]any
	if json.Unmarshal(payload, &object) != nil || object["type"] != "message" || object["model"] != "public-alias" {
		t.Fatalf("response = %s", payload)
	}
}

func TestAnthropicMessagesStreamingTranslatesChatSSE(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = io.WriteString(w, "data: {\"id\":\"chatcmpl_stream\",\"model\":\"real-model\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\"},\"finish_reason\":null}]}\n\n")
		_, _ = io.WriteString(w, "data: {\"id\":\"chatcmpl_stream\",\"model\":\"real-model\",\"choices\":[{\"index\":0,\"delta\":{\"content\":\"hello\"},\"finish_reason\":\"stop\"}]}\n\n")
		_, _ = io.WriteString(w, "data: [DONE]\n\n")
	}))
	defer upstream.Close()

	gatewayServer := newConfiguredTestGateway(t, upstream, map[string]string{"supports_streaming": "true"})
	defer gatewayServer.Close()

	response := doAnthropicRequest(t, gatewayServer.URL, "{\"model\":\"public-alias\",\"max_tokens\":64,\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"stream\":true}", true)
	defer response.Body.Close()
	payload, _ := io.ReadAll(response.Body)
	stream := string(payload)
	if response.StatusCode != http.StatusOK || !strings.HasPrefix(response.Header.Get("Content-Type"), "text/event-stream") {
		t.Fatalf("status=%d content-type=%q body=%s", response.StatusCode, response.Header.Get("Content-Type"), stream)
	}
	for _, required := range []string{"event: message_start", "text_delta", "\"text\":\"hello\"", "event: message_stop"} {
		if !strings.Contains(stream, required) {
			t.Fatalf("stream missing %q: %s", required, stream)
		}
	}
}

func doAnthropicRequest(t *testing.T, baseURL, body string, stream bool) *http.Response {
	t.Helper()
	request, err := http.NewRequest(http.MethodPost, baseURL+"/v1/messages", bytes.NewBufferString(body))
	if err != nil {
		t.Fatalf("new request: %v", err)
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("x-api-key", testClientToken)
	if stream {
		request.Header.Set("Accept", "text/event-stream")
	}
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatalf("gateway request: %v", err)
	}
	return response
}
