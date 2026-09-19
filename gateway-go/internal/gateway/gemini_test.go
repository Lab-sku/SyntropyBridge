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

func TestGeminiGenerateContentEndToEndThroughOpenAICompatibleUpstream(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		body, _ := io.ReadAll(request.Body)
		var object map[string]json.RawMessage
		if err := json.Unmarshal(body, &object); err != nil {
			t.Fatalf("invalid upstream JSON: %v body=%s", err, body)
		}
		var model string
		_ = json.Unmarshal(object["model"], &model)
		if model != "real-model" || object["messages"] == nil {
			t.Fatalf("upstream body = %s", body)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, "{\"id\":\"chatcmpl_g\",\"model\":\"real-model\",\"choices\":[{\"index\":0,\"message\":{\"role\":\"assistant\",\"content\":\"hello\"},\"finish_reason\":\"stop\"}],\"usage\":{\"prompt_tokens\":2,\"completion_tokens\":1,\"total_tokens\":3}}")
	}))
	defer upstream.Close()

	gatewayServer := newConfiguredTestGateway(t, upstream, nil)
	defer gatewayServer.Close()

	response := doGeminiRequest(
		t,
		gatewayServer.URL,
		"/v1beta/models/public-alias:generateContent",
		"{\"contents\":[{\"role\":\"user\",\"parts\":[{\"text\":\"hi\"}]}]}",
	)
	defer response.Body.Close()
	payload, _ := io.ReadAll(response.Body)
	if response.StatusCode != http.StatusOK {
		t.Fatalf("status=%d body=%s", response.StatusCode, payload)
	}
	if !strings.Contains(string(payload), "\"modelVersion\":\"public-alias\"") {
		t.Fatalf("public model alias not preserved: %s", payload)
	}
	if !strings.Contains(string(payload), "\"text\":\"hello\"") {
		t.Fatalf("assistant text missing: %s", payload)
	}
}

func TestGeminiStreamGenerateContentTranslatesChatSSE(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = io.WriteString(w, "data: {\"id\":\"chatcmpl_stream\",\"model\":\"real-model\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\"},\"finish_reason\":null}]}\n\n")
		_, _ = io.WriteString(w, "data: {\"id\":\"chatcmpl_stream\",\"model\":\"real-model\",\"choices\":[{\"index\":0,\"delta\":{\"content\":\"hello\"},\"finish_reason\":\"stop\"}]}\n\n")
		_, _ = io.WriteString(w, "data: [DONE]\n\n")
	}))
	defer upstream.Close()

	gatewayServer := newConfiguredTestGateway(t, upstream, map[string]string{"supports_streaming": "true"})
	defer gatewayServer.Close()

	response := doGeminiRequest(
		t,
		gatewayServer.URL,
		"/v1beta/models/public-alias:streamGenerateContent?alt=sse",
		"{\"contents\":[{\"role\":\"user\",\"parts\":[{\"text\":\"hi\"}]}]}",
	)
	defer response.Body.Close()
	payload, _ := io.ReadAll(response.Body)
	stream := string(payload)
	if response.StatusCode != http.StatusOK || !strings.HasPrefix(response.Header.Get("Content-Type"), "text/event-stream") {
		t.Fatalf("status=%d content-type=%q body=%s", response.StatusCode, response.Header.Get("Content-Type"), stream)
	}
	if !strings.Contains(stream, "\"text\":\"hello\"") {
		t.Fatalf("translated text missing: %s", stream)
	}
	if !strings.Contains(stream, "\"finishReason\":\"STOP\"") {
		t.Fatalf("Gemini finish reason missing: %s", stream)
	}
	if strings.Contains(stream, "[DONE]") || strings.Contains(stream, "chat.completion.chunk") {
		t.Fatalf("OpenAI wire format leaked to Gemini client: %s", stream)
	}
}

func TestGeminiClientAcceptsGoogleAPIKeyHeader(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, "{\"id\":\"chatcmpl_g\",\"model\":\"real-model\",\"choices\":[{\"index\":0,\"message\":{\"role\":\"assistant\",\"content\":\"ok\"},\"finish_reason\":\"stop\"}]}")
	}))
	defer upstream.Close()

	gatewayServer := newConfiguredTestGateway(t, upstream, nil)
	defer gatewayServer.Close()

	request, err := http.NewRequest(
		http.MethodPost,
		gatewayServer.URL+"/v1beta/models/public-alias:generateContent",
		bytes.NewBufferString("{\"contents\":[{\"role\":\"user\",\"parts\":[{\"text\":\"hi\"}]}]}"),
	)
	if err != nil {
		t.Fatalf("new request: %v", err)
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("x-goog-api-key", testClientToken)
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatalf("gateway request: %v", err)
	}
	response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", response.StatusCode)
	}
}

func doGeminiRequest(t *testing.T, baseURL, path, body string) *http.Response {
	t.Helper()
	request, err := http.NewRequest(http.MethodPost, baseURL+path, bytes.NewBufferString(body))
	if err != nil {
		t.Fatalf("new request: %v", err)
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("x-goog-api-key", testClientToken)
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatalf("gateway request: %v", err)
	}
	return response
}
