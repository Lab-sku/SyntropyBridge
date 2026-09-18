// Package bootstrap wires one explicit OpenAI-compatible deployment for development
// and migration testing. It is intentionally narrower than the future control-plane
// snapshot runtime and is disabled unless the operator opts in.
package bootstrap

import (
	"fmt"
	"net"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/config"
	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/execution"
	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/provider"
	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/provider/openaicompat"
	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/router"
)

const (
	bootstrapDeploymentID = "bootstrap-deployment"
	bootstrapCredentialID = "bootstrap-credential"
)

// Runtime contains the request executor and client authentication material required by
// the HTTP server. Values containing secrets must never be logged.
type Runtime struct {
	Executor    *execution.Executor
	ClientToken string
	HTTPClient  *http.Client
}

// Build returns nil when bootstrap mode is disabled.
func Build(cfg config.Config, registry *provider.Registry) (*Runtime, error) {
	if !cfg.Bootstrap.Enabled {
		return nil, nil
	}
	if registry == nil {
		return nil, fmt.Errorf("build bootstrap runtime: provider registry is nil")
	}
	if err := cfg.Bootstrap.Validate(); err != nil {
		return nil, fmt.Errorf("build bootstrap runtime: %w", err)
	}

	authScheme := cfg.Bootstrap.UpstreamAuthScheme
	if strings.EqualFold(authScheme, "none") {
		authScheme = ""
	}
	metadata := map[string]string{
		"upstream_model":             cfg.Bootstrap.UpstreamModel,
		"chat_path":                  cfg.Bootstrap.UpstreamChatPath,
		"auth_header":                cfg.Bootstrap.UpstreamAuthHeader,
		"auth_scheme":                authScheme,
		"supports_streaming":         strconv.FormatBool(cfg.Bootstrap.SupportsStreaming),
		"supports_tools":             strconv.FormatBool(cfg.Bootstrap.SupportsTools),
		"supports_parallel_tools":    strconv.FormatBool(cfg.Bootstrap.SupportsParallelTools),
		"supports_multimodal_input":  strconv.FormatBool(cfg.Bootstrap.SupportsMultimodalInput),
		"supports_reasoning":         strconv.FormatBool(cfg.Bootstrap.SupportsReasoning),
		"supports_structured_output": strconv.FormatBool(cfg.Bootstrap.SupportsStructuredOutput),
	}
	resolver, err := router.NewStaticResolver(router.Plan{
		ModelAlias: cfg.Bootstrap.ModelAlias,
		Targets: []router.Target{{
			Candidate: router.Candidate{
				DeploymentID: bootstrapDeploymentID,
				CredentialID: bootstrapCredentialID,
				Weight:       1,
				Enabled:      true,
				Healthy:      true,
			},
			Adapter: openaicompat.Name,
			Deployment: provider.Deployment{
				ID:         bootstrapDeploymentID,
				ProviderID: openaicompat.Name,
				BaseURL:    cfg.Bootstrap.UpstreamBaseURL,
				Metadata:   metadata,
			},
			Credential: provider.Credential{
				ID:     bootstrapCredentialID,
				Secret: cfg.Bootstrap.UpstreamAPIKey,
			},
		}},
	})
	if err != nil {
		return nil, fmt.Errorf("build bootstrap route: %w", err)
	}

	client := newHTTPClient(cfg.Transport)
	executor, err := execution.New(registry, resolver, router.NewWeightedSelector(), client)
	if err != nil {
		return nil, fmt.Errorf("build bootstrap executor: %w", err)
	}
	return &Runtime{
		Executor:    executor,
		ClientToken: cfg.Bootstrap.ClientToken,
		HTTPClient:  client,
	}, nil
}

func newHTTPClient(cfg config.TransportConfig) *http.Client {
	dialer := &net.Dialer{
		Timeout:   cfg.DialTimeout,
		KeepAlive: 30 * time.Second,
	}
	transport := &http.Transport{
		Proxy:                 http.ProxyFromEnvironment,
		DialContext:           dialer.DialContext,
		ForceAttemptHTTP2:     true,
		MaxIdleConns:          cfg.MaxIdleConns,
		MaxIdleConnsPerHost:   cfg.MaxIdleConnsPerHost,
		IdleConnTimeout:       cfg.IdleConnTimeout,
		TLSHandshakeTimeout:   cfg.TLSHandshakeTimeout,
		ResponseHeaderTimeout: cfg.ResponseHeaderTimeout,
		ExpectContinueTimeout: time.Second,
	}
	return &http.Client{
		Transport: transport,
		// Redirects can move a request to another origin. Do not risk replaying a
		// request carrying provider credentials; operators must configure the final URL.
		CheckRedirect: func(*http.Request, []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}
}
