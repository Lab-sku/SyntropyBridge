package execution

import (
	"context"
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"

	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/protocol"
	openaiwire "github.com/Lab-sku/SyntropyBridge/gateway-go/internal/protocol/openai"
	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/provider"
	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/provider/openaicompat"
	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/router"
)

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) Do(request *http.Request) (*http.Response, error) { return f(request) }

func TestExecutorCompleteRoutesAndDecodes(t *testing.T) {
	executor := newTestExecutor(t, map[string]string{}, roundTripFunc(func(request *http.Request) (*http.Response, error) {
		if request.URL.String() != "https://upstream.invalid/v1/chat/completions" {
			t.Fatalf("upstream URL = %q", request.URL.String())
		}
		return &http.Response{
			StatusCode: http.StatusOK,
			Body: io.NopCloser(strings.NewReader(`{
				"id":"chatcmpl_test","model":"real-model","created":1,
				"choices":[{"index":0,"message":{"role":"assistant","content":"hello"},"finish_reason":"stop"}],
				"usage":{"prompt_tokens":2,"completion_tokens":1,"total_tokens":3}
			}`)),
		}, nil
	}))

	req, err := openaiwire.DecodeChatRequest([]byte(`{"model":"alias","messages":[{"role":"user","content":"hi"}]}`), "req_test")
	if err != nil {
		t.Fatalf("DecodeChatRequest() error = %v", err)
	}
	result, err := executor.Complete(context.Background(), req)
	if err != nil {
		t.Fatalf("Complete() error = %v", err)
	}
	if result.Response.RequestID != "req_test" || result.Attempt.DeploymentID != "dep-1" {
		t.Fatalf("result = %#v", result)
	}
	if result.Response.Usage == nil || result.Response.Usage.TotalTokens != 3 {
		t.Fatalf("usage = %#v", result.Response.Usage)
	}
}

func TestExecutorRejectsUndeclaredCapabilityBeforeNetwork(t *testing.T) {
	called := false
	executor := newTestExecutor(t, map[string]string{}, roundTripFunc(func(*http.Request) (*http.Response, error) {
		called = true
		return nil, errors.New("must not be called")
	}))
	req, err := openaiwire.DecodeChatRequest([]byte(`{
		"model":"alias",
		"messages":[{"role":"user","content":"hi"}],
		"tools":[{"type":"function","function":{"name":"weather","parameters":{"type":"object"}}}]
	}`), "req_tools")
	if err != nil {
		t.Fatalf("DecodeChatRequest() error = %v", err)
	}
	_, err = executor.Complete(context.Background(), req)
	if !errors.Is(err, ErrCapabilityUnsupported) {
		t.Fatalf("Complete() error = %v, want ErrCapabilityUnsupported", err)
	}
	if called {
		t.Fatal("network was called despite failed capability check")
	}
}

func TestExecutorStreamPropagatesSemanticEvents(t *testing.T) {
	executor := newTestExecutor(t, map[string]string{"supports_streaming": "true"}, roundTripFunc(func(*http.Request) (*http.Response, error) {
		return &http.Response{
			StatusCode: http.StatusOK,
			Body: io.NopCloser(strings.NewReader(strings.Join([]string{
				`data: {"id":"chatcmpl_test","model":"real-model","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}`,
				"",
				`data: {"id":"chatcmpl_test","model":"real-model","choices":[{"index":0,"delta":{"content":"hello"},"finish_reason":"stop"}]}`,
				"",
				"data: [DONE]",
				"",
			}, "\n"))),
		}, nil
	}))
	req, err := openaiwire.DecodeChatRequest([]byte(`{"model":"alias","messages":[{"role":"user","content":"hi"}],"stream":true}`), "req_stream")
	if err != nil {
		t.Fatalf("DecodeChatRequest() error = %v", err)
	}
	var events []protocol.StreamEvent
	result, err := executor.Stream(context.Background(), req, func(event protocol.StreamEvent) error {
		events = append(events, event)
		return nil
	})
	if err != nil {
		t.Fatalf("Stream() error = %v", err)
	}
	if result.Attempt.HTTPStatus != http.StatusOK || len(events) == 0 {
		t.Fatalf("stream result = %#v events=%#v", result, events)
	}
}

func newTestExecutor(t *testing.T, metadata map[string]string, client HTTPDoer) *Executor {
	t.Helper()
	registry := provider.NewRegistry()
	if err := registry.Register(openaicompat.New()); err != nil {
		t.Fatalf("Register() error = %v", err)
	}
	deploymentMetadata := map[string]string{"upstream_model": "real-model"}
	for key, value := range metadata {
		deploymentMetadata[key] = value
	}
	resolver, err := router.NewStaticResolver(router.Plan{
		ModelAlias: "alias",
		Targets: []router.Target{{
			Candidate: router.Candidate{DeploymentID: "dep-1", CredentialID: "cred-1", Weight: 1, Enabled: true, Healthy: true},
			Adapter:   openaicompat.Name,
			Deployment: provider.Deployment{
				ID:       "dep-1",
				BaseURL:  "https://upstream.invalid",
				Metadata: deploymentMetadata,
			},
			Credential: provider.Credential{ID: "cred-1", Secret: "secret"},
		}},
	})
	if err != nil {
		t.Fatalf("NewStaticResolver() error = %v", err)
	}
	executor, err := New(registry, resolver, router.NewWeightedSelector(), client)
	if err != nil {
		t.Fatalf("New() error = %v", err)
	}
	return executor
}
