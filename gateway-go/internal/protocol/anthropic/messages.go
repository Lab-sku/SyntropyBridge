package anthropic

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/protocol"
)

var ErrInvalidMessagesRequest = errors.New("invalid Anthropic Messages request")

func DecodeMessagesRequest(body []byte, requestID string) (*protocol.CanonicalRequest, error) {
	object, err := decodeObject(body)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrInvalidMessagesRequest, err)
	}
	model := jsonString(object["model"])
	if strings.TrimSpace(model) == "" {
		return nil, fmt.Errorf("%w: model is required", ErrInvalidMessagesRequest)
	}
	var maxTokens int
	if err := json.Unmarshal(object["max_tokens"], &maxTokens); err != nil || maxTokens < 1 {
		return nil, fmt.Errorf("%w: max_tokens must be a positive integer", ErrInvalidMessagesRequest)
	}

	var rawMessages []json.RawMessage
	if err := json.Unmarshal(object["messages"], &rawMessages); err != nil || len(rawMessages) == 0 {
		return nil, fmt.Errorf("%w: messages must be a non-empty array", ErrInvalidMessagesRequest)
	}

	req := &protocol.CanonicalRequest{
		RequestID: requestID,
		Protocol:  protocol.ProtocolAnthropic,
		Operation: protocol.OperationChat,
		Model:     model,
		Parameters: protocol.GenerationParameters{
			MaxOutputTokens: &maxTokens,
		},
		RawBody: append(json.RawMessage(nil), body...),
	}

	if systemRaw := object["system"]; nonNull(systemRaw) {
		parts, err := decodeSystem(systemRaw)
		if err != nil {
			return nil, fmt.Errorf("%w: system: %v", ErrInvalidMessagesRequest, err)
		}
		if len(parts) > 0 {
			req.Messages = append(req.Messages, protocol.Message{Role: protocol.RoleSystem, Content: parts})
		}
	}

	for index, raw := range rawMessages {
		messages, err := decodeMessage(raw)
		if err != nil {
			return nil, fmt.Errorf("%w: messages[%d]: %v", ErrInvalidMessagesRequest, index, err)
		}
		req.Messages = append(req.Messages, messages...)
	}

	decodeOptional(object, "stream", &req.Stream)
	decodeOptional(object, "temperature", &req.Parameters.Temperature)
	decodeOptional(object, "top_p", &req.Parameters.TopP)
	decodeOptional(object, "top_k", &req.Parameters.TopK)
	if raw := object["stop_sequences"]; nonNull(raw) {
		_ = json.Unmarshal(raw, &req.Parameters.Stop)
	}

	if raw := object["tools"]; nonNull(raw) {
		tools, err := decodeTools(raw)
		if err != nil {
			return nil, fmt.Errorf("%w: tools: %v", ErrInvalidMessagesRequest, err)
		}
		req.Tools = tools
	}
	if raw := object["tool_choice"]; nonNull(raw) {
		choice, parallel, err := decodeToolChoice(raw)
		if err != nil {
			return nil, fmt.Errorf("%w: tool_choice: %v", ErrInvalidMessagesRequest, err)
		}
		req.ToolChoice = choice
		if parallel != nil {
			req.Extensions = ensureExtensions(req.Extensions)
			encoded, _ := json.Marshal(*parallel)
			req.Extensions["parallel_tool_calls"] = encoded
		}
	}
	if raw := object["thinking"]; nonNull(raw) {
		thinking, err := decodeThinking(raw)
		if err != nil {
			return nil, fmt.Errorf("%w: thinking: %v", ErrInvalidMessagesRequest, err)
		}
		req.Reasoning = thinking
	}
	if raw := object["output_config"]; nonNull(raw) {
		format, effort, err := decodeOutputConfig(raw)
		if err != nil {
			return nil, fmt.Errorf("%w: output_config: %v", ErrInvalidMessagesRequest, err)
		}
		req.ResponseFormat = format
		if effort != nil {
			if req.Reasoning == nil {
				req.Reasoning = &protocol.ReasoningConfig{}
			}
			req.Reasoning.Effort = effort
		}
	}
	if raw := object["metadata"]; nonNull(raw) {
		var metadata map[string]json.RawMessage
		if json.Unmarshal(raw, &metadata) == nil {
			if userID := jsonString(metadata["user_id"]); userID != "" {
				req.Metadata = map[string]string{"user_id": userID}
			}
		}
	}
	return req, nil
}

