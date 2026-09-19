package gateway

import (
	"context"
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/execution"
	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/protocol"
	anthropicwire "github.com/Lab-sku/SyntropyBridge/gateway-go/internal/protocol/anthropic"
	geminiwire "github.com/Lab-sku/SyntropyBridge/gateway-go/internal/protocol/gemini"
	openaiwire "github.com/Lab-sku/SyntropyBridge/gateway-go/internal/protocol/openai"
	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/provider"
	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/router"
)

const defaultMaxRequestBytes int64 = 16 << 20

type InferenceService interface {
	Complete(ctx context.Context, req *protocol.CanonicalRequest) (*execution.Result, error)
	Stream(ctx context.Context, req *protocol.CanonicalRequest, emit func(protocol.StreamEvent) error) (*execution.StreamResult, error)
}

type Authenticator interface {
	Authenticate(request *http.Request) error
}

type Option func(*Server)

func WithInferenceService(service InferenceService) Option {
	return func(server *Server) { server.inference = service }
}

func WithAuthenticator(authenticator Authenticator) Option {
	return func(server *Server) { server.authenticator = authenticator }
}

func WithBearerToken(token string) Option {
	return func(server *Server) {
		if token != "" {
			server.authenticator = bearerAuthenticator{token: []byte(token)}
		}
	}
}

func WithMaxRequestBytes(limit int64) Option {
	return func(server *Server) {
		if limit > 0 {
			server.maxRequestBytes = limit
		}
	}
}

type Server struct {
	logger          *slog.Logger
	registry        *provider.Registry
	inference       InferenceService
	authenticator   Authenticator
	maxRequestBytes int64
	started         time.Time
}

func NewServer(logger *slog.Logger, registry *provider.Registry, options ...Option) *Server {
	if logger == nil {
		logger = slog.Default()
	}
	if registry == nil {
		registry = provider.NewRegistry()
	}
	server := &Server{
		logger:          logger,
		registry:        registry,
		maxRequestBytes: defaultMaxRequestBytes,
		started:         time.Now().UTC(),
	}
	for _, option := range options {
		if option != nil {
			option(server)
		}
	}
	return server
}

func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.handleHealth)
	mux.HandleFunc("GET /readyz", s.handleReady)
	mux.HandleFunc("GET /v1/gateway/capabilities", s.handleCapabilities)
	mux.HandleFunc("POST /v1/chat/completions", s.handleChatCompletions)
	mux.HandleFunc("POST /v1/responses", s.handleResponses)
	mux.HandleFunc("POST /v1/messages", s.handleAnthropicMessages)
	mux.HandleFunc("POST /v1beta/models/{gemini_action...}", s.handleGeminiGenerateContent)
	mux.HandleFunc("POST /v1/models/{gemini_action...}", s.handleGeminiGenerateContent)
	return requestLogMiddleware(s.logger, mux)
}

func (s *Server) inferenceReady() bool {
	return s.inference != nil && s.authenticator != nil
}

func (s *Server) handleHealth(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"status":     "ok",
		"started_at": s.started,
	})
}

func (s *Server) handleReady(w http.ResponseWriter, _ *http.Request) {
	if !s.inferenceReady() {
		writeJSON(w, http.StatusServiceUnavailable, map[string]any{
			"status":          "not_ready",
			"inference_ready": false,
		})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"status":          "ready",
		"inference_ready": true,
	})
}

func (s *Server) handleCapabilities(w http.ResponseWriter, _ *http.Request) {
	ready := s.inferenceReady()
	note := "protocol and adapter contracts are available; inference requires an injected route resolver and authenticator"
	if ready {
		note = "inference service and authentication are configured"
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"protocols": []protocol.Protocol{
			protocol.ProtocolOpenAIChat,
			protocol.ProtocolOpenAIResponses,
			protocol.ProtocolAnthropic,
			protocol.ProtocolGemini,
		},
		"registered_adapters": s.registry.Names(),
		"inference_ready":     ready,
		"note":                note,
	})
}

