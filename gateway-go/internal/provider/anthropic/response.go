package anthropic

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/protocol"
)

type usageBody struct {
	InputTokens              int64 `json:"input_tokens"`
	OutputTokens             int64 `json:"output_tokens"`
	CacheCreationInputTokens int64 `json:"cache_creation_input_tokens"`
	CacheReadInputTokens     int64 `json:"cache_read_input_tokens"`
}

func (u usageBody) canonical() *protocol.Usage {
	return &protocol.Usage{
		InputTokens:       u.InputTokens,
		OutputTokens:      u.OutputTokens,
		CachedInputTokens: u.CacheReadInputTokens,
		TotalTokens:       u.InputTokens + u.OutputTokens,
	}
}

type messageResponse struct {
	ID           string            `json:"id"`
	Type         string            `json:"type"`
	Role         string            `json:"role"`
	Model        string            `json:"model"`
	Content      []json.RawMessage `json:"content"`
	StopReason   *string           `json:"stop_reason"`
	StopSequence *string           `json:"stop_sequence"`
	Usage        usageBody         `json:"usage"`
}

func decodeMessagesResponse(body []byte) (*protocol.CanonicalResponse, error) {
	var envelope messageResponse
	if err := json.Unmarshal(body, &envelope); err != nil {
		return nil, fmt.Errorf("decode Anthropic response JSON: %w", err)
	}
	if envelope.Type != "" && envelope.Type != "message" {
		return nil, fmt.Errorf("decode Anthropic response: unexpected type %q", envelope.Type)
	}
	message := protocol.Message{Role: protocol.RoleAssistant}
	for index, raw := range envelope.Content {
		part, err := decodeContentBlock(raw)
		if err != nil {
			return nil, fmt.Errorf("decode Anthropic content block %d: %w", index, err)
		}
		message.Content = append(message.Content, part)
	}
	result := &protocol.CanonicalResponse{
		ResponseID: envelope.ID,
		ProviderID: Name,
		Model:      envelope.Model,
		Messages:   []protocol.Message{message},
		Usage:      envelope.Usage.canonical(),
		RawBody:    append(json.RawMessage(nil), body...),
		Extensions: make(map[string]json.RawMessage),
	}
	if envelope.StopReason != nil {
		result.FinishReason = normalizeStopReason(*envelope.StopReason)
		raw, _ := json.Marshal(*envelope.StopReason)
		result.Extensions["anthropic.stop_reason"] = raw
	}
	if envelope.StopSequence != nil {
		raw, _ := json.Marshal(*envelope.StopSequence)
		result.Extensions["anthropic.stop_sequence"] = raw
	}
	if len(result.Extensions) == 0 {
		result.Extensions = nil
	}
	return result, nil
}

func decodeContentBlock(raw json.RawMessage) (protocol.ContentPart, error) {
	var header struct {
		Type string `json:"type"`
	}
	if err := json.Unmarshal(raw, &header); err != nil {
		return protocol.ContentPart{}, err
	}
	part := protocol.ContentPart{Type: header.Type, Raw: append(json.RawMessage(nil), raw...)}
	switch header.Type {
	case "text":
		var block struct {
			Text string `json:"text"`
		}
		if err := json.Unmarshal(raw, &block); err != nil {
			return protocol.ContentPart{}, err
		}
		part.Text = &block.Text
	case "tool_use":
		var block struct {
			ID    string          `json:"id"`
			Name  string          `json:"name"`
			Input json.RawMessage `json:"input"`
		}
		if err := json.Unmarshal(raw, &block); err != nil {
			return protocol.ContentPart{}, err
		}
		if len(bytes.TrimSpace(block.Input)) == 0 {
			block.Input = json.RawMessage(`{}`)
		}
		part.ToolCall = &protocol.ToolCall{
			ID:        block.ID,
			Type:      "function",
			Name:      block.Name,
			Arguments: append(json.RawMessage(nil), block.Input...),
		}
	case "thinking":
		var block struct {
			Thinking  string          `json:"thinking"`
			Signature json.RawMessage `json:"signature"`
		}
		if err := json.Unmarshal(raw, &block); err != nil {
			return protocol.ContentPart{}, err
		}
		part.Reasoning = &protocol.ReasoningBlock{
			Text:      block.Thinking,
			Signature: append(json.RawMessage(nil), block.Signature...),
		}
	case "redacted_thinking":
		// Preserve encrypted/redacted thinking exactly in Raw. It must not be
		// flattened into user-visible assistant text.
	case "tool_result":
		return protocol.ContentPart{}, errors.New("tool_result is not valid in an Anthropic assistant response")
	default:
		// New content block types are retained in Raw for forward compatibility.
	}
	return part, nil
}

func normalizeStopReason(reason string) string {
	switch reason {
	case "end_turn", "stop_sequence":
		return "stop"
	case "max_tokens":
		return "length"
	case "tool_use":
		return "tool_calls"
	case "refusal":
		return "content_filter"
	default:
		return reason
	}
}
