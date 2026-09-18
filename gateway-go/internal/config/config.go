package config

import (
	"fmt"
	"os"
	"time"
)

const (
	defaultAddr              = ":8081"
	defaultShutdownTimeout   = 10 * time.Second
	defaultReadHeaderTimeout = 5 * time.Second
	defaultIdleTimeout       = 120 * time.Second
)

// Config contains process-level gateway settings. Request-specific policy belongs in
// the control plane and must not be represented as environment variables here.
type Config struct {
	Addr              string
	ShutdownTimeout   time.Duration
	ReadHeaderTimeout time.Duration
	IdleTimeout       time.Duration
}

// FromEnv loads configuration and returns a descriptive error for malformed values.
func FromEnv() (Config, error) {
	cfg := Config{
		Addr:              envOrDefault("SYNTROPY_GATEWAY_ADDR", defaultAddr),
		ShutdownTimeout:   defaultShutdownTimeout,
		ReadHeaderTimeout: defaultReadHeaderTimeout,
		IdleTimeout:       defaultIdleTimeout,
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

	return cfg, nil
}

func envOrDefault(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}

func durationFromEnv(name string, fallback time.Duration) (time.Duration, error) {
	value := os.Getenv(name)
	if value == "" {
		return fallback, nil
	}

	parsed, err := time.ParseDuration(value)
	if err != nil {
		return 0, fmt.Errorf("parse %s: %w", name, err)
	}
	if parsed <= 0 {
		return 0, fmt.Errorf("parse %s: duration must be positive", name)
	}
	return parsed, nil
}
