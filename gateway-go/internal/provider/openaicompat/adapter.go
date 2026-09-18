// Package openaicompat implements the OpenAI Chat Completions protocol family used by
// OpenAI and many compatible providers. It deliberately identifies a protocol family,
// not a vendor inferred from a model-name prefix.
package openaicompat

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"strings"

	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/protocol"
	openaiwire "github.com/Lab-sku/SyntropyBridge/gateway-go/internal/protocol/openai"
	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/provider"
)

const (
	Name                 = "openai-compatible"
	defaultChatPath      = "/v1/chat/completions"
	maxResponseBodyBytes = 32 << 20
)

type Adapter struct{}

func New() *Adapter { return &Adapter{} }

func (*Adapter) Name() string { return Name }

func (*Adapter) Capabilities(_ context.Context, deployment provider.Deployment) provider.Capabilities {
	return provider.Capabilities{
		Operations:       map[protocol.Operation]bool{protocol.OperationChat: true},
		Protocols:        map[protocol.Protocol]bool{protocol.ProtocolOpenAIChat: true},
		Streaming:        capabilityFlag(deployment.Metadata, "supports_streaming", false),
		Tools:            capabilityFlag(deployment.Metadata, "supports_tools", false),
		ParallelTools:    capabilityFlag(deployment.Metadata, "supports_parallel_tools", false),
		MultimodalInput:  capabilityFlag(deployment.Metadata, "supports_multimodal_input", false),
		Reasoning:        capabilityFlag(deployment.Metadata, "supports_reasoning", false),
		StructuredOutput: capabilityFlag(deployment.Metadata, "supports_structured_output", false),
	}
}

func (*Adapter) BuildRequest(
	ctx context.Context,
	req *protocol.CanonicalRequest,
	deployment provider.Deployment,
	credential provider.Credential,
) (*http.Request, error) {
	if req == nil {
		return nil, errors.New("build OpenAI-compatible request: request is nil")
	}
	if req.Operation != protocol.OperationChat {
		return nil, fmt.Errorf("build OpenAI-compatible request: unsupported operation %q", req.Operation)
	}

	upstreamModel := deployment.Metadata["upstream_model"]
	if upstreamModel == "" {
		upstreamModel = req.Model
	}

	var body []byte
	var err error
	if req.Protocol == protocol.ProtocolOpenAIChat && len(req.RawBody) > 0 {
		body, err = openaiwire.ReplaceModelAndStream(req.RawBody, upstreamModel, req.Stream)
	} else {
		copyReq := *req
		copyReq.Model = upstreamModel
		body, err = openaiwire.EncodeChatRequest(&copyReq)
	}
	if err != nil {
		return nil, fmt.Errorf("build OpenAI-compatible request body: %w", err)
	}

	endpoint, err := chatEndpoint(deployment)
	if err != nil {
		return nil, err
	}
	httpRequest, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("build OpenAI-compatible HTTP request: %w", err)
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

func (*Adapter) DecodeResponse(ctx context.Context, resp *http.Response) (*protocol.CanonicalResponse, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if resp == nil {
		return nil, errors.New("decode OpenAI-compatible response: response is nil")
	}
	body, err := readLimited(resp.Body, maxResponseBodyBytes)
	if err != nil {
		return nil, fmt.Errorf("read OpenAI-compatible response: %w", err)
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, New().NormalizeError(ctx, resp, body)
	}

	var envelope chatResponse
	if err := json.Unmarshal(body, &envelope); err != nil {
		return nil, fmt.Errorf("decode OpenAI-compatible response JSON: %w", err)
	}
	result := &protocol.CanonicalResponse{
		ResponseID: envelope.ID,
		Model:      envelope.Model,
		CreatedAt:  envelope.Created,
		RawBody:    append(json.RawMessage(nil), body...),
	}
	for _, choice := range envelope.Choices {
		message, err := openaiwire.DecodeMessage(choice.Message)
		if err != nil {
			return nil, fmt.Errorf("decode OpenAI-compatible choice %d: %w", choice.Index, err)
		}
		result.Messages = append(result.Messages, message)
		if result.FinishReason == "" && choice.FinishReason != nil {
			result.FinishReason = *choice.FinishReason
		}
	}
	if envelope.Usage != nil {
		result.Usage = envelope.Usage.canonical()
	}
	return result, nil
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
			Message string          `json:"message"`
			Type    string          `json:"type"`
			Code    json.RawMessage `json:"code"`
		} `json:"error"`
	}
	if json.Unmarshal(body, &envelope) == nil {
		if envelope.Error.Message != "" {
			result.Message = envelope.Error.Message
		}
		if envelope.Error.Type != "" {
			result.ProviderCode = envelope.Error.Type
		}
		if len(envelope.Error.Code) > 0 && !bytes.Equal(envelope.Error.Code, []byte("null")) {
			var code string
			if json.Unmarshal(envelope.Error.Code, &code) == nil && code != "" {
				result.Code = code
			}
		}
	}
	if result.Message == "" {
		result.Message = "upstream request failed"
	}
	return result
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
