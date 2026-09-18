package gateway

import (
	"encoding/json"
	"log/slog"
	"net/http"
	"time"

	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/protocol"
	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/provider"
)

type Server struct {
	logger   *slog.Logger
	registry *provider.Registry
	started  time.Time
}

func NewServer(logger *slog.Logger, registry *provider.Registry) *Server {
	if logger == nil {
		logger = slog.Default()
	}
	if registry == nil {
		registry = provider.NewRegistry()
	}
	return &Server{logger: logger, registry: registry, started: time.Now().UTC()}
}

func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.handleHealth)
	mux.HandleFunc("GET /readyz", s.handleReady)
	mux.HandleFunc("GET /v1/gateway/capabilities", s.handleCapabilities)
	return requestLogMiddleware(s.logger, mux)
}

func (s *Server) handleHealth(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"status":     "ok",
		"started_at": s.started,
	})
}

func (s *Server) handleReady(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"status": "ready",
	})
}

func (s *Server) handleCapabilities(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"protocols": []protocol.Protocol{
			protocol.ProtocolOpenAIChat,
			protocol.ProtocolOpenAIResponses,
			protocol.ProtocolAnthropic,
			protocol.ProtocolGemini,
		},
		"registered_adapters": s.registry.Names(),
		"inference_ready":     false,
		"note":                "protocol types are scaffolded; provider inference is not yet enabled",
	})
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(value); err != nil {
		// The headers are already committed. Logging is handled by the surrounding
		// server in later milestones; health payloads are intentionally small.
		return
	}
}

func requestLogMiddleware(logger *slog.Logger, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		started := time.Now()
		next.ServeHTTP(w, r)
		logger.InfoContext(r.Context(), "gateway request",
			"method", r.Method,
			"path", r.URL.Path,
			"duration", time.Since(started),
		)
	})
}
