// Package gemini implements Google's native Gemini GenerateContent protocol.
package gemini

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
	Name                 = "gemini"
	defaultAPIVersion    = "v1beta"
	maxResponseBodyBytes = 32 << 20
)

type Adapter struct{}

func New() *Adapter { return &Adapter{} }

func (*Adapter) Name() string { return Name }

func (*Adapter) Capabilities(_ context.Context, deployment provider.Deployment) provider.Capabilities {
	return provider.Capabilities{
		Operations:       map[protocol.Operation]bool{protocol.OperationChat: true},
		Protocols:        map[protocol.Protocol]bool{protocol.ProtocolGemini: true},
		Streaming:        capabilityFlag(deployment.Metadata, "supports_streaming", true),
		Tools:            capabilityFlag(deployment.Metadata, "supports_tools", true),
		ParallelTools:    capabilityFlag(deployment.Metadata, "supports_parallel_tools", true),
		MultimodalInput:  capabilityFlag(deployment.Metadata, "supports_multimodal_input", true),
		Reasoning:        capabilityFlag(deployment.Metadata, "supports_reasoning", true),
		StructuredOutput: capabilityFlag(deployment.Metadata, "supports_structured_output", true),
	}
}

func (a *Adapter) BuildRequest(ctx context.Context, req *protocol.CanonicalRequest, deployment provider.Deployment, credential provider.Credential) (*http.Request, error) {
	if req == nil {
		return nil, errors.New("build Gemini request: request is nil")
	}
	if req.Operation != protocol.OperationChat {
		return nil, fmt.Errorf("build Gemini request: unsupported operation %q", req.Operation)
	}
	upstreamModel := strings.TrimSpace(deployment.Metadata["upstream_model"])
	if upstreamModel == "" {
		upstreamModel = req.Model
	}
	if strings.TrimSpace(upstreamModel) == "" {
		return nil, errors.New("build Gemini request: model is empty")
	}

	var body []byte
	var err error
	if req.Protocol == protocol.ProtocolGemini && len(req.RawBody) > 0 {
		body = append([]byte(nil), req.RawBody...)
	} else {
		body, err = encodeGenerateContentRequest(req)
		if err != nil {
			return nil, fmt.Errorf("build Gemini request body: %w", err)
		}
	}

	endpoint, err := generateContentEndpoint(deployment, upstreamModel, req.Stream)
	if err != nil {
		return nil, err
	}
	httpRequest, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("build Gemini HTTP request: %w", err)
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
	applyCredential(httpRequest.Header, deployment, credential)
	return httpRequest, nil
}

func (a *Adapter) DecodeResponse(ctx context.Context, resp *http.Response) (*protocol.CanonicalResponse, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if resp == nil {
		return nil, errors.New("decode Gemini response: response is nil")
	}
	body, err := readLimited(resp.Body, maxResponseBodyBytes)
	if err != nil {
		return nil, fmt.Errorf("read Gemini response: %w", err)
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, a.NormalizeError(ctx, resp, body)
	}
	return decodeGenerateContentResponse(body)
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
		Retryable:  status == http.StatusRequestTimeout || status == http.StatusConflict || status == http.StatusTooManyRequests || status >= 500,
		Raw:        append(json.RawMessage(nil), body...),
	}
	var envelope struct {
		Error struct {
			Code    int    `json:"code"`
			Message string `json:"message"`
			Status  string `json:"status"`
		} `json:"error"`
	}
	if json.Unmarshal(body, &envelope) == nil {
		if envelope.Error.Status != "" {
			result.Code = strings.ToLower(envelope.Error.Status)
			result.ProviderCode = envelope.Error.Status
		}
		if envelope.Error.Message != "" {
			result.Message = envelope.Error.Message
		}
	}
	if result.Message == "" {
		result.Message = "Gemini upstream request failed"
	}
	return result
}

func generateContentEndpoint(deployment provider.Deployment, model string, stream bool) (string, error) {
	base := strings.TrimSpace(deployment.BaseURL)
	if base == "" {
		return "", errors.New("build Gemini request: deployment base URL is empty")
	}
	parsed, err := url.Parse(base)
	if err != nil {
		return "", fmt.Errorf("build Gemini request: invalid base URL: %w", err)
	}
	if parsed.Scheme != "http" && parsed.Scheme != "https" {
		return "", fmt.Errorf("build Gemini request: unsupported URL scheme %q", parsed.Scheme)
	}
	if parsed.Host == "" {
		return "", errors.New("build Gemini request: base URL host is empty")
	}
	if parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" {
		return "", errors.New("build Gemini request: base URL must not contain user info, query, or fragment")
	}
	version := strings.Trim(strings.TrimSpace(deployment.Metadata["api_version"]), "/")
	if version == "" {
		version = defaultAPIVersion
	}
	basePath := strings.TrimRight(parsed.Path, "/")
	if strings.HasSuffix(basePath, "/"+version) {
		basePath = strings.TrimSuffix(basePath, "/"+version)
	}
	action := "generateContent"
	if stream {
		action = "streamGenerateContent"
	}
	parsed.Path = basePath + "/" + version + "/models/" + url.PathEscape(strings.TrimPrefix(model, "models/")) + ":" + action
	if stream {
		query := parsed.Query()
		query.Set("alt", "sse")
		parsed.RawQuery = query.Encode()
	}
	return parsed.String(), nil
}

func applyCredential(headers http.Header, deployment provider.Deployment, credential provider.Credential) {
	if credential.Secret == "" {
		return
	}
	headerName := strings.TrimSpace(deployment.Metadata["auth_header"])
	if headerName == "" {
		headerName = "x-goog-api-key"
	}
	if headers.Get(headerName) != "" {
		return
	}
	scheme := strings.TrimSpace(deployment.Metadata["auth_scheme"])
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
