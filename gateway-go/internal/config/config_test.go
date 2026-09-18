package config

import (
	"strings"
	"testing"
	"time"
)

func TestFromEnvDefaults(t *testing.T) {
	clearConfigEnv(t)
	cfg, err := FromEnv()
	if err != nil {
		t.Fatalf("FromEnv() error = %v", err)
	}
	if cfg.Addr != defaultAddr || cfg.ShutdownTimeout != defaultShutdownTimeout {
		t.Fatalf("unexpected defaults: %#v", cfg)
	}
	if cfg.Bootstrap.Enabled {
		t.Fatal("bootstrap must be disabled by default")
	}
	if cfg.Transport.ResponseHeaderTimeout != defaultResponseHeaderTimeout {
		t.Fatalf("ResponseHeaderTimeout = %v", cfg.Transport.ResponseHeaderTimeout)
	}
}

func TestFromEnvRejectsInvalidDuration(t *testing.T) {
	clearConfigEnv(t)
	t.Setenv("SYNTROPY_GATEWAY_SHUTDOWN_TIMEOUT", "not-a-duration")
	if _, err := FromEnv(); err == nil {
		t.Fatal("FromEnv() error = nil, want an error")
	}
}

func TestFromEnvAcceptsOverrides(t *testing.T) {
	clearConfigEnv(t)
	t.Setenv("SYNTROPY_GATEWAY_ADDR", "127.0.0.1:9090")
	t.Setenv("SYNTROPY_GATEWAY_SHUTDOWN_TIMEOUT", "3s")
	t.Setenv("SYNTROPY_UPSTREAM_MAX_IDLE_CONNS", "77")

	cfg, err := FromEnv()
	if err != nil {
		t.Fatalf("FromEnv() error = %v", err)
	}
	if cfg.Addr != "127.0.0.1:9090" || cfg.ShutdownTimeout != 3*time.Second {
		t.Fatalf("unexpected overrides: %#v", cfg)
	}
	if cfg.Transport.MaxIdleConns != 77 {
		t.Fatalf("MaxIdleConns = %d", cfg.Transport.MaxIdleConns)
	}
}

func TestBootstrapModeRequiresExplicitSecretsAndRoute(t *testing.T) {
	clearConfigEnv(t)
	t.Setenv("SYNTROPY_BOOTSTRAP_ENABLED", "true")
	t.Setenv("SYNTROPY_BOOTSTRAP_CLIENT_TOKEN", "client-secret-value")

	_, err := FromEnv()
	if err == nil {
		t.Fatal("FromEnv() error = nil, want missing route error")
	}
	message := err.Error()
	if !strings.Contains(message, "SYNTROPY_BOOTSTRAP_MODEL_ALIAS") || strings.Contains(message, "client-secret-value") {
		t.Fatalf("unsafe or incomplete error = %q", message)
	}
}

func TestBootstrapModeAcceptsExplicitConfiguration(t *testing.T) {
	setValidBootstrapEnv(t)
	t.Setenv("SYNTROPY_BOOTSTRAP_SUPPORTS_STREAMING", "true")
	t.Setenv("SYNTROPY_BOOTSTRAP_SUPPORTS_TOOLS", "true")
	t.Setenv("SYNTROPY_BOOTSTRAP_SUPPORTS_PARALLEL_TOOLS", "true")

	cfg, err := FromEnv()
	if err != nil {
		t.Fatalf("FromEnv() error = %v", err)
	}
	if !cfg.Bootstrap.Enabled || !cfg.Bootstrap.SupportsStreaming || !cfg.Bootstrap.SupportsParallelTools {
		t.Fatalf("bootstrap flags = %#v", cfg.Bootstrap)
	}
	if cfg.Bootstrap.UpstreamAuthHeader != "Authorization" || cfg.Bootstrap.UpstreamAuthScheme != "Bearer" {
		t.Fatalf("bootstrap auth defaults = %#v", cfg.Bootstrap)
	}
}

