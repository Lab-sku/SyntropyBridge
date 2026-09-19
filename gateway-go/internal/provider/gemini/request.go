package gemini

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/protocol"
)

func encodeGenerateContentRequest(req *protocol.CanonicalRequest) ([]byte, error) {
	object := map[string]any{}
	contents := make([]any, 0, len(req.Messages))
	systemParts := make([]any, 0)
	toolNames := map[string]string{}

	for _, message := range req.Messages {
		if message.Role == protocol.RoleAssistant {
			for _, part := range message.Content {
				if part.ToolCall != nil && part.ToolCall.ID != "" {
					toolNames[part.ToolCall.ID] = part.ToolCall.Name
				}
			}
		}
	}

	for index, message := range req.Messages {
		switch message.Role {
		case protocol.RoleSystem, protocol.RoleDeveloper:
			parts, err := encodeParts(message.Content, protocol.RoleUser, toolNames, message.ToolCallID)
			if err != nil {
				return nil, fmt.Errorf("message %d system content: %w", index, err)
			}
			systemParts = append(systemParts, parts...)
		case protocol.RoleUser, protocol.RoleAssistant, protocol.RoleTool:
			role := "user"
			if message.Role == protocol.RoleAssistant {
				role = "model"
			}
			parts, err := encodeParts(message.Content, message.Role, toolNames, message.ToolCallID)
			if err != nil {
				return nil, fmt.Errorf("message %d: %w", index, err)
			}
			contents = append(contents, map[string]any{"role": role, "parts": parts})
		default:
			return nil, fmt.Errorf("message %d role %q cannot be represented by Gemini", index, message.Role)
		}
	}
	if len(contents) == 0 {
		return nil, errors.New("Gemini GenerateContent requires at least one non-system message")
	}
	object["contents"] = contents
	if len(systemParts) > 0 {
		object["systemInstruction"] = map[string]any{"parts": systemParts}
	}

	generationConfig := map[string]any{}
	if req.Parameters.MaxOutputTokens != nil {
		generationConfig["maxOutputTokens"] = *req.Parameters.MaxOutputTokens
	}
	if req.Parameters.Temperature != nil {
		generationConfig["temperature"] = *req.Parameters.Temperature
	}
	if req.Parameters.TopP != nil {
		generationConfig["topP"] = *req.Parameters.TopP
	}
	if req.Parameters.TopK != nil {
		generationConfig["topK"] = *req.Parameters.TopK
	}
	if len(req.Parameters.Stop) > 0 {
		generationConfig["stopSequences"] = req.Parameters.Stop
	}
	if len(req.ResponseFormat) > 0 {
		if err := applyResponseFormat(generationConfig, req.ResponseFormat); err != nil {
			return nil, err
		}
	}
	if req.Reasoning != nil {
		thinking := map[string]any{"includeThoughts": req.Reasoning.IncludeInOut}
		if req.Reasoning.MaxTokens != nil {
			thinking["thinkingBudget"] = *req.Reasoning.MaxTokens
		}
		if req.Reasoning.Effort != nil && strings.TrimSpace(*req.Reasoning.Effort) != "" && !strings.EqualFold(*req.Reasoning.Effort, "adaptive") {
			thinking["thinkingLevel"] = strings.ToUpper(strings.TrimSpace(*req.Reasoning.Effort))
		}
		generationConfig["thinkingConfig"] = thinking
	}
	if len(generationConfig) > 0 {
		object["generationConfig"] = generationConfig
	}

	if len(req.Tools) > 0 {
		declarations := make([]any, 0, len(req.Tools))
		for _, tool := range req.Tools {
			if strings.TrimSpace(tool.Name) == "" {
				return nil, errors.New("tool name is empty")
			}
			parameters := any(map[string]any{"type": "object"})
			if len(bytes.TrimSpace(tool.InputSchema)) > 0 {
				if err := json.Unmarshal(tool.InputSchema, &parameters); err != nil {
					return nil, fmt.Errorf("tool %q parameters: %w", tool.Name, err)
				}
			}
			declaration := map[string]any{
				"name":       tool.Name,
				"parameters": parameters,
			}
			if tool.Description != "" {
				declaration["description"] = tool.Description
			}
			declarations = append(declarations, declaration)
		}
		object["tools"] = []any{map[string]any{"functionDeclarations": declarations}}
	}
	if len(req.ToolChoice) > 0 {
		config, err := encodeToolConfig(req.ToolChoice)
		if err != nil {
			return nil, err
		}
		object["toolConfig"] = config
	}
	return json.Marshal(object)
}