func (s *Server) handleChatCompletions(w http.ResponseWriter, request *http.Request) {
	requestID := requestIDFromHeader(request.Header.Get("X-Request-ID"))
	w.Header().Set("X-Request-ID", requestID)

	if !s.inferenceReady() {
		writeOpenAIError(w, http.StatusServiceUnavailable, "gateway_not_ready", "gateway inference is not configured", "server_error")
		return
	}
	if err := s.authenticator.Authenticate(request); err != nil {
		writeOpenAIError(w, http.StatusUnauthorized, "invalid_api_key", "invalid authentication credentials", "authentication_error")
		return
	}

	request.Body = http.MaxBytesReader(w, request.Body, s.maxRequestBytes)
	body, err := io.ReadAll(request.Body)
	if err != nil {
		var maxBytesError *http.MaxBytesError
		if errors.As(err, &maxBytesError) {
			writeOpenAIError(w, http.StatusRequestEntityTooLarge, "request_too_large", "request body exceeds the configured limit", "invalid_request_error")
		} else {
			writeOpenAIError(w, http.StatusBadRequest, "request_read_error", "gateway could not read the request body", "invalid_request_error")
		}
		return
	}
	canonical, err := openaiwire.DecodeChatRequest(body, requestID)
	if err != nil {
		writeOpenAIError(w, http.StatusBadRequest, "invalid_request", "invalid Chat Completions request", "invalid_request_error")
		return
	}

	if canonical.Stream {
		s.handleChatStream(w, request, canonical)
		return
	}
	result, err := s.inference.Complete(request.Context(), canonical)
	if err != nil {
		s.writeExecutionError(w, err)
		return
	}
	if result == nil || result.Response == nil {
		writeOpenAIError(w, http.StatusBadGateway, "empty_upstream_response", "the upstream provider returned no response", "server_error")
		return
	}

	responseBody := result.Response.RawBody
	if len(responseBody) == 0 || !json.Valid(responseBody) {
		responseBody, err = openaiwire.EncodeChatResponse(result.Response)
		if err != nil {
			s.logger.ErrorContext(request.Context(), "encode chat response", "request_id", requestID, "error", err)
			writeOpenAIError(w, http.StatusInternalServerError, "response_encoding_error", "gateway could not encode the upstream response", "server_error")
			return
		}
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(responseBody)
}

func (s *Server) handleChatStream(w http.ResponseWriter, request *http.Request, canonical *protocol.CanonicalRequest) {
	flusher, ok := w.(http.Flusher)
	if !ok {
		writeOpenAIError(w, http.StatusInternalServerError, "streaming_unsupported", "HTTP streaming is unavailable", "server_error")
		return
	}

	encoder := openaiwire.NewChatStreamEncoder()
	started := false
	writeFrame := func(frame []byte) error {
		if !started {
			w.Header().Set("Content-Type", "text/event-stream; charset=utf-8")
			w.Header().Set("Cache-Control", "no-cache")
			w.Header().Set("X-Accel-Buffering", "no")
			w.WriteHeader(http.StatusOK)
			started = true
		}
		if _, err := w.Write(frame); err != nil {
			return err
		}
		flusher.Flush()
		return nil
	}

	_, err := s.inference.Stream(request.Context(), canonical, func(event protocol.StreamEvent) error {
		frames, encodeErr := encoder.Encode(event)
		if encodeErr != nil {
			return encodeErr
		}
		for _, frame := range frames {
			if err := writeFrame(frame); err != nil {
				return err
			}
		}
		return nil
	})
	if err == nil {
		if !started {
			writeOpenAIError(w, http.StatusBadGateway, "empty_upstream_stream", "the upstream provider returned no stream events", "server_error")
		}
		return
	}
	if request.Context().Err() != nil || errors.Is(err, request.Context().Err()) {
		return
	}
	if !started {
		s.writeExecutionError(w, err)
		return
	}

	status, code, message, errorType := classifyExecutionError(err)
	_ = status // HTTP status is already committed for an SSE response.
	errorBody := openaiwire.EncodeChatError(code, message, errorType, nil)
	_ = writeFrame(append(append([]byte("data: "), errorBody...), []byte("\n\n")...))
	_ = writeFrame([]byte("data: [DONE]\n\n"))
}

func (s *Server) handleResponses(w http.ResponseWriter, request *http.Request) {
	requestID := requestIDFromHeader(request.Header.Get("X-Request-ID"))
	w.Header().Set("X-Request-ID", requestID)

	if !s.inferenceReady() {
		writeOpenAIError(w, http.StatusServiceUnavailable, "gateway_not_ready", "gateway inference is not configured", "server_error")
		return
	}
	if err := s.authenticator.Authenticate(request); err != nil {
		writeOpenAIError(w, http.StatusUnauthorized, "invalid_api_key", "invalid authentication credentials", "authentication_error")
		return
	}

	request.Body = http.MaxBytesReader(w, request.Body, s.maxRequestBytes)
	body, err := io.ReadAll(request.Body)
	if err != nil {
		var maxBytesError *http.MaxBytesError
		if errors.As(err, &maxBytesError) {
			writeOpenAIError(w, http.StatusRequestEntityTooLarge, "request_too_large", "request body exceeds the configured limit", "invalid_request_error")
		} else {
			writeOpenAIError(w, http.StatusBadRequest, "request_read_error", "gateway could not read the request body", "invalid_request_error")
		}
		return
	}

	canonical, err := openaiwire.DecodeResponsesRequest(body, requestID)
	if err != nil {
		writeOpenAIError(w, http.StatusBadRequest, "invalid_request", err.Error(), "invalid_request_error")
		return
	}
	if canonical.Stream {
		s.handleResponsesStream(w, request, canonical)
		return
	}

	result, err := s.inference.Complete(request.Context(), canonical)
	if err != nil {
		s.writeExecutionError(w, err)
		return
	}
	if result == nil || result.Response == nil {
		writeOpenAIError(w, http.StatusBadGateway, "empty_upstream_response", "the upstream provider returned no response", "server_error")
		return
	}

	response := *result.Response
	response.Model = canonical.Model
	responseBody, err := openaiwire.EncodeResponsesResponse(&response)
	if err != nil {
		s.logger.ErrorContext(request.Context(), "encode responses response", "request_id", requestID, "error", err)
		writeOpenAIError(w, http.StatusInternalServerError, "response_encoding_error", "gateway could not encode the upstream response", "server_error")
		return
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(responseBody)
}

func (s *Server) handleResponsesStream(w http.ResponseWriter, request *http.Request, canonical *protocol.CanonicalRequest) {
	flusher, ok := w.(http.Flusher)
	if !ok {
		writeOpenAIError(w, http.StatusInternalServerError, "streaming_unsupported", "HTTP streaming is unavailable", "server_error")
		return
	}

	encoder := openaiwire.NewResponsesStreamEncoder(canonical.RequestID, canonical.Model)
	started := false
	writeFrame := func(frame []byte) error {
		if !started {
			w.Header().Set("Content-Type", "text/event-stream; charset=utf-8")
			w.Header().Set("Cache-Control", "no-cache")
			w.Header().Set("X-Accel-Buffering", "no")
			w.WriteHeader(http.StatusOK)
			started = true
		}
		if _, err := w.Write(frame); err != nil {
			return err
		}
		flusher.Flush()
		return nil
	}

	_, err := s.inference.Stream(request.Context(), canonical, func(event protocol.StreamEvent) error {
		frames, encodeErr := encoder.Encode(event)
		if encodeErr != nil {
			return encodeErr
		}
		for _, frame := range frames {
			if err := writeFrame(frame); err != nil {
				return err
			}
		}
		return nil
	})
	if err == nil {
		if !started {
			writeOpenAIError(w, http.StatusBadGateway, "empty_upstream_stream", "the upstream provider returned no stream events", "server_error")
		}
		return
	}
	if request.Context().Err() != nil || errors.Is(err, request.Context().Err()) {
		return
	}
	if !started {
		s.writeExecutionError(w, err)
		return
	}

	_, code, message, errorType := classifyExecutionError(err)
	_ = writeFrame(openaiwire.EncodeResponsesErrorEvent(code, message, errorType))
}

func (s *Server) handleAnthropicMessages(w http.ResponseWriter, request *http.Request) {
	requestID := requestIDFromHeader(request.Header.Get("request-id"))
	if requestID == "" {
		requestID = requestIDFromHeader(request.Header.Get("X-Request-ID"))
	}
	w.Header().Set("request-id", requestID)
	w.Header().Set("X-Request-ID", requestID)

	if !s.inferenceReady() {
		writeAnthropicError(w, http.StatusServiceUnavailable, requestID, "overloaded_error", "gateway inference is not configured")
		return
	}
	if err := s.authenticator.Authenticate(request); err != nil {
		writeAnthropicError(w, http.StatusUnauthorized, requestID, "authentication_error", "invalid authentication credentials")
		return
	}

	request.Body = http.MaxBytesReader(w, request.Body, s.maxRequestBytes)
	body, err := io.ReadAll(request.Body)
	if err != nil {
		var maxBytesError *http.MaxBytesError
		if errors.As(err, &maxBytesError) {
			writeAnthropicError(w, http.StatusRequestEntityTooLarge, requestID, "request_too_large", "request body exceeds the configured limit")
		} else {
			writeAnthropicError(w, http.StatusBadRequest, requestID, "invalid_request_error", "gateway could not read the request body")
		}
		return
	}
	canonical, err := anthropicwire.DecodeMessagesRequest(body, requestID)
	if err != nil {
		writeAnthropicError(w, http.StatusBadRequest, requestID, "invalid_request_error", err.Error())
		return
	}
	if canonical.Stream {
		s.handleAnthropicStream(w, request, canonical)
		return
	}

	result, err := s.inference.Complete(request.Context(), canonical)
	if err != nil {
		s.writeAnthropicExecutionError(w, requestID, err)
		return
	}
	if result == nil || result.Response == nil {
		writeAnthropicError(w, http.StatusBadGateway, requestID, "api_error", "the upstream provider returned no response")
		return
	}
	body, err = anthropicwire.EncodeMessagesResponse(result.Response, canonical.Model)
	if err != nil {
		s.logger.ErrorContext(request.Context(), "encode Anthropic response", "request_id", requestID, "error", err)
		writeAnthropicError(w, http.StatusInternalServerError, requestID, "api_error", "gateway could not encode the upstream response")
		return
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(body)
}

func (s *Server) handleAnthropicStream(w http.ResponseWriter, request *http.Request, canonical *protocol.CanonicalRequest) {
	flusher, ok := w.(http.Flusher)
	if !ok {
		writeAnthropicError(w, http.StatusInternalServerError, canonical.RequestID, "api_error", "HTTP streaming is unavailable")
		return
	}
	encoder := anthropicwire.NewStreamEncoder(canonical.RequestID, canonical.Model)
	started := false
	writeFrame := func(frame []byte) error {
		if !started {
			w.Header().Set("Content-Type", "text/event-stream; charset=utf-8")
			w.Header().Set("Cache-Control", "no-cache")
			w.Header().Set("X-Accel-Buffering", "no")
			w.WriteHeader(http.StatusOK)
			started = true
		}
		if _, err := w.Write(frame); err != nil {
			return err
		}
		flusher.Flush()
		return nil
	}

	_, err := s.inference.Stream(request.Context(), canonical, func(event protocol.StreamEvent) error {
		frames, encodeErr := encoder.Encode(event)
		if encodeErr != nil {
			return encodeErr
		}
		for _, frame := range frames {
			if err := writeFrame(frame); err != nil {
				return err
			}
		}
		return nil
	})
	if err == nil {
		if !started {
			writeAnthropicError(w, http.StatusBadGateway, canonical.RequestID, "api_error", "the upstream provider returned no stream events")
		}
		return
	}
	if request.Context().Err() != nil || errors.Is(err, request.Context().Err()) {
		return
	}
	status, _, message, errorType := classifyExecutionError(err)
	anthropicType := anthropicErrorType(status, errorType)
	if !started {
		writeAnthropicError(w, status, canonical.RequestID, anthropicType, message)
		return
	}
	_ = writeFrame(anthropicwire.EncodeErrorEvent(anthropicType, message))
}

func (s *Server) writeAnthropicExecutionError(w http.ResponseWriter, requestID string, err error) {
	status, _, message, errorType := classifyExecutionError(err)
	writeAnthropicError(w, status, requestID, anthropicErrorType(status, errorType), message)
}

func anthropicErrorType(status int, errorType string) string {
	switch status {
	case http.StatusBadRequest:
		return "invalid_request_error"
	case http.StatusUnauthorized:
		return "authentication_error"
	case http.StatusForbidden:
		return "permission_error"
	case http.StatusNotFound:
		return "not_found_error"
	case http.StatusRequestEntityTooLarge:
		return "request_too_large"
	case http.StatusTooManyRequests:
		return "rate_limit_error"
	case http.StatusServiceUnavailable:
		return "overloaded_error"
	default:
		if errorType == "rate_limit_error" {
			return "rate_limit_error"
		}
		return "api_error"
	}
}

func writeAnthropicError(w http.ResponseWriter, status int, requestID, errorType, message string) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("request-id", requestID)
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]any{
		"type": "error",
		"error": map[string]any{
			"type":    errorType,
			"message": message,
		},
		"request_id": requestID,
	})
}

