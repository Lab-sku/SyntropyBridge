package config

import (
	"os"
	"strings"
	"time"
)

const (
	defaultAddr                  = ":8081"
	defaultShutdownTimeout       = 10 * time.Second
	defaultReadHeaderTimeout     = 5 * time.Second
	defaultIdleTimeout           = 120 * time.Second
	defaultDialTimeout           = 10 * time.Second
	defaultTLSHandshakeTimeout   = 10 * time.Second
	defaultResponseHeaderTimeout = 60 * time.Second
	defaultUpstreamIdleTimeout   = 90 * time.Second
	defaultMaxIdleConnections    = 100
	defaultMaxIdlePerHost        = 20
)

// Config contains process-level gateway settings. The Bootstrap block exists only to
// make the new data plane testable before the control-plane snapshot API is available.
type Config struct {
	Addr              string
	ShutdownTimeout   time.Duration
	ReadHeaderTimeout time.Duration
	IdleTimeout       time.Duration
	Transport         TransportConfig
	Bootstrap         BootstrapConfig
}

// TransportConfig controls the shared upstream HTTP transport. Client.Timeout remains
// unset so long-lived streams are governed by request contexts instead of a wall clock.
type TransportConfig struct {
	DialTimeout           time.Duration
	TLSHandshakeTimeout   time.Duration
	ResponseHeaderTimeout time.Duration
	IdleConnTimeout       time.Duration
	MaxIdleConns          int
	MaxIdleConnsPerHost   int
}

// BootstrapConfig defines one explicit model alias and deployment. It is disabled by
// default and is not a replacement for the v2 control plane.
type BootstrapConfig struct {
	Enabled                  bool
	ClientToken              string
	ModelAlias               string
	UpstreamBaseURL          string
	UpstreamModel            string
	UpstreamAPIKey           string
	UpstreamAuthHeader       string
	UpstreamAuthScheme       string
	UpstreamChatPath         string
	SupportsStreaming        bool
	SupportsTools            bool
	SupportsParallelTools    bool
	SupportsMultimodalInput  bool
	SupportsReasoning        bool
	SupportsStructuredOutput bool
}

