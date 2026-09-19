package anthropic

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"

	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/protocol"
)

const maxSSEEventBytes = 4 << 20

type streamState struct {
	responseID string
	model      string
	inputUsage usageBody
	blocks     map[int]streamBlock
}

type streamBlock struct {
	kind string
	id   string
	name string
}

func (a *Adapter) DecodeStream(
	ctx context.Context,
	resp *http.Response,
	emit func(protocol.StreamEvent) error,
) error {
	if resp == nil {
		return errors.New("decode Anthropic stream: response is nil")
	}
	if emit == nil {
		return errors.New("decode Anthropic stream: emit is nil")
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		body, err := readLimited(resp.Body, maxResponseBodyBytes)
		if err != nil {
			return err
		}
		return a.NormalizeError(ctx, resp, body)
	}

	reader := bufio.NewReaderSize(resp.Body, 64<<10)
	state := streamState{blocks: make(map[int]streamBlock)}
	var sequence uint64
	var sourceSequence uint64
	started := false
	stopped := false

	emitEvent := func(event protocol.StreamEvent) error {
		sequence++
		event.Sequence = sequence
		event.SourceSequence = sourceSequence
		if event.ResponseID == "" {
			event.ResponseID = state.responseID
		}
		if event.Model == "" {
			event.Model = state.model
		}
		return emit(event)
	}

	for {
		if err := ctx.Err(); err != nil {
			return err
		}
		eventName, data, err := readSSEEvent(reader, maxSSEEventBytes)
		if err != nil {
			if errors.Is(err, io.EOF) {
				if !started {
					return nil
				}
				if stopped {
					return nil
				}
				return io.ErrUnexpectedEOF
			}
			return fmt.Errorf("read Anthropic stream: %w", err)
		}
		if len(data) == 0 {
			continue
		}
		sourceSequence++
		var envelope map[string]json.RawMessage
		if err := json.Unmarshal(data, &envelope); err != nil {
			return fmt.Errorf("decode Anthropic stream JSON: %w", err)
		}
		var wireType string
		_ = json.Unmarshal(envelope["type"], &wireType)
		if eventName == "" {
			eventName = wireType
		}
		raw := append(json.RawMessage(nil), data...)

		switch eventName {
		case "ping":
			continue
		case "message_start":
			var message struct {
				ID    string    `json:"id"`
				Model string    `json:"model"`
				Usage usageBody `json:"usage"`
			}
			if err := json.Unmarshal(envelope["message"], &message); err != nil {
				return fmt.Errorf("decode Anthropic message_start: %w", err)
			}
			state.responseID = message.ID
			state.model = message.Model
			state.inputUsage = message.Usage
			started = true
			if err := emitEvent(protocol.StreamEvent{Type: protocol.StreamEventResponseStart, Raw: raw}); err != nil {
				return err
			}
			if err := emitEvent(protocol.StreamEvent{Type: protocol.StreamEventMessageStart, Raw: raw}); err != nil {
				return err
			}
			if message.Usage.InputTokens != 0 || message.Usage.CacheReadInputTokens != 0 || message.Usage.CacheCreationInputTokens != 0 {
				if err := emitEvent(protocol.StreamEvent{Type: protocol.StreamEventUsage, Usage: message.Usage.canonical(), Raw: raw}); err != nil {
					return err
				}
			}
		case "content_block_start":
			var index int
			_ = json.Unmarshal(envelope["index"], &index)
			var block struct {
				Type  string          `json:"type"`
				ID    string          `json:"id"`
				Name  string          `json:"name"`
				Input json.RawMessage `json:"input"`
			}
			if err := json.Unmarshal(envelope["content_block"], &block); err != nil {
				return fmt.Errorf("decode Anthropic content_block_start: %w", err)
			}
			state.blocks[index] = streamBlock{kind: block.Type, id: block.ID, name: block.Name}
			if block.Type == "tool_use" {
				fragment := ""
				trimmed := bytes.TrimSpace(block.Input)
				if len(trimmed) > 0 && !bytes.Equal(trimmed, []byte(`{}`)) {
					fragment = string(trimmed)
				}
				if err := emitEvent(protocol.StreamEvent{
					Type: protocol.StreamEventToolCallStart,
					ToolCallDelta: &protocol.ToolCallDelta{
						Index: index, ID: block.ID, Type: "function", Name: block.Name, ArgumentsFragment: fragment,
					},
					Raw: raw,
				}); err != nil {
					return err
				}
			}
		case "content_block_delta":
			var index int
			_ = json.Unmarshal(envelope["index"], &index)
			var delta struct {
				Type        string `json:"type"`
				Text        string `json:"text"`
				PartialJSON string `json:"partial_json"`
				Thinking    string `json:"thinking"`
				Signature   string `json:"signature"`
			}
			if err := json.Unmarshal(envelope["delta"], &delta); err != nil {
				return fmt.Errorf("decode Anthropic content_block_delta: %w", err)
			}
			switch delta.Type {
			case "text_delta":
				text := delta.Text
				if err := emitEvent(protocol.StreamEvent{Type: protocol.StreamEventTextDelta, TextDelta: &text, Raw: raw}); err != nil {
					return err
				}
			case "input_json_delta":
				block := state.blocks[index]
				if err := emitEvent(protocol.StreamEvent{
					Type: protocol.StreamEventToolCallDelta,
					ToolCallDelta: &protocol.ToolCallDelta{
						Index: index, ID: block.id, Type: "function", Name: block.name, ArgumentsFragment: delta.PartialJSON,
					},
					Raw: raw,
				}); err != nil {
					return err
				}
			case "thinking_delta":
				thinking := delta.Thinking
				if err := emitEvent(protocol.StreamEvent{Type: protocol.StreamEventReasoningDelta, ReasoningDelta: &thinking, Raw: raw}); err != nil {
					return err
				}
			case "signature_delta":
				signature, _ := json.Marshal(delta.Signature)
				if err := emitEvent(protocol.StreamEvent{
					Type:       protocol.StreamEventReasoningDelta,
					Extensions: map[string]json.RawMessage{"anthropic.signature_delta": signature},
					Raw:        raw,
				}); err != nil {
					return err
				}
			default:
				if err := emitEvent(protocol.StreamEvent{Type: protocol.StreamEventType("anthropic." + delta.Type), Raw: raw}); err != nil {
					return err
				}
			}
		case "content_block_stop":
			var index int
			_ = json.Unmarshal(envelope["index"], &index)
			block := state.blocks[index]
			if block.kind == "tool_use" {
				if err := emitEvent(protocol.StreamEvent{
					Type: protocol.StreamEventToolCallEnd,
					ToolCallDelta: &protocol.ToolCallDelta{
						Index: index, ID: block.id, Type: "function", Name: block.name,
					},
					Raw: raw,
				}); err != nil {
					return err
				}
			}
			delete(state.blocks, index)
		case "message_delta":
			var delta struct {
				StopReason   *string `json:"stop_reason"`
				StopSequence *string `json:"stop_sequence"`
			}
			if err := json.Unmarshal(envelope["delta"], &delta); err != nil {
				return fmt.Errorf("decode Anthropic message_delta: %w", err)
			}
			var outputUsage usageBody
			_ = json.Unmarshal(envelope["usage"], &outputUsage)
			combined := state.inputUsage
			combined.OutputTokens = outputUsage.OutputTokens
			if err := emitEvent(protocol.StreamEvent{Type: protocol.StreamEventUsage, Usage: combined.canonical(), Raw: raw}); err != nil {
				return err
			}
			if delta.StopReason != nil {
				finish := normalizeStopReason(*delta.StopReason)
				extensions := map[string]json.RawMessage{}
				rawReason, _ := json.Marshal(*delta.StopReason)
				extensions["anthropic.stop_reason"] = rawReason
				if delta.StopSequence != nil {
					rawSequence, _ := json.Marshal(*delta.StopSequence)
					extensions["anthropic.stop_sequence"] = rawSequence
				}
				if err := emitEvent(protocol.StreamEvent{Type: protocol.StreamEventMessageEnd, FinishReason: &finish, Extensions: extensions, Raw: raw}); err != nil {
					return err
				}
			}
		case "message_stop":
			stopped = true
			return emitEvent(protocol.StreamEvent{Type: protocol.StreamEventResponseEnd, Raw: raw})
		case "error":
			var errorEnvelope struct {
				Error struct {
					Type    string `json:"type"`
					Message string `json:"message"`
				} `json:"error"`
			}
			if err := json.Unmarshal(data, &errorEnvelope); err != nil {
				return fmt.Errorf("decode Anthropic stream error: %w", err)
			}
			return &protocol.GatewayError{
				Code:         valueOrDefault(errorEnvelope.Error.Type, "upstream_stream_error"),
				ProviderCode: errorEnvelope.Error.Type,
				Message:      valueOrDefault(errorEnvelope.Error.Message, "Anthropic stream failed"),
				Retryable:    isRetryableErrorType(errorEnvelope.Error.Type),
				Raw:          raw,
			}
		default:
			// Anthropic's versioning policy permits new stream event types. Preserve
			// them for tracing and allow downstream encoders to ignore what they do
			// not yet understand.
			if err := emitEvent(protocol.StreamEvent{Type: protocol.StreamEventType("anthropic." + eventName), Raw: raw}); err != nil {
				return err
			}
		}
	}
}