func (s *Server) handleGeminiGenerateContent(w http.ResponseWriter, request *http.Request) {
	action := request.PathValue("gemini_action")
	model, stream, ok := parseGeminiAction(action)
	requestID := requestIDFromHeader(request.Header.Get("X-Request-ID"))
	w.Header().Set("X-Request-ID", requestID)

	if !ok {
		writeGeminiError(w, http.StatusNotFound, "NOT_FOUND", "unsupported Gemini method")
		return
	}
	if !s.inferenceReady() {
		writeGeminiError(w, http.StatusServiceUnavailable, "UNAVAILABLE", "gateway inference is not configured")
		return
	}
	if err := s.authenticator.Authenticate(request); err != nil {
		writeGeminiError(w, http.StatusUnauthorized, "UNAUTHENTICATED", "invalid authentication credentials")
		return
	}

	request.Body = http.MaxBytesReader(w, request.Body, s.maxRequestBytes)
	body, err := io.ReadAll(request.Body)
	if err != nil {
		var maxBytesError *http.MaxBytesError
		if errors.As(err, &maxBytesError) {
			writeGeminiError(w, http.StatusRequestEntityTooLarge, "RESOURCE_EXHAUSTED", "request body exceeds the configured limit")
		} else {
			writeGeminiError(w, http.StatusBadRequest, "INVALID_ARGUMENT", "gateway could not read the request body")
		}
		return
	}

	canonical, err := geminiwire.DecodeGenerateContentRequest(body, model, requestID, stream)
	if err != nil {
		writeGeminiError(w, http.StatusBadRequest, "INVALID_ARGUMENT", err.Error())
		return
	}
	if stream {
		s.handleGeminiStream(w, request, canonical)
		return
	}

	result, err := s.inference.Complete(request.Context(), canonical)
	if err != nil {
		s.writeGeminiExecutionError(w, err)
		return
	}
	if result == nil || result.Response == nil {
		writeGeminiError(w, http.StatusBadGateway, "INTERNAL", "the upstream provider returned no response")
		return
	}

	body, err = geminiwire.EncodeGenerateContentResponse(result.Response, canonical.Model)
	if err != nil {
		s.logger.ErrorContext(request.Context(), "encode Gemini response", "request_id", requestID, "error", err)
		writeGeminiError(w, http.StatusInternalServerError, "INTERNAL", "gateway could not encode the upstream response")
		return
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(body)
}

func (s *Server) handleGeminiStream(w http.ResponseWriter, request *http.Request, canonical *protocol.CanonicalRequest) {
	flusher, ok := w.(http.Flusher)
	if !ok {
		writeGeminiError(w, http.StatusInternalServerError, "INTERNAL", "HTTP streaming is unavailable")
		return
	}

	encoder := geminiwire.NewStreamEncoder(canonical.RequestID, canonical.Model)
	started := false
	writeFrame := func(frame []byte) error {
		if !started {
			w.Header().Set("Content-Type", "text/event-stream; charset=utf-8")
			w.Header().Set("Cache-Control", "no-cache")
			w.Header().Set("X-Accel-Buffering", "no")
			w.WriteHeader(http.StatusOK)
			started = true
		}
		if _, err := w.Write(frame); err != nil {
			return err
		}
		flusher.Flush()
		return nil
	}

	_, err := s.inference.Stream(request.Context(), canonical, func(event protocol.StreamEvent) error {
		frames, encodeErr := encoder.Encode(event)
		if encodeErr != nil {
			return encodeErr
		}
		for _, frame := range frames {
			if err := writeFrame(frame); err != nil {
				return err
			}
		}
		return nil
	})
	if err == nil {
		if !started {
			writeGeminiError(w, http.StatusBadGateway, "INTERNAL", "the upstream provider returned no stream events")
		}
		return
	}
	if request.Context().Err() != nil || errors.Is(err, request.Context().Err()) {
		return
	}

	status, _, message, errorType := classifyExecutionError(err)
	errorStatus := geminiErrorStatus(status, errorType)
	if !started {
		writeGeminiError(w, status, errorStatus, message)
		return
	}
	_ = writeFrame(geminiwire.EncodeErrorEvent(status, message, errorStatus))
}

func parseGeminiAction(action string) (model string, stream bool, ok bool) {
	switch {
	case strings.HasSuffix(action, ":streamGenerateContent"):
		return strings.TrimSuffix(action, ":streamGenerateContent"), true, true
	case strings.HasSuffix(action, ":generateContent"):
		return strings.TrimSuffix(action, ":generateContent"), false, true
	default:
		return "", false, false
	}
}

func (s *Server) writeGeminiExecutionError(w http.ResponseWriter, err error) {
	status, _, message, errorType := classifyExecutionError(err)
	writeGeminiError(w, status, geminiErrorStatus(status, errorType), message)
}

func geminiErrorStatus(status int, errorType string) string {
	switch status {
	case http.StatusBadRequest:
		return "INVALID_ARGUMENT"
	case http.StatusUnauthorized:
		return "UNAUTHENTICATED"
	case http.StatusForbidden:
		return "PERMISSION_DENIED"
	case http.StatusNotFound:
		return "NOT_FOUND"
	case http.StatusTooManyRequests, http.StatusRequestEntityTooLarge:
		return "RESOURCE_EXHAUSTED"
	case http.StatusGatewayTimeout:
		return "DEADLINE_EXCEEDED"
	case http.StatusServiceUnavailable:
		return "UNAVAILABLE"
	default:
		if errorType == "rate_limit_error" {
			return "RESOURCE_EXHAUSTED"
		}
		return "INTERNAL"
	}
}

func writeGeminiError(w http.ResponseWriter, status int, errorStatus, message string) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]any{
		"error": map[string]any{
			"code":    status,
			"message": message,
			"status":  errorStatus,
		},
	})
}