// FromEnv loads configuration and returns descriptive errors that never include secret
// values. Bootstrap settings are validated only when explicitly enabled.
func FromEnv() (Config, error) {
	cfg := Config{
		Addr:              envOrDefault("SYNTROPY_GATEWAY_ADDR", defaultAddr),
		ShutdownTimeout:   defaultShutdownTimeout,
		ReadHeaderTimeout: defaultReadHeaderTimeout,
		IdleTimeout:       defaultIdleTimeout,
		Transport: TransportConfig{
			DialTimeout:           defaultDialTimeout,
			TLSHandshakeTimeout:   defaultTLSHandshakeTimeout,
			ResponseHeaderTimeout: defaultResponseHeaderTimeout,
			IdleConnTimeout:       defaultUpstreamIdleTimeout,
			MaxIdleConns:          defaultMaxIdleConnections,
			MaxIdleConnsPerHost:   defaultMaxIdlePerHost,
		},
		Bootstrap: BootstrapConfig{
			ClientToken:        os.Getenv("SYNTROPY_BOOTSTRAP_CLIENT_TOKEN"),
			ModelAlias:         strings.TrimSpace(os.Getenv("SYNTROPY_BOOTSTRAP_MODEL_ALIAS")),
			UpstreamBaseURL:    strings.TrimSpace(os.Getenv("SYNTROPY_BOOTSTRAP_UPSTREAM_BASE_URL")),
			UpstreamModel:      strings.TrimSpace(os.Getenv("SYNTROPY_BOOTSTRAP_UPSTREAM_MODEL")),
			UpstreamAPIKey:     os.Getenv("SYNTROPY_BOOTSTRAP_UPSTREAM_API_KEY"),
			UpstreamAuthHeader: envOrDefault("SYNTROPY_BOOTSTRAP_UPSTREAM_AUTH_HEADER", "Authorization"),
			UpstreamAuthScheme: envOrDefault("SYNTROPY_BOOTSTRAP_UPSTREAM_AUTH_SCHEME", "Bearer"),
			UpstreamChatPath:   envOrDefault("SYNTROPY_BOOTSTRAP_UPSTREAM_CHAT_PATH", "/v1/chat/completions"),
		},
	}

	var err error
	if cfg.ShutdownTimeout, err = durationFromEnv("SYNTROPY_GATEWAY_SHUTDOWN_TIMEOUT", cfg.ShutdownTimeout); err != nil {
		return Config{}, err
	}
	if cfg.ReadHeaderTimeout, err = durationFromEnv("SYNTROPY_GATEWAY_READ_HEADER_TIMEOUT", cfg.ReadHeaderTimeout); err != nil {
		return Config{}, err
	}
	if cfg.IdleTimeout, err = durationFromEnv("SYNTROPY_GATEWAY_IDLE_TIMEOUT", cfg.IdleTimeout); err != nil {
		return Config{}, err
	}
	if cfg.Transport.DialTimeout, err = durationFromEnv("SYNTROPY_UPSTREAM_DIAL_TIMEOUT", cfg.Transport.DialTimeout); err != nil {
		return Config{}, err
	}
	if cfg.Transport.TLSHandshakeTimeout, err = durationFromEnv("SYNTROPY_UPSTREAM_TLS_HANDSHAKE_TIMEOUT", cfg.Transport.TLSHandshakeTimeout); err != nil {
		return Config{}, err
	}
	if cfg.Transport.ResponseHeaderTimeout, err = durationFromEnv("SYNTROPY_UPSTREAM_RESPONSE_HEADER_TIMEOUT", cfg.Transport.ResponseHeaderTimeout); err != nil {
		return Config{}, err
	}
	if cfg.Transport.IdleConnTimeout, err = durationFromEnv("SYNTROPY_UPSTREAM_IDLE_CONN_TIMEOUT", cfg.Transport.IdleConnTimeout); err != nil {
		return Config{}, err
	}
	if cfg.Transport.MaxIdleConns, err = positiveIntFromEnv("SYNTROPY_UPSTREAM_MAX_IDLE_CONNS", cfg.Transport.MaxIdleConns); err != nil {
		return Config{}, err
	}
	if cfg.Transport.MaxIdleConnsPerHost, err = positiveIntFromEnv("SYNTROPY_UPSTREAM_MAX_IDLE_CONNS_PER_HOST", cfg.Transport.MaxIdleConnsPerHost); err != nil {
		return Config{}, err
	}
	if cfg.Bootstrap.Enabled, err = boolFromEnv("SYNTROPY_BOOTSTRAP_ENABLED", false); err != nil {
		return Config{}, err
	}
	if cfg.Bootstrap.SupportsStreaming, err = boolFromEnv("SYNTROPY_BOOTSTRAP_SUPPORTS_STREAMING", false); err != nil {
		return Config{}, err
	}
	if cfg.Bootstrap.SupportsTools, err = boolFromEnv("SYNTROPY_BOOTSTRAP_SUPPORTS_TOOLS", false); err != nil {
		return Config{}, err
	}
	if cfg.Bootstrap.SupportsParallelTools, err = boolFromEnv("SYNTROPY_BOOTSTRAP_SUPPORTS_PARALLEL_TOOLS", false); err != nil {
		return Config{}, err
	}
	if cfg.Bootstrap.SupportsMultimodalInput, err = boolFromEnv("SYNTROPY_BOOTSTRAP_SUPPORTS_MULTIMODAL_INPUT", false); err != nil {
		return Config{}, err
	}
	if cfg.Bootstrap.SupportsReasoning, err = boolFromEnv("SYNTROPY_BOOTSTRAP_SUPPORTS_REASONING", false); err != nil {
		return Config{}, err
	}
	if cfg.Bootstrap.SupportsStructuredOutput, err = boolFromEnv("SYNTROPY_BOOTSTRAP_SUPPORTS_STRUCTURED_OUTPUT", false); err != nil {
		return Config{}, err
	}

	if err := cfg.Bootstrap.Validate(); err != nil {
		return Config{}, err
	}
	return cfg, nil
}