func EncodeMessagesResponse(response *protocol.CanonicalResponse, publicModel string) ([]byte, error) {
	if response == nil {
		return nil, errors.New("encode Anthropic Messages response: response is nil")
	}
	responseID := normalizeMessageID(response.ResponseID)
	if response.ResponseID == "" {
		responseID = normalizeMessageID(response.RequestID)
	}
	content := make([]any, 0)
	for _, message := range response.Messages {
		if message.Role != protocol.RoleAssistant {
			continue
		}
		for _, part := range message.Content {
			block, err := encodeContentBlock(part)
			if err != nil {
				return nil, err
			}
			if block != nil {
				content = append(content, block)
			}
		}
	}
	usage := map[string]any{"input_tokens": int64(0), "output_tokens": int64(0)}
	if response.Usage != nil {
		usage["input_tokens"] = response.Usage.InputTokens
		usage["output_tokens"] = response.Usage.OutputTokens
		if response.Usage.CachedInputTokens != 0 {
			usage["cache_read_input_tokens"] = response.Usage.CachedInputTokens
		}
	}
	return json.Marshal(map[string]any{
		"id":            responseID,
		"type":          "message",
		"role":          "assistant",
		"model":         publicModel,
		"content":       content,
		"stop_reason":   anthropicStopReason(response.FinishReason),
		"stop_sequence": nil,
		"usage":         usage,
	})
}

func decodeMessage(raw json.RawMessage) ([]protocol.Message, error) {
	object, err := decodeObject(raw)
	if err != nil {
		return nil, err
	}
	role := protocol.Role(jsonString(object["role"]))
	if role != protocol.RoleUser && role != protocol.RoleAssistant {
		return nil, fmt.Errorf("role %q is not supported", role)
	}
	contentRaw := object["content"]
	var text string
	if json.Unmarshal(contentRaw, &text) == nil {
		return []protocol.Message{{Role: role, Content: []protocol.ContentPart{{Type: "text", Text: &text}}}}, nil
	}
	var blocks []json.RawMessage
	if err := json.Unmarshal(contentRaw, &blocks); err != nil {
		return nil, errors.New("content must be a string or array")
	}

	if role == protocol.RoleAssistant {
		parts := make([]protocol.ContentPart, 0, len(blocks))
		for _, block := range blocks {
			part, toolResult, err := decodeBlock(block)
			if err != nil {
				return nil, err
			}
			if toolResult != nil {
				return nil, errors.New("tool_result is not valid in assistant content")
			}
			parts = append(parts, part)
		}
		return []protocol.Message{{Role: role, Content: parts}}, nil
	}

	messages := make([]protocol.Message, 0, len(blocks))
	current := make([]protocol.ContentPart, 0)
	flushUser := func() {
		if len(current) == 0 {
			return
		}
		copyParts := append([]protocol.ContentPart(nil), current...)
		messages = append(messages, protocol.Message{Role: protocol.RoleUser, Content: copyParts})
		current = current[:0]
	}
	for _, block := range blocks {
		part, toolResult, err := decodeBlock(block)
		if err != nil {
			return nil, err
		}
		if toolResult == nil {
			current = append(current, part)
			continue
		}
		flushUser()
		callID := toolResult.ToolCallID
		messages = append(messages, protocol.Message{
			Role:       protocol.RoleTool,
			ToolCallID: &callID,
			Content:    toolResult.Content,
		})
	}
	flushUser()
	if len(messages) == 0 {
		messages = append(messages, protocol.Message{Role: protocol.RoleUser})
	}
	return messages, nil
}

func decodeSystem(raw json.RawMessage) ([]protocol.ContentPart, error) {
	var text string
	if json.Unmarshal(raw, &text) == nil {
		return []protocol.ContentPart{{Type: "text", Text: &text}}, nil
	}
	var blocks []json.RawMessage
	if err := json.Unmarshal(raw, &blocks); err != nil {
		return nil, errors.New("must be a string or array")
	}
	parts := make([]protocol.ContentPart, 0, len(blocks))
	for _, block := range blocks {
		part, result, err := decodeBlock(block)
		if err != nil {
			return nil, err
		}
		if result != nil || part.ToolCall != nil || part.ImageURL != nil || part.InlineData != nil {
			return nil, errors.New("system content contains a non-system block")
		}
		parts = append(parts, part)
	}
	return parts, nil
}