func (s *Server) writeExecutionError(w http.ResponseWriter, err error) {
	status, code, message, errorType := classifyExecutionError(err)
	writeOpenAIError(w, status, code, message, errorType)
}

func classifyExecutionError(err error) (status int, code, message, errorType string) {
	switch {
	case errors.Is(err, router.ErrRouteNotFound):
		return http.StatusNotFound, "model_not_found", "the requested model alias is not configured", "invalid_request_error"
	case errors.Is(err, router.ErrNoEligibleCandidate):
		return http.StatusServiceUnavailable, "no_available_deployment", "no healthy deployment is currently available", "server_error"
	case errors.Is(err, execution.ErrCapabilityUnsupported):
		return http.StatusBadRequest, "unsupported_capability", "the selected deployment does not declare support for this request", "invalid_request_error"
	case errors.Is(err, execution.ErrAdapterNotFound):
		return http.StatusServiceUnavailable, "adapter_not_configured", "the selected provider adapter is unavailable", "server_error"
	}

	var gatewayError *protocol.GatewayError
	if errors.As(err, &gatewayError) {
		switch gatewayError.HTTPStatus {
		case http.StatusTooManyRequests:
			return http.StatusTooManyRequests, "upstream_rate_limit", "the upstream provider rate limited the request", "rate_limit_error"
		case http.StatusBadRequest:
			return http.StatusBadRequest, "upstream_rejected_request", "the upstream provider rejected the request", "invalid_request_error"
		case http.StatusRequestTimeout, http.StatusGatewayTimeout:
			return http.StatusGatewayTimeout, "upstream_timeout", "the upstream provider timed out", "server_error"
		default:
			return http.StatusBadGateway, "upstream_error", "the upstream provider request failed", "server_error"
		}
	}
	var transportError *execution.TransportError
	if errors.As(err, &transportError) {
		return http.StatusBadGateway, "upstream_transport_error", "the gateway could not reach the upstream provider", "server_error"
	}
	return http.StatusInternalServerError, "gateway_error", "the gateway could not complete the request", "server_error"
}

