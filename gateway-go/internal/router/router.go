package router

import (
	"context"
	"errors"
	"fmt"
	"math/rand"
	"time"
)

var ErrNoEligibleCandidate = errors.New("no eligible routing candidate")

type Candidate struct {
	DeploymentID       string
	CredentialID       string
	Weight             int
	Priority           int
	Healthy            bool
	Enabled            bool
	EstimatedCostMicros int64
	ObservedLatency    time.Duration
}

type Assessment struct {
	Candidate Candidate
	Eligible  bool
	Reason    string
}

type Decision struct {
	Strategy       string
	Selected       Candidate
	SelectedReason string
	Considered     []Assessment
}

type Selector interface {
	Select(ctx context.Context, candidates []Candidate) (Decision, error)
}

type intnSource interface {
	Intn(n int) int
}

type defaultSource struct{}

func (defaultSource) Intn(n int) int { return rand.Intn(n) }

// WeightedSelector selects among enabled, healthy, positively weighted candidates and
// returns the complete assessment list for tracing and operator explanation.
type WeightedSelector struct {
	random intnSource
}

func NewWeightedSelector() *WeightedSelector {
	return &WeightedSelector{random: defaultSource{}}
}

func newWeightedSelectorWithSource(source intnSource) *WeightedSelector {
	return &WeightedSelector{random: source}
}

func (s *WeightedSelector) Select(ctx context.Context, candidates []Candidate) (Decision, error) {
	if err := ctx.Err(); err != nil {
		return Decision{}, err
	}
	if s == nil || s.random == nil {
		return Decision{}, fmt.Errorf("weighted selector: random source is nil")
	}

	decision := Decision{Strategy: "weighted", Considered: make([]Assessment, 0, len(candidates))}
	eligible := make([]Candidate, 0, len(candidates))
	totalWeight := 0

	for _, candidate := range candidates {
		assessment := Assessment{Candidate: candidate}
		switch {
		case !candidate.Enabled:
			assessment.Reason = "deployment disabled"
		case !candidate.Healthy:
			assessment.Reason = "deployment unhealthy"
		case candidate.Weight <= 0:
			assessment.Reason = "non-positive weight"
		default:
			assessment.Eligible = true
			assessment.Reason = "eligible"
			eligible = append(eligible, candidate)
			totalWeight += candidate.Weight
		}
		decision.Considered = append(decision.Considered, assessment)
	}

	if len(eligible) == 0 || totalWeight <= 0 {
		return decision, ErrNoEligibleCandidate
	}

	draw := s.random.Intn(totalWeight)
	for _, candidate := range eligible {
		if draw < candidate.Weight {
			decision.Selected = candidate
			decision.SelectedReason = fmt.Sprintf(
				"selected by weighted draw among %d eligible deployments (total weight %d)",
				len(eligible),
				totalWeight,
			)
			return decision, nil
		}
		draw -= candidate.Weight
	}

	return decision, fmt.Errorf("weighted selector: unreachable selection state")
}
