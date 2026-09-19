package anthropic

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"

	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/protocol"
)

func replaceModelAndStream(body json.RawMessage, model string, stream bool) ([]byte, error) {
	var object map[string]json.RawMessage
	if err := json.Unmarshal(body, &object); err != nil {
		return nil, fmt.Errorf("decode Anthropic request: %w", err)
	}
	if object == nil {
		return nil, errors.New("Anthropic request must be a JSON object")
	}
	encodedModel, _ := json.Marshal(model)
	encodedStream, _ := json.Marshal(stream)
	object["model"] = encodedModel
	object["stream"] = encodedStream
	return json.Marshal(object)
}

func encodeMessagesRequest(req *protocol.CanonicalRequest, model string, metadata map[string]string) ([]byte, error) {
	object := map[string]any{
		"model":  model,
		"stream": req.Stream,
	}

	maxTokens, err := maxOutputTokens(req, metadata)
	if err != nil {
		return nil, err
	}
	object["max_tokens"] = maxTokens

	messages := make([]any, 0, len(req.Messages))
	system := make([]any, 0)
	for index, message := range req.Messages {
		switch message.Role {
		case protocol.RoleSystem, protocol.RoleDeveloper:
			blocks, err := encodeSystemContent(message.Content)
			if err != nil {
				return nil, fmt.Errorf("message %d system content: %w", index, err)
			}
			system = append(system, blocks...)
		case protocol.RoleUser, protocol.RoleAssistant, protocol.RoleTool:
			encoded, err := encodeMessage(message)
			if err != nil {
				return nil, fmt.Errorf("message %d: %w", index, err)
			}
			messages = append(messages, encoded)
		default:
			return nil, fmt.Errorf("message %d: role %q cannot be represented by Anthropic Messages", index, message.Role)
		}
	}
	if len(messages) == 0 {
		return nil, errors.New("Anthropic Messages requires at least one user or assistant message")
	}
	object["messages"] = messages
	if len(system) == 1 {
		if block, ok := system[0].(map[string]any); ok && block["type"] == "text" {
			if text, ok := block["text"].(string); ok {
				object["system"] = text
			} else {
				object["system"] = system
			}
		} else {
			object["system"] = system
		}
	} else if len(system) > 0 {
		object["system"] = system
	}

	if req.Parameters.Temperature != nil {
		object["temperature"] = *req.Parameters.Temperature
	}
	if req.Parameters.TopP != nil {
		object["top_p"] = *req.Parameters.TopP
	}
	if req.Parameters.TopK != nil {
		object["top_k"] = *req.Parameters.TopK
	}
	if len(req.Parameters.Stop) > 0 {
		object["stop_sequences"] = req.Parameters.Stop
	}

	if len(req.Tools) > 0 {
		tools := make([]any, 0, len(req.Tools))
		for _, tool := range req.Tools {
			if strings.TrimSpace(tool.Name) == "" {
				return nil, errors.New("tool name is empty")
			}
			inputSchema := any(map[string]any{"type": "object", "properties": map[string]any{}})
			if len(bytes.TrimSpace(tool.InputSchema)) > 0 {
				if err := json.Unmarshal(tool.InputSchema, &inputSchema); err != nil {
					return nil, fmt.Errorf("tool %q input schema: %w", tool.Name, err)
				}
			}
			encoded := map[string]any{
				"name":         tool.Name,
				"input_schema": inputSchema,
			}
			if tool.Description != "" {
				encoded["description"] = tool.Description
			}
			if tool.Strict != nil {
				encoded["strict"] = *tool.Strict
			}
			tools = append(tools, encoded)
		}
		object["tools"] = tools
	}
	if len(req.ToolChoice) > 0 {
		choice, err := encodeToolChoice(req.ToolChoice, req.Extensions)
		if err != nil {
			return nil, err
		}
		object["tool_choice"] = choice
	} else if raw, ok := req.Extensions["parallel_tool_calls"]; ok && len(req.Tools) > 0 {
		var enabled bool
		if err := json.Unmarshal(raw, &enabled); err == nil {
			object["tool_choice"] = map[string]any{"type": "auto", "disable_parallel_tool_use": !enabled}
		}
	}

	outputConfig := map[string]any{}
	if req.Reasoning != nil {
		if req.Reasoning.MaxTokens != nil {
			if *req.Reasoning.MaxTokens < 1024 {
				return nil, errors.New("Anthropic thinking budget must be at least 1024 tokens")
			}
			object["thinking"] = map[string]any{"type": "enabled", "budget_tokens": *req.Reasoning.MaxTokens}
		}
		if req.Reasoning.Effort != nil && strings.TrimSpace(*req.Reasoning.Effort) != "" {
			outputConfig["effort"] = *req.Reasoning.Effort
		}
	}
	if len(req.ResponseFormat) > 0 {
		format, err := encodeResponseFormat(req.ResponseFormat)
		if err != nil {
			return nil, err
		}
		outputConfig["format"] = format
	}
	if len(outputConfig) > 0 {
		object["output_config"] = outputConfig
	}

	if userID := strings.TrimSpace(req.Metadata["user_id"]); userID != "" {
		object["metadata"] = map[string]any{"user_id": userID}
	}
	for _, key := range []string{"service_tier", "inference_geo", "container", "context_management"} {
		if raw, ok := req.Extensions[key]; ok && len(raw) > 0 {
			object[key] = json.RawMessage(raw)
		}
	}
	return json.Marshal(object)
}