func writeOpenAIError(w http.ResponseWriter, status int, code, message, errorType string) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_, _ = w.Write(openaiwire.EncodeChatError(code, message, errorType, nil))
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func requestIDFromHeader(value string) string {
	value = strings.TrimSpace(value)
	if value != "" && len(value) <= 128 && isSafeRequestID(value) {
		return value
	}
	var randomBytes [12]byte
	if _, err := rand.Read(randomBytes[:]); err == nil {
		return "req_" + hex.EncodeToString(randomBytes[:])
	}
	return "req_" + hex.EncodeToString([]byte(time.Now().UTC().Format(time.RFC3339Nano)))
}

func isSafeRequestID(value string) bool {
	for _, char := range value {
		if (char >= 'a' && char <= 'z') || (char >= 'A' && char <= 'Z') ||
			(char >= '0' && char <= '9') || char == '-' || char == '_' || char == '.' {
			continue
		}
		return false
	}
	return true
}

type bearerAuthenticator struct{ token []byte }

func (auth bearerAuthenticator) Authenticate(request *http.Request) error {
	var token string
	parts := strings.Fields(request.Header.Get("Authorization"))
	if len(parts) == 2 && strings.EqualFold(parts[0], "Bearer") {
		token = parts[1]
	} else {
		token = strings.TrimSpace(request.Header.Get("x-api-key"))
		if token == "" {
			token = strings.TrimSpace(request.Header.Get("x-goog-api-key"))
		}
	}
	if token == "" {
		return errors.New("missing gateway token")
	}
	provided := []byte(token)
	if len(provided) != len(auth.token) || subtle.ConstantTimeCompare(provided, auth.token) != 1 {
		return errors.New("invalid gateway token")
	}
	return nil
}

