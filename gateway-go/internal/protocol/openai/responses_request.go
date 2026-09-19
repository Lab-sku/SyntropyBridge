package openai

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/protocol"
)

var ErrInvalidResponsesRequest = errors.New("invalid OpenAI Responses request")

func DecodeResponsesRequest(body []byte, requestID string) (*protocol.CanonicalRequest, error) {
	object, err := decodeObject(body)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrInvalidResponsesRequest, err)
	}

	var model string
	if err := decodeRequired(object, "model", &model); err != nil || strings.TrimSpace(model) == "" {
		return nil, fmt.Errorf("%w: model is required", ErrInvalidResponsesRequest)
	}
	rawInput, ok := object["input"]
	if !ok {
		return nil, fmt.Errorf("%w: input is required", ErrInvalidResponsesRequest)
	}
	messages, err := decodeResponsesInput(rawInput)
	if err != nil {
		return nil, fmt.Errorf("%w: input: %v", ErrInvalidResponsesRequest, err)
	}

	if raw := object["previous_response_id"]; nonNullJSON(raw) {
		return nil, fmt.Errorf("%w: previous_response_id requires a native Responses state backend", ErrInvalidResponsesRequest)
	}
	if raw := object["conversation"]; nonNullJSON(raw) {
		return nil, fmt.Errorf("%w: conversation requires a native Responses state backend", ErrInvalidResponsesRequest)
	}
	if raw := object["background"]; nonNullJSON(raw) {
		var enabled bool
		_ = json.Unmarshal(raw, &enabled)
		if enabled {
			return nil, fmt.Errorf("%w: background responses require a native Responses backend", ErrInvalidResponsesRequest)
		}
	}

	req := &protocol.CanonicalRequest{
		RequestID:  requestID,
		Protocol:   protocol.ProtocolOpenAIResponses,
		Operation:  protocol.OperationChat,
		Model:      model,
		Messages:   messages,
		Parameters: protocol.GenerationParameters{},
		RawBody:    append(json.RawMessage(nil), body...),
	}

	var instructions string
	decodeOptional(object, "instructions", &instructions)
	if strings.TrimSpace(instructions) != "" {
		text := instructions
		req.Messages = append([]protocol.Message{{
			Role: protocol.RoleDeveloper,
			Content: []protocol.ContentPart{{Type: "text", Text: &text}},
		}}, req.Messages...)
	}

	decodeOptional(object, "stream", &req.Stream)
	decodeOptional(object, "max_output_tokens", &req.Parameters.MaxOutputTokens)
	decodeOptional(object, "temperature", &req.Parameters.Temperature)
	decodeOptional(object, "top_p", &req.Parameters.TopP)

	if raw := object["metadata"]; nonNullJSON(raw) {
		_ = json.Unmarshal(raw, &req.Metadata)
	}
	if raw := object["reasoning"]; nonNullJSON(raw) {
		var reasoning map[string]json.RawMessage
		if json.Unmarshal(raw, &reasoning) == nil {
			var effort string
			_ = json.Unmarshal(reasoning["effort"], &effort)
			if effort != "" {
				req.Reasoning = &protocol.ReasoningConfig{Effort: &effort}
			}
		}
	}
	if raw := object["text"]; nonNullJSON(raw) {
		format, err := responsesTextFormatToChat(raw)
		if err != nil {
			return nil, fmt.Errorf("%w: text.format: %v", ErrInvalidResponsesRequest, err)
		}
		req.ResponseFormat = format
	}
	if raw := object["tool_choice"]; nonNullJSON(raw) {
		choice, err := responsesToolChoiceToChat(raw)
		if err != nil {
			return nil, fmt.Errorf("%w: tool_choice: %v", ErrInvalidResponsesRequest, err)
		}
		req.ToolChoice = choice
	}
	if raw := object["tools"]; nonNullJSON(raw) {
		tools, err := decodeResponsesTools(raw)
		if err != nil {
			return nil, fmt.Errorf("%w: tools: %v", ErrInvalidResponsesRequest, err)
		}
		req.Tools = tools
	}
	return req, nil
}

func decodeResponsesInput(raw json.RawMessage) ([]protocol.Message, error) {
	if bytes.Equal(bytes.TrimSpace(raw), []byte("null")) {
		return nil, errors.New("must not be null")
	}
	var text string
	if json.Unmarshal(raw, &text) == nil {
		return []protocol.Message{responseTextMessage(protocol.RoleUser, text)}, nil
	}

	var items []json.RawMessage
	if err := json.Unmarshal(raw, &items); err != nil {
		return nil, errors.New("must be a string or an array of input items")
	}
	messages := make([]protocol.Message, 0, len(items))
	for index, item := range items {
		object, err := decodeObject(item)
		if err != nil {
			return nil, fmt.Errorf("item %d: %w", index, err)
		}
		kind := rawJSONString(object["type"])
		switch kind {
		case "", "message":
			message, err := DecodeMessage(item)
			if err != nil {
				return nil, fmt.Errorf("item %d message: %w", index, err)
			}
			if message.Extensions != nil {
				delete(message.Extensions, "type")
				delete(message.Extensions, "id")
				delete(message.Extensions, "status")
				if len(message.Extensions) == 0 {
					message.Extensions = nil
				}
			}
			message.Raw = nil
			messages = append(messages, message)
		case "function_call":
			name := rawJSONString(object["name"])
			callID := rawJSONString(object["call_id"])
			if callID == "" {
				callID = rawJSONString(object["id"])
			}
			if name == "" || callID == "" {
				return nil, fmt.Errorf("item %d: function_call requires name and call_id", index)
			}
			arguments := rawJSONString(object["arguments"])
			messages = append(messages, protocol.Message{
				Role: protocol.RoleAssistant,
				Content: []protocol.ContentPart{{
					Type: "tool_call",
					ToolCall: &protocol.ToolCall{
						ID:        callID,
						Type:      "function",
						Name:      name,
						Arguments: normalizeArguments(arguments),
					},
				}},
			})
		case "function_call_output":
			callID := rawJSONString(object["call_id"])
			if callID == "" {
				return nil, fmt.Errorf("item %d: function_call_output requires call_id", index)
			}
			text, err := responsesOutputText(object["output"])
			if err != nil {
				return nil, fmt.Errorf("item %d function_call_output: %w", index, err)
			}
			messages = append(messages, responseToolMessage(callID, text))
		default:
			return nil, fmt.Errorf("item %d has unsupported type %q", index, kind)
		}
	}
	if len(messages) == 0 {
		return nil, errors.New("must contain at least one input item")
	}
	return messages, nil
}

