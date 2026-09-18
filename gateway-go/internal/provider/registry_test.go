package provider

import (
	"context"
	"net/http"
	"testing"

	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/protocol"
)

type fakeAdapter struct{ name string }

func (f fakeAdapter) Name() string { return f.name }
func (f fakeAdapter) Capabilities(context.Context, Deployment) Capabilities {
	return Capabilities{}
}
func (f fakeAdapter) BuildRequest(context.Context, *protocol.CanonicalRequest, Deployment, Credential) (*http.Request, error) {
	return nil, nil
}
func (f fakeAdapter) DecodeResponse(context.Context, *http.Response) (*protocol.CanonicalResponse, error) {
	return nil, nil
}
func (f fakeAdapter) DecodeStream(context.Context, *http.Response, func(protocol.StreamEvent) error) error {
	return nil
}
func (f fakeAdapter) NormalizeError(context.Context, *http.Response, []byte) *protocol.GatewayError {
	return nil
}

func TestRegistryRejectsDuplicateAdapterNames(t *testing.T) {
	registry := NewRegistry()
	if err := registry.Register(fakeAdapter{name: "openai"}); err != nil {
		t.Fatalf("first Register() error = %v", err)
	}
	if err := registry.Register(fakeAdapter{name: "openai"}); err == nil {
		t.Fatal("second Register() error = nil, want duplicate error")
	}
}

func TestRegistryNamesAreSorted(t *testing.T) {
	registry := NewRegistry()
	_ = registry.Register(fakeAdapter{name: "gemini"})
	_ = registry.Register(fakeAdapter{name: "anthropic"})

	names := registry.Names()
	if len(names) != 2 || names[0] != "anthropic" || names[1] != "gemini" {
		t.Fatalf("Names() = %#v", names)
	}
}
