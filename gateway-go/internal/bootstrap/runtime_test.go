package bootstrap

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/config"
	openaiwire "github.com/Lab-sku/SyntropyBridge/gateway-go/internal/protocol/openai"
	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/provider"
	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/provider/openaicompat"
)

func TestBuildDisabledReturnsNil(t *testing.T) {
	runtime, err := Build(config.Config{}, provider.NewRegistry())
	if err != nil || runtime != nil {
		t.Fatalf("Build() runtime=%#v error=%v", runtime, err)
	}
}

func TestBuildCreatesCallableExplicitRoute(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		if request.Header.Get("Authorization") != "Bearer upstream-secret" {
			t.Errorf("Authorization = %q", request.Header.Get("Authorization"))
		}
		_, _ = io.WriteString(w, `{"id":"chatcmpl_1","model":"real-model","choices":[{"index":0,"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}]}`)
	}))
	defer upstream.Close()

	registry := provider.NewRegistry()
	if err := registry.Register(openaicompat.New()); err != nil {
		t.Fatalf("Register() error = %v", err)
	}
	cfg := validConfig(upstream.URL)
	runtime, err := Build(cfg, registry)
	if err != nil {
		t.Fatalf("Build() error = %v", err)
	}
	req, err := openaiwire.DecodeChatRequest([]byte(`{"model":"smart-chat","messages":[{"role":"user","content":"hi"}]}`), "req_1")
	if err != nil {
		t.Fatalf("DecodeChatRequest() error = %v", err)
	}
	result, err := runtime.Executor.Complete(context.Background(), req)
	if err != nil {
		t.Fatalf("Complete() error = %v", err)
	}
	if result.Response.Model != "real-model" || runtime.ClientToken != "client-secret-0123456789" {
		t.Fatalf("runtime result=%#v", result)
	}
}

func TestBootstrapHTTPClientDoesNotFollowRedirects(t *testing.T) {
	var redirected atomic.Bool
	target := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		redirected.Store(true)
	}))
	defer target.Close()
	redirector := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Redirect(w, &http.Request{}, target.URL, http.StatusTemporaryRedirect)
	}))
	defer redirector.Close()

	client := newHTTPClient(config.TransportConfig{
		DialTimeout:           time.Second,
		TLSHandshakeTimeout:   time.Second,
		ResponseHeaderTimeout: time.Second,
		IdleConnTimeout:       time.Second,
		MaxIdleConns:          4,
		MaxIdleConnsPerHost:   2,
	})
	response, err := client.Post(redirector.URL, "application/json", strings.NewReader(`{}`))
	if err != nil {
		t.Fatalf("Post() error = %v", err)
	}
	response.Body.Close()
	if response.StatusCode != http.StatusTemporaryRedirect || redirected.Load() {
		t.Fatalf("status=%d redirected=%t", response.StatusCode, redirected.Load())
	}
}

func validConfig(baseURL string) config.Config {
	return config.Config{
		Transport: config.TransportConfig{
			DialTimeout:           time.Second,
			TLSHandshakeTimeout:   time.Second,
			ResponseHeaderTimeout: time.Second,
			IdleConnTimeout:       time.Second,
			MaxIdleConns:          10,
			MaxIdleConnsPerHost:   5,
		},
		Bootstrap: config.BootstrapConfig{
			Enabled:            true,
			ClientToken:        "client-secret-0123456789",
			ModelAlias:         "smart-chat",
			UpstreamBaseURL:    baseURL,
			UpstreamModel:      "real-model",
			UpstreamAPIKey:     "upstream-secret",
			UpstreamAuthHeader: "Authorization",
			UpstreamAuthScheme: "Bearer",
			UpstreamChatPath:   "/v1/chat/completions",
		},
	}
}
