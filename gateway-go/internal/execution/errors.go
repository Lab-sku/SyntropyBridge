package execution

import "fmt"

// TransportError means no usable HTTP response was obtained. Retry policy may inspect
// it separately from provider HTTP errors.
type TransportError struct {
	Attempt Attempt
	Cause   error
}

func (e *TransportError) Error() string {
	if e == nil {
		return "<nil>"
	}
	return fmt.Sprintf("upstream transport failed for deployment %s: %v", e.Attempt.DeploymentID, e.Cause)
}

func (e *TransportError) Unwrap() error { return e.Cause }

// AttemptError associates a decoded provider or stream error with the safe attempt
// metadata used by traces.
type AttemptError struct {
	Attempt Attempt
	Cause   error
}

func (e *AttemptError) Error() string {
	if e == nil {
		return "<nil>"
	}
	return fmt.Sprintf("upstream attempt failed for deployment %s: %v", e.Attempt.DeploymentID, e.Cause)
}

func (e *AttemptError) Unwrap() error { return e.Cause }
