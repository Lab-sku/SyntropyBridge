package provider

import (
	"context"
	"net/http"

	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/protocol"
)

// Deployment is one concrete upstream endpoint. Health and circuit state are keyed by
// deployment (and, where necessary, credential), never solely by provider name.
type Deployment struct {
	ID         string
	ProviderID string
	BaseURL    string
	Region     string
	Headers    map[string]string
	Metadata   map[string]string
}

// Credential is a resolved secret reference for one attempt. Secret contains the
// in-memory plaintext only at the adapter boundary and must never be logged.
type Credential struct {
	ID     string
	Secret string
	Fields map[string]string
}

type Capabilities struct {
	Operations       map[protocol.Operation]bool
	Protocols        map[protocol.Protocol]bool
	Streaming        bool
	Tools            bool
	ParallelTools    bool
	MultimodalInput  bool
	Reasoning        bool
	StructuredOutput bool
}

// Adapter converts between the canonical representation and one provider protocol.
// Implementations must be stateless or safe for concurrent use.
type Adapter interface {
	Name() string
	Capabilities(ctx context.Context, deployment Deployment) Capabilities

	BuildRequest(
		ctx context.Context,
		req *protocol.CanonicalRequest,
		deployment Deployment,
		credential Credential,
	) (*http.Request, error)

	DecodeResponse(
		ctx context.Context,
		resp *http.Response,
	) (*protocol.CanonicalResponse, error)

	DecodeStream(
		ctx context.Context,
		resp *http.Response,
		emit func(protocol.StreamEvent) error,
	) error

	NormalizeError(
		ctx context.Context,
		resp *http.Response,
		body []byte,
	) *protocol.GatewayError
}