func maxOutputTokens(req *protocol.CanonicalRequest, metadata map[string]string) (int, error) {
	if req.Parameters.MaxOutputTokens != nil {
		if *req.Parameters.MaxOutputTokens < 0 {
			return 0, errors.New("max output tokens must not be negative")
		}
		return *req.Parameters.MaxOutputTokens, nil
	}
	if raw := strings.TrimSpace(metadata["default_max_tokens"]); raw != "" {
		value, err := strconv.Atoi(raw)
		if err != nil || value < 0 {
			return 0, fmt.Errorf("invalid deployment default_max_tokens %q", raw)
		}
		return value, nil
	}
	return defaultMaxOutput, nil
}

func encodeSystemContent(parts []protocol.ContentPart) ([]any, error) {
	blocks := make([]any, 0, len(parts))
	for _, part := range parts {
		switch {
		case part.Text != nil:
			blocks = append(blocks, map[string]any{"type": "text", "text": *part.Text})
		case len(part.Raw) > 0:
			blocks = append(blocks, json.RawMessage(part.Raw))
		default:
			return nil, fmt.Errorf("content part %q is not valid in an Anthropic system prompt", part.Type)
		}
	}
	return blocks, nil
}

func encodeMessage(message protocol.Message) (map[string]any, error) {
	role := message.Role
	if role == protocol.RoleTool {
		role = protocol.RoleUser
	}
	if role != protocol.RoleUser && role != protocol.RoleAssistant {
		return nil, fmt.Errorf("unsupported role %q", message.Role)
	}

	blocks := make([]any, 0, len(message.Content)+1)
	if message.Role == protocol.RoleTool {
		if message.ToolCallID == nil || *message.ToolCallID == "" {
			return nil, errors.New("tool message is missing tool_call_id")
		}
		content, err := encodeToolResultContent(message.Content)
		if err != nil {
			return nil, err
		}
		blocks = append(blocks, map[string]any{
			"type":        "tool_result",
			"tool_use_id": *message.ToolCallID,
			"content":     content,
		})
	} else {
		for _, part := range message.Content {
			encoded, err := encodeContentPart(part, role)
			if err != nil {
				return nil, err
			}
			if encoded != nil {
				blocks = append(blocks, encoded)
			}
		}
	}
	return map[string]any{"role": string(role), "content": blocks}, nil
}

func encodeContentPart(part protocol.ContentPart, role protocol.Role) (any, error) {
	switch {
	case part.ToolCall != nil:
		if role != protocol.RoleAssistant {
			return nil, errors.New("tool_use blocks are only valid in assistant messages")
		}
		input := any(map[string]any{})
		if len(bytes.TrimSpace(part.ToolCall.Arguments)) > 0 {
			if err := json.Unmarshal(part.ToolCall.Arguments, &input); err != nil {
				return nil, fmt.Errorf("tool call %q arguments: %w", part.ToolCall.Name, err)
			}
		}
		return map[string]any{
			"type":  "tool_use",
			"id":    part.ToolCall.ID,
			"name":  part.ToolCall.Name,
			"input": input,
		}, nil
	case part.ToolResult != nil:
		if role != protocol.RoleUser {
			return nil, errors.New("tool_result blocks are only valid in user messages")
		}
		content, err := encodeToolResultContent(part.ToolResult.Content)
		if err != nil {
			return nil, err
		}
		result := map[string]any{
			"type":        "tool_result",
			"tool_use_id": part.ToolResult.ToolCallID,
			"content":     content,
		}
		if part.ToolResult.IsError {
			result["is_error"] = true
		}
		return result, nil
	case part.Text != nil:
		return map[string]any{"type": "text", "text": *part.Text}, nil
	case part.ImageURL != nil:
		if role != protocol.RoleUser {
			return nil, errors.New("image blocks are only valid in user messages")
		}
		if strings.TrimSpace(part.ImageURL.URL) == "" {
			return nil, errors.New("image URL is empty")
		}
		return map[string]any{
			"type": "image",
			"source": map[string]any{
				"type": "url",
				"url":  part.ImageURL.URL,
			},
		}, nil
	case part.InlineData != nil:
		if role != protocol.RoleUser {
			return nil, errors.New("inline images are only valid in user messages")
		}
		return map[string]any{
			"type": "image",
			"source": map[string]any{
				"type":       "base64",
				"media_type": part.InlineData.MIMEType,
				"data":       base64.StdEncoding.EncodeToString(part.InlineData.Data),
			},
		}, nil
	case part.Reasoning != nil:
		if role != protocol.RoleAssistant {
			return nil, errors.New("thinking blocks are only valid in assistant messages")
		}
		block := map[string]any{"type": "thinking", "thinking": part.Reasoning.Text}
		if len(part.Reasoning.Signature) > 0 {
			var signature string
			if err := json.Unmarshal(part.Reasoning.Signature, &signature); err != nil {
				return nil, fmt.Errorf("decode thinking signature: %w", err)
			}
			block["signature"] = signature
		}
		return block, nil
	case len(part.Raw) > 0:
		if !json.Valid(part.Raw) {
			return nil, errors.New("raw content block is not valid JSON")
		}
		return json.RawMessage(part.Raw), nil
	default:
		return nil, fmt.Errorf("content part %q cannot be represented by Anthropic Messages", part.Type)
	}
}

