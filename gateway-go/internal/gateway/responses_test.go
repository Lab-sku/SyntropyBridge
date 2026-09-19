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

func TestResponsesEndToEndConvertsThroughChatCompatibleUpstream(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		body, _ := io.ReadAll(request.Body)
		var object map[string]json.RawMessage
		if err := json.Unmarshal(body, &object); err != nil {
			t.Fatalf("decode upstream body: %v", err)
		}
		var model string
		_ = json.Unmarshal(object["model"], &model)
		if model != "real-model" {
			t.Fatalf("upstream model = %q", model)
		}
		if _, ok := object["messages"]; !ok {
			t.Fatalf("chat-compatible upstream body has no messages: %s", body)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, "{\"id\":\"chatcmpl_resp\",\"object\":\"chat.completion\",\"model\":\"real-model\",\"choices\":[{\"index\":0,\"message\":{\"role\":\"assistant\",\"content\":\"hello\"},\"finish_reason\":\"stop\"}],\"usage\":{\"prompt_tokens\":2,\"completion_tokens\":1,\"total_tokens\":3}}")
	}))
	defer upstream.Close()

	gatewayServer := newConfiguredTestGateway(t, upstream, nil)
	defer gatewayServer.Close()

	response := doResponsesRequest(t, gatewayServer.URL, "{\"model\":\"public-alias\",\"input\":\"hi\"}")
	defer response.Body.Close()
	payload, _ := io.ReadAll(response.Body)
	if response.StatusCode != http.StatusOK {
		t.Fatalf("status=%d body=%s", response.StatusCode, payload)
	}
	var object map[string]any
	if err := json.Unmarshal(payload, &object); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	output, _ := object["output"].([]any)
	if object["object"] != "response" || object["model"] != "public-alias" || len(output) != 1 {
		t.Fatalf("response body = %s", payload)
	}
}

func TestResponsesStreamingTranslatesChatSSE(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = io.WriteString(w, "data: {\"id\":\"chatcmpl_stream\",\"model\":\"real-model\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\"},\"finish_reason\":null}]}\n\n")
		_, _ = io.WriteString(w, "data: {\"id\":\"chatcmpl_stream\",\"model\":\"real-model\",\"choices\":[{\"index\":0,\"delta\":{\"content\":\"hello\"},\"finish_reason\":\"stop\"}]}\n\n")
		_, _ = io.WriteString(w, "data: [DONE]\n\n")
	}))
	defer upstream.Close()

	gatewayServer := newConfiguredTestGateway(t, upstream, map[string]string{"supports_streaming": "true"})
	defer gatewayServer.Close()

	response := doResponsesRequest(t, gatewayServer.URL, "{\"model\":\"public-alias\",\"input\":\"hi\",\"stream\":true}")
	defer response.Body.Close()
	payload, _ := io.ReadAll(response.Body)
	stream := string(payload)
	if response.StatusCode != http.StatusOK || !strings.HasPrefix(response.Header.Get("Content-Type"), "text/event-stream") {
		t.Fatalf("status=%d content-type=%q body=%s", response.StatusCode, response.Header.Get("Content-Type"), stream)
	}
	for _, required := range []string{"event: response.created", "event: response.output_text.delta", "\"delta\":\"hello\"", "event: response.completed"} {
		if !strings.Contains(stream, required) {
			t.Fatalf("stream missing %q: %s", required, stream)
		}
	}
	if strings.Contains(stream, "chat.completion.chunk") {
		t.Fatalf("chat wire format leaked to Responses client: %s", stream)
	}
}

func TestResponsesRejectsPreviousResponseIDBeforeUpstream(t *testing.T) {
	called := false
	upstream := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		called = true
	}))
	defer upstream.Close()

	gatewayServer := newConfiguredTestGateway(t, upstream, nil)
	defer gatewayServer.Close()

	response := doResponsesRequest(t, gatewayServer.URL, "{\"model\":\"public-alias\",\"input\":\"hi\",\"previous_response_id\":\"resp_old\"}")
	response.Body.Close()
	if response.StatusCode != http.StatusBadRequest || called {
		t.Fatalf("status=%d upstream_called=%t", response.StatusCode, called)
	}
}

func doResponsesRequest(t *testing.T, baseURL, body string) *http.Response {
	t.Helper()
	request, err := http.NewRequest(http.MethodPost, baseURL+"/v1/responses", bytes.NewBufferString(body))
	if err != nil {
		t.Fatalf("new request: %v", err)
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Authorization", "Bearer "+testClientToken)
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatalf("gateway request: %v", err)
	}
	return response
}
