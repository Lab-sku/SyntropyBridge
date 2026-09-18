// Package openai translates the OpenAI Chat Completions wire format to and from the
// gateway's canonical representation. Provider adapters may use RawBody for a
// same-protocol fast path, but parsing still populates the fields required for policy,
// capability checks and tracing.
package openai

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/protocol"
)

var ErrInvalidChatRequest = errors.New("invalid OpenAI chat completion request")

var knownRequestFields = map[string]struct{}{
	"model": {}, "messages": {}, "stream": {}, "stream_options": {},
	"max_tokens": {}, "max_completion_tokens": {}, "temperature": {}, "top_p": {},
	"stop": {}, "seed": {}, "tools": {}, "tool_choice": {}, "parallel_tool_calls": {},
	"response_format": {}, "reasoning_effort": {}, "metadata": {},
	"presence_penalty": {}, "frequency_penalty": {}, "logit_bias": {}, "logprobs": {},
	"top_logprobs": {}, "n": {}, "user": {}, "store": {}, "service_tier": {},
	"modalities": {}, "audio": {}, "prediction": {}, "web_search_options": {},
}

// DecodeChatRequest parses enough of a Chat Completions request to route and validate
// it, while retaining the exact JSON for a lossless same-protocol forwarding path.
func DecodeChatRequest(body []byte, requestID string) (*protocol.CanonicalRequest, error) {
	object, err := decodeObject(body)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrInvalidChatRequest, err)
	}

	var model string
	if err := decodeRequired(object, "model", &model); err != nil || model == "" {
		return nil, fmt.Errorf("%w: model is required", ErrInvalidChatRequest)
	}

	var rawMessages []json.RawMessage
	if err := decodeRequired(object, "messages", &rawMessages); err != nil || len(rawMessages) == 0 {
		return nil, fmt.Errorf("%w: messages must be a non-empty array", ErrInvalidChatRequest)
	}
	messages := make([]protocol.Message, 0, len(rawMessages))
	for index, raw := range rawMessages {
		message, err := DecodeMessage(raw)
		if err != nil {
			return nil, fmt.Errorf("%w: messages[%d]: %v", ErrInvalidChatRequest, index, err)
		}
		messages = append(messages, message)
	}

	req := &protocol.CanonicalRequest{
		RequestID:  requestID,
		Protocol:   protocol.ProtocolOpenAIChat,
		Operation:  protocol.OperationChat,
		Model:      model,
		Messages:   messages,
		Parameters: protocol.GenerationParameters{},
		RawBody:    append(json.RawMessage(nil), body...),
		Extensions: make(map[string]json.RawMessage),
	}

	decodeOptional(object, "stream", &req.Stream)
	decodeOptional(object, "max_completion_tokens", &req.Parameters.MaxOutputTokens)
	if req.Parameters.MaxOutputTokens == nil {
		decodeOptional(object, "max_tokens", &req.Parameters.MaxOutputTokens)
	}
	decodeOptional(object, "temperature", &req.Parameters.Temperature)
	decodeOptional(object, "top_p", &req.Parameters.TopP)
	decodeOptional(object, "seed", &req.Parameters.Seed)
	decodeStop(object["stop"], &req.Parameters.Stop)

	if raw := object["tool_choice"]; len(raw) > 0 {
		req.ToolChoice = append(json.RawMessage(nil), raw...)
	}
	if raw := object["response_format"]; len(raw) > 0 {
		req.ResponseFormat = append(json.RawMessage(nil), raw...)
	}
	if raw := object["reasoning_effort"]; len(raw) > 0 {
		var effort string
		if json.Unmarshal(raw, &effort) == nil && effort != "" {
			req.Reasoning = &protocol.ReasoningConfig{Effort: &effort}
		}
	}
	if raw := object["metadata"]; len(raw) > 0 {
		_ = json.Unmarshal(raw, &req.Metadata)
	}
	if raw := object["tools"]; len(raw) > 0 {
		req.Tools = decodeTools(raw)
	}

	for key, raw := range object {
		if _, known := knownRequestFields[key]; known {
			continue
		}
		req.Extensions[key] = append(json.RawMessage(nil), raw...)
	}
	if len(req.Extensions) == 0 {
		req.Extensions = nil
	}
	return req, nil
}

