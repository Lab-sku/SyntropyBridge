package openai

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/protocol"
)

// EncodeChatResponse renders a canonical response in the Chat Completions format.
// Callers may relay RawBody instead when the upstream protocol is already compatible
// and preserving vendor fields is preferred.
func EncodeChatResponse(response *protocol.CanonicalResponse) ([]byte, error) {
	if response == nil {
		return nil, errors.New("encode OpenAI chat response: response is nil")
	}
	responseID := response.ResponseID
	if responseID == "" {
		responseID = response.RequestID
	}
	created := response.CreatedAt
	if created == 0 {
		created = time.Now().Unix()
	}

	choices := make([]any, 0, len(response.Messages))
	for index, message := range response.Messages {
		encodedMessage, err := encodeMessage(message)
		if err != nil {
			return nil, fmt.Errorf("encode OpenAI chat response message %d: %w", index, err)
		}
		finishReason := any(nil)
		if response.FinishReason != "" {
			finishReason = response.FinishReason
		}
		choices = append(choices, map[string]any{
			"index":         index,
			"message":       encodedMessage,
			"finish_reason": finishReason,
		})
	}

	object := map[string]any{
		"id":      responseID,
		"object":  "chat.completion",
		"created": created,
		"model":   response.Model,
		"choices": choices,
	}
	if response.Usage != nil {
		object["usage"] = encodeUsage(response.Usage)
	}
	for key, raw := range response.Extensions {
		if _, exists := object[key]; !exists {
			object[key] = json.RawMessage(raw)
		}
	}
	return json.Marshal(object)
}

// EncodeChatError returns the error envelope expected by OpenAI-compatible clients.
func EncodeChatError(code, message, errorType string, param *string) []byte {
	if code == "" {
		code = "gateway_error"
	}
	if message == "" {
		message = "gateway request failed"
	}
	if errorType == "" {
		errorType = "gateway_error"
	}
	body, _ := json.Marshal(map[string]any{
		"error": map[string]any{
			"message": message,
			"type":    errorType,
			"param":   param,
			"code":    code,
		},
	})
	return body
}

// ChatStreamEncoder converts semantic stream events into Chat Completions SSE frames.
// For an OpenAI-compatible upstream, wire.chunk events preserve each original JSON
// chunk exactly once. Other adapters can omit Raw and use the semantic fallback.
type ChatStreamEncoder struct {
	responseID         string
	model              string
	lastSourceSequence uint64
	done               bool
}

func NewChatStreamEncoder() *ChatStreamEncoder { return &ChatStreamEncoder{} }

func (e *ChatStreamEncoder) Encode(event protocol.StreamEvent) ([][]byte, error) {
	if e == nil {
		return nil, errors.New("encode OpenAI chat stream: encoder is nil")
	}
	if e.done {
		return nil, nil
	}
	if event.ResponseID != "" {
		e.responseID = event.ResponseID
	}
	if event.Model != "" {
		e.model = event.Model
	}

	if event.Type == protocol.StreamEventResponseEnd {
		e.done = true
		return [][]byte{[]byte("data: [DONE]\n\n")}, nil
	}

	if event.Type == protocol.StreamEventWireChunk && len(event.Raw) > 0 {
		if event.SourceSequence != 0 && event.SourceSequence == e.lastSourceSequence {
			return nil, nil
		}
		if !json.Valid(event.Raw) {
			return nil, errors.New("encode OpenAI chat stream: wire chunk is not valid JSON")
		}
		e.lastSourceSequence = event.SourceSequence
		return [][]byte{sseFrame(event.Raw)}, nil
	}
	if event.SourceSequence != 0 && event.SourceSequence == e.lastSourceSequence {
		return nil, nil
	}

	chunk, ok := e.semanticChunk(event)
	if !ok {
		return nil, nil
	}
	encoded, err := json.Marshal(chunk)
	if err != nil {
		return nil, fmt.Errorf("encode OpenAI chat stream chunk: %w", err)
	}
	return [][]byte{sseFrame(encoded)}, nil
}

func (e *ChatStreamEncoder) semanticChunk(event protocol.StreamEvent) (map[string]any, bool) {
	base := map[string]any{
		"id":      e.responseID,
		"object":  "chat.completion.chunk",
		"created": time.Now().Unix(),
		"model":   e.model,
	}

	switch event.Type {
	case protocol.StreamEventMessageStart:
		base["choices"] = []any{map[string]any{
			"index":         0,
			"delta":         map[string]any{"role": "assistant", "content": nil},
			"finish_reason": nil,
		}}
	case protocol.StreamEventTextDelta:
		if event.TextDelta == nil {
			return nil, false
		}
		base["choices"] = []any{map[string]any{
			"index":         0,
			"delta":         map[string]any{"content": *event.TextDelta},
			"finish_reason": nil,
		}}
	case protocol.StreamEventReasoningDelta:
		if event.ReasoningDelta == nil {
			return nil, false
		}
		base["choices"] = []any{map[string]any{
			"index":         0,
			"delta":         map[string]any{"reasoning_content": *event.ReasoningDelta},
			"finish_reason": nil,
		}}
	case protocol.StreamEventToolCallStart, protocol.StreamEventToolCallDelta:
		if event.ToolCallDelta == nil {
			return nil, false
		}
		delta := event.ToolCallDelta
		function := map[string]any{}
		if delta.Name != "" {
			function["name"] = delta.Name
		}
		if delta.ArgumentsFragment != "" {
			function["arguments"] = delta.ArgumentsFragment
		}
		tool := map[string]any{"index": delta.Index, "function": function}
		if delta.ID != "" {
			tool["id"] = delta.ID
		}
		if delta.Type != "" {
			tool["type"] = delta.Type
		}
		base["choices"] = []any{map[string]any{
			"index":         0,
			"delta":         map[string]any{"tool_calls": []any{tool}},
			"finish_reason": nil,
		}}
	case protocol.StreamEventUsage:
		if event.Usage == nil {
			return nil, false
		}
		base["choices"] = []any{}
		base["usage"] = encodeUsage(event.Usage)
	case protocol.StreamEventMessageEnd:
		finishReason := any(nil)
		if event.FinishReason != nil {
			finishReason = *event.FinishReason
		}
		base["choices"] = []any{map[string]any{
			"index":         0,
			"delta":         map[string]any{},
			"finish_reason": finishReason,
		}}
	default:
		return nil, false
	}
	return base, true
}

func encodeUsage(usage *protocol.Usage) map[string]any {
	result := map[string]any{
		"prompt_tokens":     usage.InputTokens,
		"completion_tokens": usage.OutputTokens,
		"total_tokens":      usage.TotalTokens,
	}
	if usage.CachedInputTokens != 0 {
		result["prompt_tokens_details"] = map[string]any{"cached_tokens": usage.CachedInputTokens}
	}
	if usage.ReasoningTokens != 0 {
		result["completion_tokens_details"] = map[string]any{"reasoning_tokens": usage.ReasoningTokens}
	}
	return result
}

func sseFrame(data []byte) []byte {
	var output bytes.Buffer
	for _, line := range bytes.Split(data, []byte("\n")) {
		output.WriteString("data: ")
		output.Write(line)
		output.WriteByte('\n')
	}
	output.WriteByte('\n')
	return output.Bytes()
}