func readSSEEvent(reader *bufio.Reader, limit int) (string, []byte, error) {
	var eventName string
	var data bytes.Buffer
	seenLine := false
	for {
		line, err := reader.ReadString('\n')
		if err != nil && len(line) == 0 {
			if errors.Is(err, io.EOF) && seenLine {
				return eventName, data.Bytes(), nil
			}
			return "", nil, err
		}
		seenLine = true
		line = strings.TrimSuffix(line, "\n")
		line = strings.TrimSuffix(line, "\r")
		if line == "" {
			return eventName, data.Bytes(), nil
		}
		if strings.HasPrefix(line, ":") {
			if err != nil {
				return eventName, data.Bytes(), nil
			}
			continue
		}
		switch {
		case strings.HasPrefix(line, "event:"):
			eventName = strings.TrimSpace(strings.TrimPrefix(line, "event:"))
		case strings.HasPrefix(line, "data:"):
			value := strings.TrimPrefix(line, "data:")
			value = strings.TrimPrefix(value, " ")
			if data.Len() > 0 {
				data.WriteByte('\n')
			}
			data.WriteString(value)
			if data.Len() > limit {
				return "", nil, fmt.Errorf("SSE event exceeds %d bytes", limit)
			}
		}
		if err != nil {
			return eventName, data.Bytes(), nil
		}
	}
}

func isRetryableErrorType(errorType string) bool {
	switch errorType {
	case "rate_limit_error", "api_error", "overloaded_error", "timeout_error":
		return true
	default:
		return false
	}
}

func valueOrDefault(value, fallback string) string {
	if value == "" {
		return fallback
	}
	return value
}
