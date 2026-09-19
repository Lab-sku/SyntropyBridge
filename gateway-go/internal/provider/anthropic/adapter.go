// Package anthropic implements Anthropic's native Messages protocol as an upstream
// provider adapter. It accepts the gateway's canonical representation, so OpenAI
// clients can be routed to Anthropic without flattening tool, image, reasoning, or
// structured-output fields.
package anthropic

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"

	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/protocol"
	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/provider"
)

const (
	Name                 = "anthropic"
	defaultMessagesPath  = "/v1/messages"
	defaultAPIVersion    = "2023-06-01"
	defaultMaxOutput     = 1024
	maxResponseBodyBytes = 32 << 20
)

type Adapter struct{}

func New() *Adapter { return &Adapter{} }

func (*Adapter) Name() string { return Name }

func (*Adapter) Capabilities(_ context.Context, deployment provider.Deployment) provider.Capabilities {
	return provider.Capabilities{
		Operations:       map[protocol.Operation]bool{protocol.OperationChat: true},
		Protocols:        map[protocol.Protocol]bool{protocol.ProtocolAnthropic: true},
		Streaming:        capabilityFlag(deployment.Metadata, "supports_streaming", true),
		Tools:            capabilityFlag(deployment.Metadata, "supports_tools", true),
		ParallelTools:    capabilityFlag(deployment.Metadata, "supports_parallel_tools", true),
		MultimodalInput:  capabilityFlag(deployment.Metadata, "supports_multimodal_input", true),
		Reasoning:        capabilityFlag(deployment.Metadata, "supports_reasoning", false),
		StructuredOutput: capabilityFlag(deployment.Metadata, "supports_structured_output", false),
	}
}

func (a *Adapter) BuildRequest(
	ctx context.Context,
	req *protocol.CanonicalRequest,
	deployment provider.Deployment,
	credential provider.Credential,
) (*http.Request, error) {
	if req == nil {
		return nil, errors.New("build Anthropic request: request is nil")
	}
	if req.Operation != protocol.OperationChat {
		return nil, fmt.Errorf("build Anthropic request: unsupported operation %q", req.Operation)
	}
	if strings.TrimSpace(req.Model) == "" {
		return nil, errors.New("build Anthropic request: model is empty")
	}

	upstreamModel := strings.TrimSpace(deployment.Metadata["upstream_model"])
	if upstreamModel == "" {
		upstreamModel = req.Model
	}

	var body []byte
	var err error
	if req.Protocol == protocol.ProtocolAnthropic && len(req.RawBody) > 0 {
		body, err = replaceModelAndStream(req.RawBody, upstreamModel, req.Stream)
	} else {
		body, err = encodeMessagesRequest(req, upstreamModel, deployment.Metadata)
	}
	if err != nil {
		return nil, fmt.Errorf("build Anthropic request body: %w", err)
	}

	endpoint, err := messagesEndpoint(deployment)
	if err != nil {
		return nil, err
	}
	httpRequest, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("build Anthropic HTTP request: %w", err)
	}
	httpRequest.Header.Set("Content-Type", "application/json")
	if req.Stream {
		httpRequest.Header.Set("Accept", "text/event-stream")
	} else {
		httpRequest.Header.Set("Accept", "application/json")
	}
	for key, value := range deployment.Headers {
		if strings.TrimSpace(key) != "" {
			httpRequest.Header.Set(key, value)
		}
	}
	if httpRequest.Header.Get("anthropic-version") == "" {
		version := strings.TrimSpace(deployment.Metadata["anthropic_version"])
		if version == "" {
			version = defaultAPIVersion
		}
		httpRequest.Header.Set("anthropic-version", version)
	}
	if beta := strings.TrimSpace(deployment.Metadata["anthropic_beta"]); beta != "" && httpRequest.Header.Get("anthropic-beta") == "" {
		httpRequest.Header.Set("anthropic-beta", beta)
	}
	applyCredential(httpRequest.Header, deployment, credential)
	return httpRequest, nil
}

