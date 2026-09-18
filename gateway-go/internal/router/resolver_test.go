package router

import (
	"context"
	"errors"
	"testing"

	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/provider"
)

func TestStaticResolverUsesExactAliasAndClonesSecrets(t *testing.T) {
	resolver, err := NewStaticResolver(Plan{
		ModelAlias: "smart-chat",
		Targets: []Target{{
			Candidate: Candidate{DeploymentID: "dep-1", CredentialID: "cred-1", Enabled: true, Healthy: true, Weight: 1},
			Adapter:   "openai-compatible",
			Deployment: provider.Deployment{
				ID:       "dep-1",
				BaseURL:  "https://example.invalid",
				Metadata: map[string]string{"upstream_model": "real-model"},
			},
			Credential: provider.Credential{ID: "cred-1", Secret: "secret"},
		}},
	})
	if err != nil {
		t.Fatalf("NewStaticResolver() error = %v", err)
	}

	plan, err := resolver.Resolve(context.Background(), "smart-chat")
	if err != nil {
		t.Fatalf("Resolve() error = %v", err)
	}
	plan.Targets[0].Deployment.Metadata["upstream_model"] = "mutated"

	again, err := resolver.Resolve(context.Background(), "smart-chat")
	if err != nil {
		t.Fatalf("second Resolve() error = %v", err)
	}
	if got := again.Targets[0].Deployment.Metadata["upstream_model"]; got != "real-model" {
		t.Fatalf("stored plan was mutated: %q", got)
	}
	if _, err := resolver.Resolve(context.Background(), "real-model"); !errors.Is(err, ErrRouteNotFound) {
		t.Fatalf("Resolve(unconfigured alias) error = %v, want ErrRouteNotFound", err)
	}
}

func TestStaticResolverRejectsMismatchedIdentifiers(t *testing.T) {
	_, err := NewStaticResolver(Plan{
		ModelAlias: "bad",
		Targets: []Target{{
			Candidate:  Candidate{DeploymentID: "candidate-dep", CredentialID: "cred"},
			Adapter:    "openai-compatible",
			Deployment: provider.Deployment{ID: "actual-dep"},
			Credential: provider.Credential{ID: "cred"},
		}},
	})
	if err == nil {
		t.Fatal("NewStaticResolver() error = nil, want mismatch error")
	}
}