func decodeBlock(raw json.RawMessage) (protocol.ContentPart, *protocol.ToolResult, error) {
	object, err := decodeObject(raw)
	if err != nil {
		return protocol.ContentPart{}, nil, err
	}
	kind := jsonString(object["type"])
	part := protocol.ContentPart{Type: kind, Raw: append(json.RawMessage(nil), raw...)}
	switch kind {
	case "text":
		text := jsonString(object["text"])
		part.Text = &text
	case "image":
		source, err := decodeObject(object["source"])
		if err != nil {
			return protocol.ContentPart{}, nil, errors.New("image source must be an object")
		}
		switch jsonString(source["type"]) {
		case "base64":
			data, err := base64.StdEncoding.DecodeString(jsonString(source["data"]))
			if err != nil {
				return protocol.ContentPart{}, nil, fmt.Errorf("decode base64 image: %w", err)
			}
			part.InlineData = &protocol.InlineData{MIMEType: jsonString(source["media_type"]), Data: data}
		case "url":
			part.ImageURL = &protocol.ImageURL{URL: jsonString(source["url"])}
		default:
			// Preserve future source types in Raw.
		}
	case "tool_use":
		name := jsonString(object["name"])
		id := jsonString(object["id"])
		input := append(json.RawMessage(nil), object["input"]...)
		if !nonNull(input) {
			input = json.RawMessage("{}")
		}
		part.ToolCall = &protocol.ToolCall{ID: id, Type: "function", Name: name, Arguments: input}
	case "tool_result":
		callID := jsonString(object["tool_use_id"])
		if callID == "" {
			return protocol.ContentPart{}, nil, errors.New("tool_result requires tool_use_id")
		}
		content, err := decodeToolResultContent(object["content"])
		if err != nil {
			return protocol.ContentPart{}, nil, err
		}
		result := &protocol.ToolResult{ToolCallID: callID, Content: content}
		decodeOptional(object, "is_error", &result.IsError)
		return protocol.ContentPart{}, result, nil
	case "thinking":
		text := jsonString(object["thinking"])
		signature := append(json.RawMessage(nil), object["signature"]...)
		part.Reasoning = &protocol.ReasoningBlock{Text: text, Signature: signature}
	case "redacted_thinking":
		// Raw is the lossless representation.
	default:
		// Unknown Anthropic blocks remain in Raw for traceability.
	}
	return part, nil, nil
}

func decodeToolResultContent(raw json.RawMessage) ([]protocol.ContentPart, error) {
	if !nonNull(raw) {
		return nil, nil
	}
	var text string
	if json.Unmarshal(raw, &text) == nil {
		return []protocol.ContentPart{{Type: "text", Text: &text}}, nil
	}
	var blocks []json.RawMessage
	if err := json.Unmarshal(raw, &blocks); err != nil {
		return nil, errors.New("tool_result content must be a string or array")
	}
	parts := make([]protocol.ContentPart, 0, len(blocks))
	for _, block := range blocks {
		part, nested, err := decodeBlock(block)
		if err != nil {
			return nil, err
		}
		if nested != nil {
			return nil, errors.New("nested tool_result is not supported")
		}
		parts = append(parts, part)
	}
	return parts, nil
}

func decodeTools(raw json.RawMessage) ([]protocol.ToolDefinition, error) {
	var items []json.RawMessage
	if err := json.Unmarshal(raw, &items); err != nil {
		return nil, errors.New("must be an array")
	}
	tools := make([]protocol.ToolDefinition, 0, len(items))
	for index, item := range items {
		object, err := decodeObject(item)
		if err != nil {
			return nil, fmt.Errorf("tool %d: %w", index, err)
		}
		name := jsonString(object["name"])
		if name == "" {
			return nil, fmt.Errorf("tool %d name is required", index)
		}
		schema := append(json.RawMessage(nil), object["input_schema"]...)
		if !nonNull(schema) {
			schema = json.RawMessage("{}")
		}
		var strict *bool
		decodeOptional(object, "strict", &strict)
		tools = append(tools, protocol.ToolDefinition{
			Name: name, Description: jsonString(object["description"]), InputSchema: schema, Strict: strict,
		})
	}
	return tools, nil
}

func decodeToolChoice(raw json.RawMessage) (json.RawMessage, *bool, error) {
	object, err := decodeObject(raw)
	if err != nil {
		return nil, nil, errors.New("must be an object")
	}
	kind := jsonString(object["type"])
	var parallel *bool
	if rawDisable := object["disable_parallel_tool_use"]; nonNull(rawDisable) {
		var disabled bool
		if json.Unmarshal(rawDisable, &disabled) == nil {
			enabled := !disabled
			parallel = &enabled
		}
	}
	switch kind {
	case "auto":
		return json.RawMessage(`"auto"`), parallel, nil
	case "none":
		return json.RawMessage(`"none"`), parallel, nil
	case "any":
		return json.RawMessage(`"required"`), parallel, nil
	case "tool":
		name := jsonString(object["name"])
		if name == "" {
			return nil, nil, errors.New("tool choice requires name")
		}
		encoded, _ := json.Marshal(map[string]any{
			"type":     "function",
			"function": map[string]any{"name": name},
		})
		return encoded, parallel, nil
	default:
		return nil, nil, fmt.Errorf("unsupported type %q", kind)
	}
}