func TestBootstrapModeRejectsUnsafeURLAndCapabilityCombination(t *testing.T) {
	setValidBootstrapEnv(t)
	t.Setenv("SYNTROPY_BOOTSTRAP_UPSTREAM_BASE_URL", "https://user:pass@example.invalid/v1?token=x")
	_, err := FromEnv()
	if err == nil || !strings.Contains(err.Error(), "userinfo") {
		t.Fatalf("URL validation error = %v", err)
	}

	setValidBootstrapEnv(t)
	t.Setenv("SYNTROPY_BOOTSTRAP_SUPPORTS_PARALLEL_TOOLS", "true")
	_, err = FromEnv()
	if err == nil || !strings.Contains(err.Error(), "requires") {
		t.Fatalf("capability validation error = %v", err)
	}
}

func setValidBootstrapEnv(t *testing.T) {
	t.Helper()
	clearConfigEnv(t)
	t.Setenv("SYNTROPY_BOOTSTRAP_ENABLED", "true")
	t.Setenv("SYNTROPY_BOOTSTRAP_CLIENT_TOKEN", "client-secret-0123456789")
	t.Setenv("SYNTROPY_BOOTSTRAP_MODEL_ALIAS", "smart-chat")
	t.Setenv("SYNTROPY_BOOTSTRAP_UPSTREAM_BASE_URL", "https://example.invalid/v1")
	t.Setenv("SYNTROPY_BOOTSTRAP_UPSTREAM_MODEL", "real-model")
	t.Setenv("SYNTROPY_BOOTSTRAP_UPSTREAM_API_KEY", "upstream-secret")
}

func clearConfigEnv(t *testing.T) {
	t.Helper()
	names := []string{
		"SYNTROPY_GATEWAY_ADDR",
		"SYNTROPY_GATEWAY_SHUTDOWN_TIMEOUT",
		"SYNTROPY_GATEWAY_READ_HEADER_TIMEOUT",
		"SYNTROPY_GATEWAY_IDLE_TIMEOUT",
		"SYNTROPY_UPSTREAM_DIAL_TIMEOUT",
		"SYNTROPY_UPSTREAM_TLS_HANDSHAKE_TIMEOUT",
		"SYNTROPY_UPSTREAM_RESPONSE_HEADER_TIMEOUT",
		"SYNTROPY_UPSTREAM_IDLE_CONN_TIMEOUT",
		"SYNTROPY_UPSTREAM_MAX_IDLE_CONNS",
		"SYNTROPY_UPSTREAM_MAX_IDLE_CONNS_PER_HOST",
		"SYNTROPY_BOOTSTRAP_ENABLED",
		"SYNTROPY_BOOTSTRAP_CLIENT_TOKEN",
		"SYNTROPY_BOOTSTRAP_MODEL_ALIAS",
		"SYNTROPY_BOOTSTRAP_UPSTREAM_BASE_URL",
		"SYNTROPY_BOOTSTRAP_UPSTREAM_MODEL",
		"SYNTROPY_BOOTSTRAP_UPSTREAM_API_KEY",
		"SYNTROPY_BOOTSTRAP_UPSTREAM_AUTH_HEADER",
		"SYNTROPY_BOOTSTRAP_UPSTREAM_AUTH_SCHEME",
		"SYNTROPY_BOOTSTRAP_UPSTREAM_CHAT_PATH",
		"SYNTROPY_BOOTSTRAP_SUPPORTS_STREAMING",
		"SYNTROPY_BOOTSTRAP_SUPPORTS_TOOLS",
		"SYNTROPY_BOOTSTRAP_SUPPORTS_PARALLEL_TOOLS",
		"SYNTROPY_BOOTSTRAP_SUPPORTS_MULTIMODAL_INPUT",
		"SYNTROPY_BOOTSTRAP_SUPPORTS_REASONING",
		"SYNTROPY_BOOTSTRAP_SUPPORTS_STRUCTURED_OUTPUT",
	}
	for _, name := range names {
		t.Setenv(name, "")
	}
}
