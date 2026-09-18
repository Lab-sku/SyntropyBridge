package gateway

import (
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestHealthEndpoint(t *testing.T) {
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	server := NewServer(logger, nil)
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/healthz", nil)

	server.Handler().ServeHTTP(recorder, request)

	if recorder.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d", recorder.Code, http.StatusOK)
	}
	var body map[string]any
	if err := json.Unmarshal(recorder.Body.Bytes(), &body); err != nil {
		t.Fatalf("json.Unmarshal() error = %v", err)
	}
	if body["status"] != "ok" {
		t.Fatalf("status body = %#v", body["status"])
	}
}

func TestCapabilitiesDoNotOverclaimInferenceSupport(t *testing.T) {
	server := NewServer(slog.New(slog.NewTextHandler(io.Discard, nil)), nil)
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/v1/gateway/capabilities", nil)

	server.Handler().ServeHTTP(recorder, request)

	var body struct {
		InferenceReady bool `json:"inference_ready"`
	}
	if err := json.Unmarshal(recorder.Body.Bytes(), &body); err != nil {
		t.Fatalf("json.Unmarshal() error = %v", err)
	}
	if body.InferenceReady {
		t.Fatal("inference_ready = true before adapters are implemented")
	}
}