func encodeToolResultContent(parts []protocol.ContentPart) (any, error) {
	if len(parts) == 1 && parts[0].Text != nil {
		return *parts[0].Text, nil
	}
	blocks := make([]any, 0, len(parts))
	for _, part := range parts {
		encoded, err := encodeContentPart(part, protocol.RoleUser)
		if err != nil {
			return nil, err
		}
		blocks = append(blocks, encoded)
	}
	return blocks, nil
}

func encodeToolChoice(raw json.RawMessage, extensions map[string]json.RawMessage) (any, error) {
	var stringChoice string
	if json.Unmarshal(raw, &stringChoice) == nil {
		choice := map[string]any{}
		switch stringChoice {
		case "auto":
			choice["type"] = "auto"
		case "none":
			choice["type"] = "none"
		case "required", "any":
			choice["type"] = "any"
		default:
			return nil, fmt.Errorf("unsupported tool_choice %q for Anthropic", stringChoice)
		}
		applyParallelChoice(choice, extensions)
		return choice, nil
	}

	var object map[string]json.RawMessage
	if err := json.Unmarshal(raw, &object); err != nil || object == nil {
		return nil, errors.New("tool_choice must be a string or object")
	}
	var choiceType string
	_ = json.Unmarshal(object["type"], &choiceType)
	choice := map[string]any{}
	switch choiceType {
	case "auto", "any", "none":
		choice["type"] = choiceType
	case "tool":
		var name string
		_ = json.Unmarshal(object["name"], &name)
		if name == "" {
			return nil, errors.New("Anthropic tool_choice type tool requires name")
		}
		choice["type"] = "tool"
		choice["name"] = name
	case "function":
		var function struct {
			Name string `json:"name"`
		}
		if err := json.Unmarshal(object["function"], &function); err != nil || function.Name == "" {
			return nil, errors.New("OpenAI function tool_choice requires function.name")
		}
		choice["type"] = "tool"
		choice["name"] = function.Name
	default:
		return nil, fmt.Errorf("unsupported tool_choice type %q for Anthropic", choiceType)
	}
	if rawDisable := object["disable_parallel_tool_use"]; len(rawDisable) > 0 {
		var disabled bool
		if json.Unmarshal(rawDisable, &disabled) == nil {
			choice["disable_parallel_tool_use"] = disabled
		}
	}
	applyParallelChoice(choice, extensions)
	return choice, nil
}

func applyParallelChoice(choice map[string]any, extensions map[string]json.RawMessage) {
	if raw, ok := extensions["parallel_tool_calls"]; ok {
		var enabled bool
		if json.Unmarshal(raw, &enabled) == nil {
			choice["disable_parallel_tool_use"] = !enabled
		}
	}
}

func encodeResponseFormat(raw json.RawMessage) (any, error) {
	var object map[string]json.RawMessage
	if err := json.Unmarshal(raw, &object); err != nil || object == nil {
		return nil, errors.New("response_format must be an object")
	}
	var kind string
	_ = json.Unmarshal(object["type"], &kind)
	if kind != "json_schema" {
		return nil, fmt.Errorf("Anthropic structured output requires response_format type json_schema, got %q", kind)
	}
	if schema := object["schema"]; len(schema) > 0 {
		if !json.Valid(schema) {
			return nil, errors.New("response_format schema is invalid JSON")
		}
		return map[string]any{"type": "json_schema", "schema": json.RawMessage(schema)}, nil
	}
	var openAI struct {
		Schema json.RawMessage `json:"schema"`
	}
	if err := json.Unmarshal(object["json_schema"], &openAI); err != nil || len(openAI.Schema) == 0 {
		return nil, errors.New("response_format json_schema.schema is required")
	}
	if !json.Valid(openAI.Schema) {
		return nil, errors.New("response_format json_schema.schema is invalid JSON")
	}
	return map[string]any{"type": "json_schema", "schema": json.RawMessage(openAI.Schema)}, nil
}
