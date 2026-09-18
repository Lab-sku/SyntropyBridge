package protocol

import "encoding/json"

// StreamEventType describes semantic events rather than a provider's wire event name.
type StreamEventType string

const (
	StreamEventResponseStart  StreamEventType = "response.start"
	StreamEventMessageStart   StreamEventType = "message.start"
	StreamEventTextDelta      StreamEventType = "text.delta"
	StreamEventReasoningDelta StreamEventType = "reasoning.delta"
	StreamEventToolCallStart  StreamEventType = "tool_call.start"
	StreamEventToolCallDelta  StreamEventType = "tool_call.delta"
	StreamEventToolCallEnd    StreamEventType = "tool_call.end"
	StreamEventUsage          StreamEventType = "usage"
	StreamEventMessageEnd     StreamEventType = "message.end"
	StreamEventResponseEnd    StreamEventType = "response.end"
	StreamEventError          StreamEventType = "error"
)

type ToolCallDelta struct {
	Index             int    `json:"index"`
	ID                string `json:"id,omitempty"`
	Type              string `json:"type,omitempty"`
	Name              string `json:"name,omitempty"`
	ArgumentsFragment string `json:"arguments_fragment,omitempty"`
}

// StreamEvent preserves ordering with Sequence and provider provenance with Raw.
type StreamEvent struct {
	Type           StreamEventType            `json:"type"`
	Sequence       uint64                     `json:"sequence"`
	ResponseID     string                     `json:"response_id,omitempty"`
	Model          string                     `json:"model,omitempty"`
	FinishReason   *string                    `json:"finish_reason,omitempty"`
	TextDelta      *string                    `json:"text_delta,omitempty"`
	ReasoningDelta *string                    `json:"reasoning_delta,omitempty"`
	ToolCallDelta  *ToolCallDelta             `json:"tool_call_delta,omitempty"`
	Usage          *Usage                     `json:"usage,omitempty"`
	Error          *GatewayError              `json:"error,omitempty"`
	Extensions     map[string]json.RawMessage `json:"extensions,omitempty"`
	Raw            json.RawMessage            `json:"raw,omitempty"`
}

// StartsOutput reports whether replaying the request on another deployment could
// duplicate user-visible output. Retry policy must treat this as a hard boundary.
func (e StreamEvent) StartsOutput() bool {
	switch e.Type {
	case StreamEventTextDelta,
		StreamEventReasoningDelta,
		StreamEventToolCallStart,
		StreamEventToolCallDelta,
		StreamEventToolCallEnd:
		return true
	default:
		return false
	}
}