func decodeResponsesTools(raw json.RawMessage) ([]protocol.ToolDefinition, error) {
	var items []json.RawMessage
	if err := json.Unmarshal(raw, &items); err != nil {
		return nil, errors.New("must be an array")
	}
	tools := make([]protocol.ToolDefinition, 0, len(items))
	for index, item := range items {
		object, err := decodeObject(item)
		if err != nil {
			return nil, fmt.Errorf("item %d: %w", index, err)
		}
		kind := rawJSONString(object["type"])
		if kind != "function" {
			return nil, fmt.Errorf("item %d type %q requires a native Responses adapter", index, kind)
		}
		name := rawJSONString(object["name"])
		if name == "" {
			return nil, fmt.Errorf("item %d function name is required", index)
		}
		var strict *bool
		decodeOptional(object, "strict", &strict)
		tools = append(tools, protocol.ToolDefinition{
			Name:        name,
			Description: rawJSONString(object["description"]),
			InputSchema: append(json.RawMessage(nil), object["parameters"]...),
			Strict:      strict,
		})
	}
	return tools, nil
}

func responsesToolChoiceToChat(raw json.RawMessage) (json.RawMessage, error) {
	var value string
	if json.Unmarshal(raw, &value) == nil {
		switch value {
		case "auto", "none", "required":
			return append(json.RawMessage(nil), raw...), nil
		default:
			return nil, fmt.Errorf("unsupported string value %q", value)
		}
	}
	object, err := decodeObject(raw)
	if err != nil {
		return nil, errors.New("must be a string or object")
	}
	kind := rawJSONString(object["type"])
	if kind != "function" {
		return nil, fmt.Errorf("type %q requires a native Responses adapter", kind)
	}
	name := rawJSONString(object["name"])
	if name == "" {
		return nil, errors.New("function name is required")
	}
	return json.Marshal(map[string]any{
		"type":     "function",
		"function": map[string]any{"name": name},
	})
}

func responsesTextFormatToChat(raw json.RawMessage) (json.RawMessage, error) {
	textObject, err := decodeObject(raw)
	if err != nil {
		return nil, err
	}
	formatRaw := textObject["format"]
	if !nonNullJSON(formatRaw) {
		return nil, nil
	}
	format, err := decodeObject(formatRaw)
	if err != nil {
		return nil, errors.New("must be an object")
	}
	kind := rawJSONString(format["type"])
	switch kind {
	case "", "text":
		return nil, nil
	case "json_object":
		return json.Marshal(map[string]any{"type": "json_object"})
	case "json_schema":
		name := rawJSONString(format["name"])
		schema := format["schema"]
		if name == "" || !nonNullJSON(schema) {
			return nil, errors.New("json_schema requires name and schema")
		}
		config := map[string]any{
			"name":   name,
			"schema": json.RawMessage(schema),
		}
		if description := rawJSONString(format["description"]); description != "" {
			config["description"] = description
		}
		var strict bool
		if raw := format["strict"]; nonNullJSON(raw) && json.Unmarshal(raw, &strict) == nil {
			config["strict"] = strict
		}
		return json.Marshal(map[string]any{
			"type":        "json_schema",
			"json_schema": config,
		})
	default:
		return nil, fmt.Errorf("format type %q requires a native Responses adapter", kind)
	}
}

func responsesOutputText(raw json.RawMessage) (string, error) {
	if !nonNullJSON(raw) {
		return "", nil
	}
	var text string
	if json.Unmarshal(raw, &text) == nil {
		return text, nil
	}
	var parts []json.RawMessage
	if json.Unmarshal(raw, &parts) != nil {
		return "", errors.New("output must be a string or text content array")
	}
	var builder strings.Builder
	for _, part := range parts {
		object, err := decodeObject(part)
		if err != nil {
			continue
		}
		builder.WriteString(rawJSONString(object["text"]))
	}
	return builder.String(), nil
}

func responseTextMessage(role protocol.Role, text string) protocol.Message {
	value := text
	return protocol.Message{
		Role:    role,
		Content: []protocol.ContentPart{{Type: "text", Text: &value}},
	}
}

func responseToolMessage(callID, text string) protocol.Message {
	value := text
	return protocol.Message{
		Role:       protocol.RoleTool,
		ToolCallID: &callID,
		Content:    []protocol.ContentPart{{Type: "text", Text: &value}},
	}
}

func rawJSONString(raw json.RawMessage) string {
	var value string
	_ = json.Unmarshal(raw, &value)
	return value
}

func nonNullJSON(raw json.RawMessage) bool {
	return len(raw) > 0 && !bytes.Equal(bytes.TrimSpace(raw), []byte("null"))
}