// EncodeChatRequest creates a Chat Completions request when a raw same-protocol body
// is unavailable (for example after cross-protocol conversion).
func EncodeChatRequest(req *protocol.CanonicalRequest) ([]byte, error) {
	if req == nil {
		return nil, fmt.Errorf("encode OpenAI chat request: request is nil")
	}
	object := make(map[string]any)
	object["model"] = req.Model
	object["stream"] = req.Stream

	messages := make([]any, 0, len(req.Messages))
	for _, message := range req.Messages {
		encoded, err := encodeMessage(message)
		if err != nil {
			return nil, err
		}
		messages = append(messages, encoded)
	}
	object["messages"] = messages

	if req.Parameters.MaxOutputTokens != nil {
		object["max_completion_tokens"] = *req.Parameters.MaxOutputTokens
	}
	if req.Parameters.Temperature != nil {
		object["temperature"] = *req.Parameters.Temperature
	}
	if req.Parameters.TopP != nil {
		object["top_p"] = *req.Parameters.TopP
	}
	if req.Parameters.Seed != nil {
		object["seed"] = *req.Parameters.Seed
	}
	if len(req.Parameters.Stop) > 0 {
		object["stop"] = req.Parameters.Stop
	}
	if len(req.Tools) > 0 {
		tools := make([]any, 0, len(req.Tools))
		for _, tool := range req.Tools {
			function := map[string]any{
				"name":       tool.Name,
				"parameters": rawOrEmptyObject(tool.InputSchema),
			}
			if tool.Description != "" {
				function["description"] = tool.Description
			}
			if tool.Strict != nil {
				function["strict"] = *tool.Strict
			}
			tools = append(tools, map[string]any{"type": "function", "function": function})
		}
		object["tools"] = tools
	}
	if len(req.ToolChoice) > 0 {
		object["tool_choice"] = json.RawMessage(req.ToolChoice)
	}
	if len(req.ResponseFormat) > 0 {
		object["response_format"] = json.RawMessage(req.ResponseFormat)
	}
	if req.Reasoning != nil && req.Reasoning.Effort != nil {
		object["reasoning_effort"] = *req.Reasoning.Effort
	}
	if len(req.Metadata) > 0 {
		object["metadata"] = req.Metadata
	}
	for key, raw := range req.Extensions {
		if _, exists := object[key]; !exists {
			object[key] = json.RawMessage(raw)
		}
	}
	return json.Marshal(object)
}

// ReplaceModelAndStream preserves every top-level field while applying routing output.
func ReplaceModelAndStream(body []byte, model string, stream bool) ([]byte, error) {
	object, err := decodeObject(body)
	if err != nil {
		return nil, err
	}
	encodedModel, _ := json.Marshal(model)
	encodedStream, _ := json.Marshal(stream)
	object["model"] = encodedModel
	object["stream"] = encodedStream
	return json.Marshal(object)
}

