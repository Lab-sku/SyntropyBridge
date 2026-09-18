package config

import (
	"testing"
	"time"
)

func TestFromEnvDefaults(t *testing.T) {
	t.Setenv("SYNTROPY_GATEWAY_ADDR", "")
	t.Setenv("SYNTROPY_GATEWAY_SHUTDOWN_TIMEOUT", "")
	t.Setenv("SYNTROPY_GATEWAY_READ_HEADER_TIMEOUT", "")
	t.Setenv("SYNTROPY_GATEWAY_IDLE_TIMEOUT", "")

	cfg, err := FromEnv()
	if err != nil {
		t.Fatalf("FromEnv() error = %v", err)
	}
	if cfg.Addr != defaultAddr {
		t.Fatalf("Addr = %q, want %q", cfg.Addr, defaultAddr)
	}
	if cfg.ShutdownTimeout != defaultShutdownTimeout {
		t.Fatalf("ShutdownTimeout = %v, want %v", cfg.ShutdownTimeout, defaultShutdownTimeout)
	}
}

func TestFromEnvRejectsInvalidDuration(t *testing.T) {
	t.Setenv("SYNTROPY_GATEWAY_SHUTDOWN_TIMEOUT", "not-a-duration")
	if _, err := FromEnv(); err == nil {
		t.Fatal("FromEnv() error = nil, want an error")
	}
}

func TestFromEnvAcceptsOverrides(t *testing.T) {
	t.Setenv("SYNTROPY_GATEWAY_ADDR", "127.0.0.1:9090")
	t.Setenv("SYNTROPY_GATEWAY_SHUTDOWN_TIMEOUT", "3s")

	cfg, err := FromEnv()
	if err != nil {
		t.Fatalf("FromEnv() error = %v", err)
	}
	if cfg.Addr != "127.0.0.1:9090" {
		t.Fatalf("Addr = %q", cfg.Addr)
	}
	if cfg.ShutdownTimeout != 3*time.Second {
		t.Fatalf("ShutdownTimeout = %v", cfg.ShutdownTimeout)
	}
}
