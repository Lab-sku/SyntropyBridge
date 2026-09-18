package execution

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"time"

	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/protocol"
	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/provider"
	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/router"
)

var (
	ErrAdapterNotFound       = errors.New("provider adapter not found")
	ErrCapabilityUnsupported = errors.New("request capability unsupported")
)

type HTTPDoer interface {
	Do(req *http.Request) (*http.Response, error)
}

// Attempt is safe to expose in traces: it intentionally contains identifiers and
// timings but never credential material.
type Attempt struct {
	AdapterName  string
	DeploymentID string
	CredentialID string
	StartedAt    time.Time
	Duration     time.Duration
	HTTPStatus   int
}

type Result struct {
	Response *protocol.CanonicalResponse
	Decision router.Decision
	Attempt  Attempt
}

type StreamResult struct {
	Decision router.Decision
	Attempt  Attempt
}

type Executor struct {
	registry *provider.Registry
	resolver router.Resolver
	selector router.Selector
	client   HTTPDoer
	now      func() time.Time
}

func New(
	registry *provider.Registry,
	resolver router.Resolver,
	selector router.Selector,
	client HTTPDoer,
) (*Executor, error) {
	if registry == nil {
		return nil, errors.New("create executor: provider registry is nil")
	}
	if resolver == nil {
		return nil, errors.New("create executor: route resolver is nil")
	}
	if selector == nil {
		return nil, errors.New("create executor: route selector is nil")
	}
	if client == nil {
		return nil, errors.New("create executor: HTTP client is nil")
	}
	return &Executor{
		registry: registry,
		resolver: resolver,
		selector: selector,
		client:   client,
		now:      time.Now,
	}, nil
}

func (e *Executor) Complete(ctx context.Context, req *protocol.CanonicalRequest) (*Result, error) {
	if req == nil {
		return nil, errors.New("execute completion: request is nil")
	}
	if req.Stream {
		return nil, errors.New("execute completion: streaming request passed to Complete")
	}

	target, decision, adapter, err := e.prepare(ctx, req)
	if err != nil {
		return nil, err
	}
	upstreamRequest, err := adapter.BuildRequest(ctx, req, target.Deployment, target.Credential)
	if err != nil {
		return nil, fmt.Errorf("build upstream request: %w", err)
	}

	attempt := e.newAttempt(target, adapter.Name())
	response, err := e.client.Do(upstreamRequest)
	attempt.Duration = e.now().Sub(attempt.StartedAt)
	if err != nil {
		return nil, &TransportError{Attempt: attempt, Cause: err}
	}
	defer response.Body.Close()
	attempt.HTTPStatus = response.StatusCode

	decoded, err := adapter.DecodeResponse(ctx, response)
	attempt.Duration = e.now().Sub(attempt.StartedAt)
	if err != nil {
		return nil, &AttemptError{Attempt: attempt, Cause: err}
	}
	decoded.RequestID = req.RequestID
	return &Result{Response: decoded, Decision: decision, Attempt: attempt}, nil
}

func (e *Executor) Stream(
	ctx context.Context,
	req *protocol.CanonicalRequest,
	emit func(protocol.StreamEvent) error,
) (*StreamResult, error) {
	if req == nil {
		return nil, errors.New("execute stream: request is nil")
	}
	if !req.Stream {
		return nil, errors.New("execute stream: non-streaming request passed to Stream")
	}
	if emit == nil {
		return nil, errors.New("execute stream: emit is nil")
	}

	target, decision, adapter, err := e.prepare(ctx, req)
	if err != nil {
		return nil, err
	}
	upstreamRequest, err := adapter.BuildRequest(ctx, req, target.Deployment, target.Credential)
	if err != nil {
		return nil, fmt.Errorf("build upstream stream request: %w", err)
	}

	attempt := e.newAttempt(target, adapter.Name())
	response, err := e.client.Do(upstreamRequest)
	if err != nil {
		attempt.Duration = e.now().Sub(attempt.StartedAt)
		return nil, &TransportError{Attempt: attempt, Cause: err}
	}
	defer response.Body.Close()
	attempt.HTTPStatus = response.StatusCode

	err = adapter.DecodeStream(ctx, response, emit)
	attempt.Duration = e.now().Sub(attempt.StartedAt)
	if err != nil {
		return &StreamResult{Decision: decision, Attempt: attempt}, &AttemptError{Attempt: attempt, Cause: err}
	}
	return &StreamResult{Decision: decision, Attempt: attempt}, nil
}

func (e *Executor) prepare(
	ctx context.Context,
	req *protocol.CanonicalRequest,
) (router.Target, router.Decision, provider.Adapter, error) {
	plan, err := e.resolver.Resolve(ctx, req.Model)
	if err != nil {
		return router.Target{}, router.Decision{}, nil, err
	}
	candidates := make([]router.Candidate, 0, len(plan.Targets))
	for _, target := range plan.Targets {
		candidates = append(candidates, target.Candidate)
	}
	decision, err := e.selector.Select(ctx, candidates)
	if err != nil {
		return router.Target{}, decision, nil, err
	}
	target, err := router.TargetForDecision(plan, decision)
	if err != nil {
		return router.Target{}, decision, nil, err
	}
	adapter, ok := e.registry.Get(target.Adapter)
	if !ok {
		return router.Target{}, decision, nil, fmt.Errorf("%w: %s", ErrAdapterNotFound, target.Adapter)
	}
	if err := checkCapabilities(ctx, adapter, target.Deployment, req); err != nil {
		return router.Target{}, decision, nil, err
	}
	return target, decision, adapter, nil
}

func (e *Executor) newAttempt(target router.Target, adapterName string) Attempt {
	return Attempt{
		AdapterName:  adapterName,
		DeploymentID: target.Deployment.ID,
		CredentialID: target.Credential.ID,
		StartedAt:    e.now().UTC(),
	}
}

func checkCapabilities(
	ctx context.Context,
	adapter provider.Adapter,
	deployment provider.Deployment,
	req *protocol.CanonicalRequest,
) error {
	capabilities := adapter.Capabilities(ctx, deployment)
	if !capabilities.Operations[req.Operation] {
		return capabilityError("operation", string(req.Operation))
	}
	if req.Stream && !capabilities.Streaming {
		return capabilityError("streaming", "true")
	}
	if len(req.Tools) > 0 && !capabilities.Tools {
		return capabilityError("tools", "true")
	}
	if req.Reasoning != nil && !capabilities.Reasoning {
		return capabilityError("reasoning", "true")
	}
	if len(req.ResponseFormat) > 0 && !capabilities.StructuredOutput {
		return capabilityError("structured_output", "true")
	}
	if hasMultimodalInput(req) && !capabilities.MultimodalInput {
		return capabilityError("multimodal_input", "true")
	}
	return nil
}

func capabilityError(name, value string) error {
	return fmt.Errorf("%w: %s=%s", ErrCapabilityUnsupported, name, value)
}

func hasMultimodalInput(req *protocol.CanonicalRequest) bool {
	for _, message := range req.Messages {
		for _, part := range message.Content {
			if part.ImageURL != nil || part.InlineData != nil {
				return true
			}
		}
	}
	return false
}