func encodeParts(parts []protocol.ContentPart, role protocol.Role, toolNames map[string]string, toolCallID *string) ([]any, error) {
	encoded := make([]any, 0, len(parts))
	if role == protocol.RoleTool {
		if toolCallID == nil || *toolCallID == "" {
			return nil, errors.New("tool message is missing tool_call_id")
		}
		name := toolNames[*toolCallID]
		if name == "" {
			name = "tool"
		}
		response, err := toolResultObject(parts)
		if err != nil {
			return nil, err
		}
		return []any{map[string]any{
			"functionResponse": map[string]any{"id": *toolCallID, "name": name, "response": response},
		}}, nil
	}
	for _, part := range parts {
		switch {
		case part.Text != nil:
			encoded = append(encoded, map[string]any{"text": *part.Text})
		case part.InlineData != nil:
			encoded = append(encoded, map[string]any{"inlineData": map[string]any{
				"mimeType": part.InlineData.MIMEType,
				"data":     base64.StdEncoding.EncodeToString(part.InlineData.Data),
			}})
		case part.ImageURL != nil:
			inline, ok, err := dataURL(part.ImageURL.URL)
			if err != nil {
				return nil, err
			}
			if ok {
				encoded = append(encoded, map[string]any{"inlineData": inline})
			} else {
				encoded = append(encoded, map[string]any{"fileData": map[string]any{
					"fileUri": part.ImageURL.URL,
				}})
			}
		case part.ToolCall != nil:
			input := any(map[string]any{})
			if len(bytes.TrimSpace(part.ToolCall.Arguments)) > 0 {
				if err := json.Unmarshal(part.ToolCall.Arguments, &input); err != nil {
					return nil, fmt.Errorf("tool call %q arguments: %w", part.ToolCall.Name, err)
				}
			}
			encoded = append(encoded, map[string]any{"functionCall": map[string]any{
				"id": part.ToolCall.ID, "name": part.ToolCall.Name, "args": input,
			}})
		case part.ToolResult != nil:
			response, err := toolResultObject(part.ToolResult.Content)
			if err != nil {
				return nil, err
			}
			encoded = append(encoded, map[string]any{"functionResponse": map[string]any{
				"id": part.ToolResult.ToolCallID, "name": toolNames[part.ToolResult.ToolCallID], "response": response,
			}})
		case part.Reasoning != nil:
			block := map[string]any{"text": part.Reasoning.Text, "thought": true}
			if len(part.Reasoning.Signature) > 0 {
				var signature string
				if json.Unmarshal(part.Reasoning.Signature, &signature) == nil && signature != "" {
					block["thoughtSignature"] = signature
				}
			}
			encoded = append(encoded, block)
		default:
			if len(part.Raw) > 0 {
				var raw any
				if json.Unmarshal(part.Raw, &raw) == nil {
					encoded = append(encoded, raw)
				}
			}
		}
	}
	return encoded, nil
}

func toolResultObject(parts []protocol.ContentPart) (map[string]any, error) {
	if len(parts) == 1 && parts[0].Text != nil {
		return map[string]any{"result": *parts[0].Text}, nil
	}
	content := make([]any, 0, len(parts))
	for _, part := range parts {
		if part.Text != nil {
			content = append(content, map[string]any{"text": *part.Text})
			continue
		}
		if part.InlineData != nil {
			content = append(content, map[string]any{"inlineData": map[string]any{
				"mimeType": part.InlineData.MIMEType,
				"data":     base64.StdEncoding.EncodeToString(part.InlineData.Data),
			}})
		}
	}
	return map[string]any{"content": content}, nil
}

func encodeToolConfig(raw json.RawMessage) (map[string]any, error) {
	var value string
	if json.Unmarshal(raw, &value) == nil {
		mode := ""
		switch value {
		case "auto":
			mode = "AUTO"
		case "required":
			mode = "ANY"
		case "none":
			mode = "NONE"
		default:
			return nil, fmt.Errorf("unsupported Gemini tool choice %q", value)
		}
		return map[string]any{"functionCallingConfig": map[string]any{"mode": mode}}, nil
	}
	var object map[string]json.RawMessage
	if err := json.Unmarshal(raw, &object); err != nil {
		return nil, errors.New("tool choice must be a string or object")
	}
	var kind string
	_ = json.Unmarshal(object["type"], &kind)
	if kind != "function" {
		return nil, fmt.Errorf("unsupported Gemini tool choice type %q", kind)
	}
	var function map[string]json.RawMessage
	if json.Unmarshal(object["function"], &function) != nil {
		return nil, errors.New("function tool choice is invalid")
	}
	var name string
	_ = json.Unmarshal(function["name"], &name)
	if name == "" {
		return nil, errors.New("function tool choice requires name")
	}
	return map[string]any{"functionCallingConfig": map[string]any{
		"mode": "ANY", "allowedFunctionNames": []string{name},
	}}, nil
}

func applyResponseFormat(config map[string]any, raw json.RawMessage) error {
	var object map[string]json.RawMessage
	if err := json.Unmarshal(raw, &object); err != nil {
		return errors.New("response_format must be an object")
	}
	var kind string
	_ = json.Unmarshal(object["type"], &kind)
	switch kind {
	case "json_object":
		config["responseMimeType"] = "application/json"
		return nil
	case "json_schema":
		var schema json.RawMessage
		if direct := object["schema"]; len(direct) > 0 {
			schema = direct
		} else {
			var nested map[string]json.RawMessage
			_ = json.Unmarshal(object["json_schema"], &nested)
			schema = nested["schema"]
		}
		if len(schema) == 0 {
			return errors.New("json_schema response format requires schema")
		}
		config["responseMimeType"] = "application/json"
		config["responseJsonSchema"] = json.RawMessage(schema)
		return nil
	default:
		return fmt.Errorf("unsupported response format %q for Gemini", kind)
	}
}

func dataURL(value string) (map[string]any, bool, error) {
	if !strings.HasPrefix(value, "data:") {
		return nil, false, nil
	}
	comma := strings.IndexByte(value, ',')
	if comma < 0 {
		return nil, false, errors.New("invalid image data URL")
	}
	header := value[len("data:"):comma]
	payload := value[comma+1:]
	parts := strings.Split(header, ";")
	mimeType := parts[0]
	if len(parts) < 2 || parts[len(parts)-1] != "base64" {
		return nil, false, errors.New("Gemini image data URL must use base64 encoding")
	}
	if _, err := base64.StdEncoding.DecodeString(payload); err != nil {
		return nil, false, fmt.Errorf("decode image data URL: %w", err)
	}
	return map[string]any{"mimeType": mimeType, "data": payload}, true, nil
}
