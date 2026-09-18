package gateway

import (
	"bytes"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/execution"
	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/provider"
	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/provider/openaicompat"
	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/router"
)

const testClientToken = "client-secret"

func TestChatCompletionsEndToEndPreservesCompatibleFields(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		if got := request.Header.Get("Authorization"); got != "Bearer upstream-secret" {
			t.Errorf("upstream Authorization = %q", got)
		}
		body, err := io.ReadAll(request.Body)
		if err != nil {
			t.Errorf("read upstream body: %v", err)
			return
		}
		var object map[string]json.RawMessage
		if err := json.Unmarshal(body, &object); err != nil {
			t.Errorf("decode upstream body: %v", err)
			return
		}
		var model string
		_ = json.Unmarshal(object["model"], &model)
		if model != "real-model" || string(object["vendor_request"]) != `{"keep":true}` {
			t.Errorf("upstream request model=%q vendor=%s", model, object["vendor_request"])
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"id":"chatcmpl_e2e","object":"chat.completion","model":"real-model","choices":[{"index":0,"message":{"role":"assistant","content":"hello"},"finish_reason":"stop"}],"usage":{"prompt_tokens":2,"completion_tokens":1,"total_tokens":3},"vendor_response":{"keep":true}}`)
	}))
	defer upstream.Close()
	gatewayServer := newConfiguredTestGateway(t, upstream, nil)
	defer gatewayServer.Close()

	response := doGatewayRequest(t, gatewayServer.URL, `{"model":"public-alias","messages":[{"role":"user","content":"hi"}],"vendor_request":{"keep":true}}`, testClientToken)
	defer response.Body.Close()
	payload, _ := io.ReadAll(response.Body)
	if response.StatusCode != http.StatusOK {
		t.Fatalf("gateway status=%d body=%s", response.StatusCode, payload)
	}
	var object map[string]json.RawMessage
	if err := json.Unmarshal(payload, &object); err != nil {
		t.Fatalf("decode gateway response: %v", err)
	}
	if got := string(object["vendor_response"]); got != `{"keep":true}` {
		t.Fatalf("vendor_response = %s", got)
	}
	if response.Header.Get("X-Request-ID") == "" {
		t.Fatal("X-Request-ID is empty")
	}
}

func TestChatCompletionsStreamingRelaysEachWireChunkOnce(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = io.WriteString(w, "data: {\"id\":\"chatcmpl_stream\",\"model\":\"real-model\",\"choices\":[{\"index\":0,\"delta\":{\"content\":\"hello\"},\"finish_reason\":\"stop\"}],\"vendor_chunk\":\"once\"}\n\ndata: [DONE]\n\n")
	}))
	defer upstream.Close()
	gatewayServer := newConfiguredTestGateway(t, upstream, map[string]string{"supports_streaming": "true"})
	defer gatewayServer.Close()

	response := doGatewayRequest(t, gatewayServer.URL, `{"model":"public-alias","messages":[{"role":"user","content":"hi"}],"stream":true}`, testClientToken)
	defer response.Body.Close()
	payload, _ := io.ReadAll(response.Body)
	text := string(payload)
	if response.StatusCode != http.StatusOK || !strings.HasPrefix(response.Header.Get("Content-Type"), "text/event-stream") {
		t.Fatalf("status=%d content-type=%q body=%s", response.StatusCode, response.Header.Get("Content-Type"), text)
	}
	if strings.Count(text, `"vendor_chunk":"once"`) != 1 || strings.Count(text, "data: [DONE]") != 1 {
		t.Fatalf("stream duplicated or truncated: %s", text)
	}
}

func TestChatCompletionsFailsClosed(t *testing.T) {
	var upstreamCalled atomic.Bool
	upstream := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		upstreamCalled.Store(true)
	}))
	defer upstream.Close()
	configured := newConfiguredTestGateway(t, upstream, nil)
	defer configured.Close()

	unauthorized := doGatewayRequest(t, configured.URL, `{"model":"public-alias","messages":[{"role":"user","content":"hi"}]}`, "wrong-token")
	unauthorized.Body.Close()
	if unauthorized.StatusCode != http.StatusUnauthorized || upstreamCalled.Load() {
		t.Fatalf("unauthorized status=%d upstream_called=%t", unauthorized.StatusCode, upstreamCalled.Load())
	}

	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	unconfigured := httptest.NewServer(NewServer(logger, nil).Handler())
	defer unconfigured.Close()
	response := doGatewayRequest(t, unconfigured.URL, `{"model":"none","messages":[{"role":"user","content":"hi"}]}`, testClientToken)
	response.Body.Close()
	if response.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("unconfigured status=%d", response.StatusCode)
	}
}

func newConfiguredTestGateway(t *testing.T, upstream *httptest.Server, extraMetadata map[string]string) *httptest.Server {
	t.Helper()
	registry := provider.NewRegistry()
	if err := registry.Register(openaicompat.New()); err != nil {
		t.Fatalf("register adapter: %v", err)
	}
	metadata := map[string]string{"upstream_model": "real-model"}
	for key, value := range extraMetadata {
		metadata[key] = value
	}
	resolver, err := router.NewStaticResolver(router.Plan{
		ModelAlias: "public-alias",
		Targets: []router.Target{{
			Candidate:  router.Candidate{DeploymentID: "dep-test", CredentialID: "cred-test", Weight: 1, Enabled: true, Healthy: true},
			Adapter:    openaicompat.Name,
			Deployment: provider.Deployment{ID: "dep-test", ProviderID: "provider-test", BaseURL: upstream.URL, Metadata: metadata},
			Credential: provider.Credential{ID: "cred-test", Secret: "upstream-secret"},
		}},
	})
	if err != nil {
		t.Fatalf("create resolver: %v", err)
	}
	executor, err := execution.New(registry, resolver, router.NewWeightedSelector(), upstream.Client())
	if err != nil {
		t.Fatalf("create executor: %v", err)
	}
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	server := NewServer(logger, registry, WithInferenceService(executor), WithBearerToken(testClientToken))
	return httptest.NewServer(server.Handler())
}

func doGatewayRequest(t *testing.T, baseURL, body, token string) *http.Response {
	t.Helper()
	request, err := http.NewRequest(http.MethodPost, baseURL+"/v1/chat/completions", bytes.NewBufferString(body))
	if err != nil {
		t.Fatalf("new request: %v", err)
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Authorization", "Bearer "+token)
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatalf("gateway request: %v", err)
	}
	return response
}