// DecodeMessage handles both string and multipart content and preserves the original
// object for fields that are not yet represented canonically.
func DecodeMessage(raw json.RawMessage) (protocol.Message, error) {
	object, err := decodeObject(raw)
	if err != nil {
		return protocol.Message{}, err
	}
	var role string
	if err := decodeRequired(object, "role", &role); err != nil || role == "" {
		return protocol.Message{}, errors.New("role is required")
	}
	message := protocol.Message{
		Role:       protocol.Role(role),
		Extensions: make(map[string]json.RawMessage),
		Raw:        append(json.RawMessage(nil), raw...),
	}
	decodeOptional(object, "name", &message.Name)
	decodeOptional(object, "tool_call_id", &message.ToolCallID)
	decodeOptional(object, "refusal", &message.Refusal)
	if annotations := object["annotations"]; len(annotations) > 0 {
		_ = json.Unmarshal(annotations, &message.Annotations)
	}

	if content := object["content"]; len(content) > 0 && !bytes.Equal(bytes.TrimSpace(content), []byte("null")) {
		parts, err := decodeContent(content)
		if err != nil {
			return protocol.Message{}, fmt.Errorf("content: %w", err)
		}
		message.Content = append(message.Content, parts...)
	}
	if toolCalls := object["tool_calls"]; len(toolCalls) > 0 {
		parts, err := decodeToolCalls(toolCalls)
		if err != nil {
			return protocol.Message{}, fmt.Errorf("tool_calls: %w", err)
		}
		message.Content = append(message.Content, parts...)
	}

	for key, value := range object {
		switch key {
		case "role", "name", "content", "tool_call_id", "tool_calls", "refusal", "annotations":
			continue
		default:
			message.Extensions[key] = append(json.RawMessage(nil), value...)
		}
	}
	if len(message.Extensions) == 0 {
		message.Extensions = nil
	}
	return message, nil
}

func decodeContent(raw json.RawMessage) ([]protocol.ContentPart, error) {
	var text string
	if err := json.Unmarshal(raw, &text); err == nil {
		return []protocol.ContentPart{{Type: "text", Text: &text}}, nil
	}

	var items []json.RawMessage
	if err := json.Unmarshal(raw, &items); err != nil {
		return nil, errors.New("must be a string, null, or array")
	}
	parts := make([]protocol.ContentPart, 0, len(items))
	for _, item := range items {
		object, err := decodeObject(item)
		if err != nil {
			return nil, err
		}
		var kind string
		_ = json.Unmarshal(object["type"], &kind)
		part := protocol.ContentPart{Type: kind, Raw: append(json.RawMessage(nil), item...)}
		switch kind {
		case "text", "input_text", "output_text":
			_ = json.Unmarshal(object["text"], &part.Text)
		case "image_url", "input_image":
			if imageRaw := object["image_url"]; len(imageRaw) > 0 {
				var imageObject map[string]json.RawMessage
				if json.Unmarshal(imageRaw, &imageObject) == nil {
					var image protocol.ImageURL
					_ = json.Unmarshal(imageObject["url"], &image.URL)
					_ = json.Unmarshal(imageObject["detail"], &image.Detail)
					part.ImageURL = &image
				} else {
					var url string
					if json.Unmarshal(imageRaw, &url) == nil {
						part.ImageURL = &protocol.ImageURL{URL: url}
					}
				}
			}
		}
		parts = append(parts, part)
	}
	return parts, nil
}

func decodeToolCalls(raw json.RawMessage) ([]protocol.ContentPart, error) {
	var calls []struct {
		ID       string `json:"id"`
		Type     string `json:"type"`
		Function struct {
			Name      string `json:"name"`
			Arguments string `json:"arguments"`
		} `json:"function"`
	}
	if err := json.Unmarshal(raw, &calls); err != nil {
		return nil, err
	}
	parts := make([]protocol.ContentPart, 0, len(calls))
	for _, call := range calls {
		arguments := normalizeArguments(call.Function.Arguments)
		parts = append(parts, protocol.ContentPart{
			Type: "tool_call",
			ToolCall: &protocol.ToolCall{
				ID:        call.ID,
				Type:      call.Type,
				Name:      call.Function.Name,
				Arguments: arguments,
			},
		})
	}
	return parts, nil
}

func decodeTools(raw json.RawMessage) []protocol.ToolDefinition {
	var tools []struct {
		Type     string `json:"type"`
		Function struct {
			Name        string          `json:"name"`
			Description string          `json:"description"`
			Parameters  json.RawMessage `json:"parameters"`
			Strict      *bool           `json:"strict"`
		} `json:"function"`
	}
	if json.Unmarshal(raw, &tools) != nil {
		return nil
	}
	result := make([]protocol.ToolDefinition, 0, len(tools))
	for _, tool := range tools {
		if tool.Type != "function" || tool.Function.Name == "" {
			continue
		}
		result = append(result, protocol.ToolDefinition{
			Name:        tool.Function.Name,
			Description: tool.Function.Description,
			InputSchema: append(json.RawMessage(nil), tool.Function.Parameters...),
			Strict:      tool.Function.Strict,
		})
	}
	return result
}

