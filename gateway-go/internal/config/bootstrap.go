package config

import (
	"errors"
	"fmt"
	"net/url"
	"strings"
)

func (cfg BootstrapConfig) Validate() error {
	if !cfg.Enabled {
		return nil
	}
	missing := make([]string, 0, 5)
	if cfg.ClientToken == "" {
		missing = append(missing, "SYNTROPY_BOOTSTRAP_CLIENT_TOKEN")
	}
	if cfg.ModelAlias == "" {
		missing = append(missing, "SYNTROPY_BOOTSTRAP_MODEL_ALIAS")
	}
	if cfg.UpstreamBaseURL == "" {
		missing = append(missing, "SYNTROPY_BOOTSTRAP_UPSTREAM_BASE_URL")
	}
	if cfg.UpstreamModel == "" {
		missing = append(missing, "SYNTROPY_BOOTSTRAP_UPSTREAM_MODEL")
	}
	if cfg.UpstreamAPIKey == "" {
		missing = append(missing, "SYNTROPY_BOOTSTRAP_UPSTREAM_API_KEY")
	}
	if len(missing) > 0 {
		return fmt.Errorf("bootstrap mode requires %s", strings.Join(missing, ", "))
	}
	if len(cfg.ClientToken) < 16 {
		return errors.New("SYNTROPY_BOOTSTRAP_CLIENT_TOKEN must contain at least 16 characters")
	}
	if strings.ContainsAny(cfg.ModelAlias, " \t\r\n") {
		return errors.New("SYNTROPY_BOOTSTRAP_MODEL_ALIAS must not contain whitespace")
	}
	if err := validateBaseURL(cfg.UpstreamBaseURL); err != nil {
		return fmt.Errorf("validate SYNTROPY_BOOTSTRAP_UPSTREAM_BASE_URL: %w", err)
	}
	if strings.TrimSpace(cfg.UpstreamAuthHeader) == "" {
		return errors.New("SYNTROPY_BOOTSTRAP_UPSTREAM_AUTH_HEADER must not be empty")
	}
	if strings.ContainsAny(cfg.UpstreamAuthHeader, "\r\n:") {
		return errors.New("SYNTROPY_BOOTSTRAP_UPSTREAM_AUTH_HEADER is invalid")
	}
	if !strings.HasPrefix(cfg.UpstreamChatPath, "/") || strings.ContainsAny(cfg.UpstreamChatPath, "?#\r\n") {
		return errors.New("SYNTROPY_BOOTSTRAP_UPSTREAM_CHAT_PATH must be an absolute path without query or fragment")
	}
	if cfg.SupportsParallelTools && !cfg.SupportsTools {
		return errors.New("SYNTROPY_BOOTSTRAP_SUPPORTS_PARALLEL_TOOLS requires SYNTROPY_BOOTSTRAP_SUPPORTS_TOOLS")
	}
	return nil
}

func validateBaseURL(value string) error {
	parsed, err := url.Parse(value)
	if err != nil {
		return err
	}
	if parsed.Scheme != "http" && parsed.Scheme != "https" {
		return fmt.Errorf("scheme %q is not http or https", parsed.Scheme)
	}
	if parsed.Host == "" {
		return errors.New("host is empty")
	}
	if parsed.User != nil {
		return errors.New("userinfo is not allowed")
	}
	if parsed.RawQuery != "" || parsed.Fragment != "" {
		return errors.New("query and fragment are not allowed")
	}
	return nil
}
