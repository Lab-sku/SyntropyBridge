package router

import (
	"context"
	"errors"
	"fmt"
	"sync"

	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/provider"
)

var ErrRouteNotFound = errors.New("route not found")

// Target joins a routing candidate with the concrete adapter, deployment and
// credential needed for one upstream attempt. Credential secrets must never be copied
// into a Decision or log record.
type Target struct {
	Candidate  Candidate
	Adapter    string
	Deployment provider.Deployment
	Credential provider.Credential
}

// Plan is the immutable routing input for one public model alias.
type Plan struct {
	ModelAlias string
	Targets    []Target
}

// Resolver resolves an explicit public model alias. Implementations must not infer a
// provider from a model-name prefix.
type Resolver interface {
	Resolve(ctx context.Context, modelAlias string) (Plan, error)
}

// StaticResolver is used by tests and the opt-in single-upstream bootstrap mode. The
// production control plane will replace it with a snapshot-backed resolver.
type StaticResolver struct {
	mu     sync.RWMutex
	routes map[string]Plan
}

func NewStaticResolver(plans ...Plan) (*StaticResolver, error) {
	resolver := &StaticResolver{routes: make(map[string]Plan, len(plans))}
	for _, plan := range plans {
		if err := resolver.Set(plan); err != nil {
			return nil, err
		}
	}
	return resolver, nil
}

func (r *StaticResolver) Set(plan Plan) error {
	if r == nil {
		return errors.New("set static route: resolver is nil")
	}
	if plan.ModelAlias == "" {
		return errors.New("set static route: model alias is empty")
	}
	if len(plan.Targets) == 0 {
		return fmt.Errorf("set static route %q: no targets", plan.ModelAlias)
	}

	cloned, err := clonePlan(plan)
	if err != nil {
		return err
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.routes == nil {
		r.routes = make(map[string]Plan)
	}
	r.routes[plan.ModelAlias] = cloned
	return nil
}

func (r *StaticResolver) Resolve(ctx context.Context, modelAlias string) (Plan, error) {
	if err := ctx.Err(); err != nil {
		return Plan{}, err
	}
	if r == nil {
		return Plan{}, ErrRouteNotFound
	}
	r.mu.RLock()
	plan, ok := r.routes[modelAlias]
	r.mu.RUnlock()
	if !ok {
		return Plan{}, fmt.Errorf("%w: %s", ErrRouteNotFound, modelAlias)
	}
	return clonePlan(plan)
}

func clonePlan(plan Plan) (Plan, error) {
	cloned := Plan{ModelAlias: plan.ModelAlias, Targets: make([]Target, len(plan.Targets))}
	seen := make(map[string]struct{}, len(plan.Targets))
	for i, target := range plan.Targets {
		if target.Adapter == "" {
			return Plan{}, fmt.Errorf("set static route %q: target %d adapter is empty", plan.ModelAlias, i)
		}
		if target.Deployment.ID == "" || target.Candidate.DeploymentID == "" {
			return Plan{}, fmt.Errorf("set static route %q: target %d deployment ID is empty", plan.ModelAlias, i)
		}
		if target.Candidate.DeploymentID != target.Deployment.ID {
			return Plan{}, fmt.Errorf(
				"set static route %q: target %d candidate deployment %q does not match deployment %q",
				plan.ModelAlias,
				i,
				target.Candidate.DeploymentID,
				target.Deployment.ID,
			)
		}
		if target.Candidate.CredentialID != target.Credential.ID {
			return Plan{}, fmt.Errorf(
				"set static route %q: target %d candidate credential %q does not match credential %q",
				plan.ModelAlias,
				i,
				target.Candidate.CredentialID,
				target.Credential.ID,
			)
		}
		key := target.Candidate.DeploymentID + "\x00" + target.Candidate.CredentialID
		if _, exists := seen[key]; exists {
			return Plan{}, fmt.Errorf("set static route %q: duplicate target %q", plan.ModelAlias, key)
		}
		seen[key] = struct{}{}
		cloned.Targets[i] = cloneTarget(target)
	}
	return cloned, nil
}

func cloneTarget(target Target) Target {
	cloned := target
	cloned.Deployment.Headers = cloneStringMap(target.Deployment.Headers)
	cloned.Deployment.Metadata = cloneStringMap(target.Deployment.Metadata)
	cloned.Credential.Fields = cloneStringMap(target.Credential.Fields)
	return cloned
}

func cloneStringMap(input map[string]string) map[string]string {
	if input == nil {
		return nil
	}
	output := make(map[string]string, len(input))
	for key, value := range input {
		output[key] = value
	}
	return output
}

func TargetForDecision(plan Plan, decision Decision) (Target, error) {
	for _, target := range plan.Targets {
		if target.Candidate.DeploymentID == decision.Selected.DeploymentID &&
			target.Candidate.CredentialID == decision.Selected.CredentialID {
			return cloneTarget(target), nil
		}
	}
	return Target{}, fmt.Errorf(
		"routing decision selected unknown target deployment=%q credential=%q",
		decision.Selected.DeploymentID,
		decision.Selected.CredentialID,
	)
}