func decodeThinking(raw json.RawMessage) (*protocol.ReasoningConfig, error) {
	object, err := decodeObject(raw)
	if err != nil {
		return nil, errors.New("must be an object")
	}
	kind := jsonString(object["type"])
	switch kind {
	case "enabled":
		var budget int
		if err := json.Unmarshal(object["budget_tokens"], &budget); err != nil || budget < 1 {
			return nil, errors.New("enabled thinking requires budget_tokens")
		}
		return &protocol.ReasoningConfig{MaxTokens: &budget, IncludeInOut: true}, nil
	case "adaptive":
		effort := "adaptive"
		return &protocol.ReasoningConfig{Effort: &effort, IncludeInOut: true}, nil
	case "disabled", "":
		return nil, nil
	default:
		return nil, fmt.Errorf("unsupported type %q", kind)
	}
}

func decodeOutputConfig(raw json.RawMessage) (json.RawMessage, *string, error) {
	object, err := decodeObject(raw)
	if err != nil {
		return nil, nil, errors.New("must be an object")
	}
	var effort *string
	if value := jsonString(object["effort"]); value != "" {
		effort = &value
	}
	format := object["format"]
	if !nonNull(format) {
		return nil, effort, nil
	}
	formatObject, err := decodeObject(format)
	if err != nil {
		return nil, nil, errors.New("format must be an object")
	}
	if jsonString(formatObject["type"]) != "json_schema" {
		return nil, nil, fmt.Errorf("unsupported format type %q", jsonString(formatObject["type"]))
	}
	schema := formatObject["schema"]
	if !nonNull(schema) {
		return nil, nil, errors.New("json_schema format requires schema")
	}
	encoded, _ := json.Marshal(map[string]any{"type": "json_schema", "schema": json.RawMessage(schema)})
	return encoded, effort, nil
}

func encodeContentBlock(part protocol.ContentPart) (any, error) {
	switch {
	case part.Text != nil:
		return map[string]any{"type": "text", "text": *part.Text}, nil
	case part.ToolCall != nil:
		input := any(map[string]any{})
		if nonNull(part.ToolCall.Arguments) {
			if err := json.Unmarshal(part.ToolCall.Arguments, &input); err != nil {
				return nil, fmt.Errorf("tool call %q arguments: %w", part.ToolCall.Name, err)
			}
		}
		return map[string]any{
			"type": "tool_use", "id": part.ToolCall.ID, "name": part.ToolCall.Name, "input": input,
		}, nil
	case part.Reasoning != nil:
		block := map[string]any{"type": "thinking", "thinking": part.Reasoning.Text}
		if nonNull(part.Reasoning.Signature) {
			var signature string
			if json.Unmarshal(part.Reasoning.Signature, &signature) == nil && signature != "" {
				block["signature"] = signature
			}
		}
		return block, nil
	default:
		return nil, nil
	}
}

func anthropicStopReason(reason string) any {
	switch reason {
	case "", "stop":
		return "end_turn"
	case "length":
		return "max_tokens"
	case "tool_calls":
		return "tool_use"
	case "content_filter":
		return "refusal"
	default:
		return reason
	}
}

func normalizeMessageID(value string) string {
	value = strings.TrimSpace(value)
	if value == "" {
		return "msg_gateway"
	}
	if strings.HasPrefix(value, "msg_") {
		return value
	}
	for _, prefix := range []string{"chatcmpl_", "resp_", "req_"} {
		value = strings.TrimPrefix(value, prefix)
	}
	return "msg_" + value
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

func decodeOptional(object map[string]json.RawMessage, key string, target any) {
	raw := object[key]
	if nonNull(raw) {
		_ = json.Unmarshal(raw, target)
	}
}

func jsonString(raw json.RawMessage) string {
	var value string
	_ = json.Unmarshal(raw, &value)
	return value
}

func nonNull(raw json.RawMessage) bool {
	return len(raw) > 0 && !bytes.Equal(bytes.TrimSpace(raw), []byte("null"))
}

func ensureExtensions(value map[string]json.RawMessage) map[string]json.RawMessage {
	if value == nil {
		return make(map[string]json.RawMessage)
	}
	return value
}