func (a *Adapter) DecodeResponse(ctx context.Context, resp *http.Response) (*protocol.CanonicalResponse, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if resp == nil {
		return nil, errors.New("decode Anthropic response: response is nil")
	}
	body, err := readLimited(resp.Body, maxResponseBodyBytes)
	if err != nil {
		return nil, fmt.Errorf("read Anthropic response: %w", err)
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, a.NormalizeError(ctx, resp, body)
	}
	return decodeMessagesResponse(body)
}

func (*Adapter) NormalizeError(_ context.Context, resp *http.Response, body []byte) *protocol.GatewayError {
	status := 0
	if resp != nil {
		status = resp.StatusCode
	}
	result := &protocol.GatewayError{
		Code:       "upstream_error",
		Message:    http.StatusText(status),
		HTTPStatus: status,
		Retryable: status == http.StatusRequestTimeout ||
			status == http.StatusConflict ||
			status == http.StatusTooManyRequests ||
			status == http.StatusGatewayTimeout ||
			status == 529 ||
			status >= 500,
		Raw: append(json.RawMessage(nil), body...),
	}
	var envelope struct {
		Type  string `json:"type"`
		Error struct {
			Type    string `json:"type"`
			Message string `json:"message"`
		} `json:"error"`
		RequestID string `json:"request_id"`
	}
	if json.Unmarshal(body, &envelope) == nil {
		if envelope.Error.Type != "" {
			result.Code = envelope.Error.Type
			result.ProviderCode = envelope.Error.Type
		}
		if envelope.Error.Message != "" {
			result.Message = envelope.Error.Message
		}
	}
	if result.Message == "" {
		result.Message = "Anthropic upstream request failed"
	}
	return result
}

func messagesEndpoint(deployment provider.Deployment) (string, error) {
	base := strings.TrimSpace(deployment.BaseURL)
	if base == "" {
		return "", errors.New("build Anthropic request: deployment base URL is empty")
	}
	parsed, err := url.Parse(base)
	if err != nil {
		return "", fmt.Errorf("build Anthropic request: invalid base URL: %w", err)
	}
	if parsed.Scheme != "http" && parsed.Scheme != "https" {
		return "", fmt.Errorf("build Anthropic request: unsupported URL scheme %q", parsed.Scheme)
	}
	if parsed.Host == "" {
		return "", errors.New("build Anthropic request: base URL host is empty")
	}
	if parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" {
		return "", errors.New("build Anthropic request: base URL must not contain user info, query, or fragment")
	}
	path := strings.TrimSpace(deployment.Metadata["messages_path"])
	if path == "" {
		path = defaultMessagesPath
	}
	if !strings.HasPrefix(path, "/") {
		path = "/" + path
	}
	basePath := strings.TrimRight(parsed.Path, "/")
	if strings.HasSuffix(basePath, "/v1") && strings.HasPrefix(path, "/v1/") {
		path = strings.TrimPrefix(path, "/v1")
	}
	parsed.Path = basePath + path
	return parsed.String(), nil
}

func applyCredential(headers http.Header, deployment provider.Deployment, credential provider.Credential) {
	if credential.Secret == "" {
		return
	}
	headerName := strings.TrimSpace(deployment.Metadata["auth_header"])
	if headerName == "" {
		headerName = "x-api-key"
	}
	if headers.Get(headerName) != "" {
		return
	}
	scheme := strings.TrimSpace(deployment.Metadata["auth_scheme"])
	if scheme == "" && strings.EqualFold(headerName, "Authorization") {
		scheme = "Bearer"
	}
	value := credential.Secret
	if scheme != "" && !strings.EqualFold(scheme, "none") {
		value = scheme + " " + value
	}
	headers.Set(headerName, value)
}

func capabilityFlag(metadata map[string]string, key string, fallback bool) bool {
	value, ok := metadata[key]
	if !ok || strings.TrimSpace(value) == "" {
		return fallback
	}
	parsed, err := strconv.ParseBool(value)
	if err != nil {
		return fallback
	}
	return parsed
}

func readLimited(reader io.Reader, limit int64) ([]byte, error) {
	limited := io.LimitReader(reader, limit+1)
	body, err := io.ReadAll(limited)
	if err != nil {
		return nil, err
	}
	if int64(len(body)) > limit {
		return nil, fmt.Errorf("body exceeds %d bytes", limit)
	}
	return body, nil
}
