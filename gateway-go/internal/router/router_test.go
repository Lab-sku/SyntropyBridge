package router

import (
	"context"
	"errors"
	"testing"
)

type fixedSource struct{ value int }

func (s fixedSource) Intn(n int) int {
	if s.value < 0 || s.value >= n {
		panic("fixed source value outside range")
	}
	return s.value
}

func TestWeightedSelectorExplainsExclusionsAndSelection(t *testing.T) {
	selector := newWeightedSelectorWithSource(fixedSource{value: 2})
	decision, err := selector.Select(context.Background(), []Candidate{
		{DeploymentID: "disabled", Enabled: false, Healthy: true, Weight: 100},
		{DeploymentID: "unhealthy", Enabled: true, Healthy: false, Weight: 100},
		{DeploymentID: "primary", Enabled: true, Healthy: true, Weight: 2},
		{DeploymentID: "backup", Enabled: true, Healthy: true, Weight: 3},
	})
	if err != nil {
		t.Fatalf("Select() error = %v", err)
	}
	if decision.Selected.DeploymentID != "backup" {
		t.Fatalf("selected = %q, want backup", decision.Selected.DeploymentID)
	}
	if len(decision.Considered) != 4 {
		t.Fatalf("considered = %d, want 4", len(decision.Considered))
	}
	if decision.Considered[0].Reason != "deployment disabled" {
		t.Fatalf("first exclusion = %q", decision.Considered[0].Reason)
	}
	if decision.SelectedReason == "" {
		t.Fatal("SelectedReason is empty")
	}
}

func TestWeightedSelectorReturnsNoEligibleCandidate(t *testing.T) {
	selector := newWeightedSelectorWithSource(fixedSource{value: 0})
	_, err := selector.Select(context.Background(), []Candidate{{
		DeploymentID: "down",
		Enabled:      true,
		Healthy:      false,
		Weight:       1,
	}})
	if !errors.Is(err, ErrNoEligibleCandidate) {
		t.Fatalf("Select() error = %v, want ErrNoEligibleCandidate", err)
	}
}

func TestWeightedSelectorHonorsCancelledContext(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	selector := newWeightedSelectorWithSource(fixedSource{value: 0})
	_, err := selector.Select(ctx, []Candidate{{Enabled: true, Healthy: true, Weight: 1}})
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("Select() error = %v, want context.Canceled", err)
	}
}