func encodeMessage(message protocol.Message) (map[string]any, error) {
	object := map[string]any{"role": string(message.Role)}
	if message.Name != nil {
		object["name"] = *message.Name
	}
	if message.ToolCallID != nil {
		object["tool_call_id"] = *message.ToolCallID
	}
	if message.Refusal != nil {
		object["refusal"] = *message.Refusal
	}
	if len(message.Annotations) > 0 {
		object["annotations"] = message.Annotations
	}

	var textParts []string
	var multipart []any
	var toolCalls []any
	for _, part := range message.Content {
		switch {
		case part.ToolCall != nil:
			arguments := string(part.ToolCall.Arguments)
			if arguments == "" {
				arguments = "{}"
			}
			toolCalls = append(toolCalls, map[string]any{
				"id":   part.ToolCall.ID,
				"type": valueOrDefault(part.ToolCall.Type, "function"),
				"function": map[string]any{
					"name":      part.ToolCall.Name,
					"arguments": arguments,
				},
			})
		case part.Text != nil:
			textParts = append(textParts, *part.Text)
			multipart = append(multipart, map[string]any{"type": "text", "text": *part.Text})
		case part.ImageURL != nil:
			multipart = append(multipart, map[string]any{"type": "image_url", "image_url": part.ImageURL})
		case len(part.Raw) > 0:
			multipart = append(multipart, json.RawMessage(part.Raw))
		}
	}
	if len(toolCalls) > 0 {
		object["tool_calls"] = toolCalls
	}
	if len(multipart) == len(textParts) && len(textParts) == 1 {
		object["content"] = textParts[0]
	} else if len(multipart) > 0 {
		object["content"] = multipart
	} else {
		object["content"] = nil
	}
	for key, raw := range message.Extensions {
		if _, exists := object[key]; !exists {
			object[key] = json.RawMessage(raw)
		}
	}
	return object, nil
}

func decodeObject(raw []byte) (map[string]json.RawMessage, error) {
	var object map[string]json.RawMessage
	if err := json.Unmarshal(raw, &object); err != nil {
		return nil, err
	}
	if object == nil {
		return nil, errors.New("expected JSON object")
	}
	return object, nil
}

func decodeRequired(object map[string]json.RawMessage, key string, target any) error {
	raw, ok := object[key]
	if !ok {
		return fmt.Errorf("%s is required", key)
	}
	return json.Unmarshal(raw, target)
}

func decodeOptional(object map[string]json.RawMessage, key string, target any) {
	if raw, ok := object[key]; ok && len(raw) > 0 && !bytes.Equal(bytes.TrimSpace(raw), []byte("null")) {
		_ = json.Unmarshal(raw, target)
	}
}

func decodeStop(raw json.RawMessage, target *[]string) {
	if len(raw) == 0 || bytes.Equal(bytes.TrimSpace(raw), []byte("null")) {
		return
	}
	var one string
	if json.Unmarshal(raw, &one) == nil {
		*target = []string{one}
		return
	}
	_ = json.Unmarshal(raw, target)
}

func normalizeArguments(value string) json.RawMessage {
	trimmed := bytes.TrimSpace([]byte(value))
	if len(trimmed) > 0 && json.Valid(trimmed) {
		return append(json.RawMessage(nil), trimmed...)
	}
	encoded, _ := json.Marshal(value)
	return encoded
}

func rawOrEmptyObject(raw json.RawMessage) any {
	if len(raw) == 0 {
		return map[string]any{}
	}
	return json.RawMessage(raw)
}

func valueOrDefault(value, fallback string) string {
	if value == "" {
		return fallback
	}
	return value
}