type responseMetrics struct {
	http.ResponseWriter
	status int
	bytes  int
}

func (writer *responseMetrics) WriteHeader(status int) {
	if writer.status != 0 {
		return
	}
	writer.status = status
	writer.ResponseWriter.WriteHeader(status)
}

func (writer *responseMetrics) Write(body []byte) (int, error) {
	if writer.status == 0 {
		writer.status = http.StatusOK
	}
	written, err := writer.ResponseWriter.Write(body)
	writer.bytes += written
	return written, err
}

type flushingResponseMetrics struct {
	*responseMetrics
	flusher http.Flusher
}

func (writer *flushingResponseMetrics) Flush() {
	writer.flusher.Flush()
}

func requestLogMiddleware(logger *slog.Logger, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		started := time.Now()
		metrics := &responseMetrics{ResponseWriter: w}
		var wrapped http.ResponseWriter = metrics
		if flusher, ok := w.(http.Flusher); ok {
			wrapped = &flushingResponseMetrics{responseMetrics: metrics, flusher: flusher}
		}
		next.ServeHTTP(wrapped, r)
		status := metrics.status
		if status == 0 {
			status = http.StatusOK
		}
		logger.InfoContext(r.Context(), "gateway request",
			"request_id", metrics.Header().Get("X-Request-ID"),
			"method", r.Method,
			"path", r.URL.Path,
			"status", status,
			"bytes", metrics.bytes,
			"duration", time.Since(started),
		)
	})
}
