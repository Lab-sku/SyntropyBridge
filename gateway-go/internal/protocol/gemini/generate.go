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

var ErrInvalidGenerateContentRequest = errors.New("invalid Gemini GenerateContent request")

func DecodeGenerateContentRequest(body []byte, model, requestID string, stream bool) (*protocol.CanonicalRequest, error) {
	model = strings.TrimPrefix(strings.TrimSpace(model), "models/")
	if model == "" {
		return nil, fmt.Errorf("%w: model is required", ErrInvalidGenerateContentRequest)
	}
	object, err := decodeObject(body)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrInvalidGenerateContentRequest, err)
	}
	var contents []json.RawMessage
	if err := json.Unmarshal(object["contents"], &contents); err != nil || len(contents) == 0 {
		return nil, fmt.Errorf("%w: contents must be a non-empty array", ErrInvalidGenerateContentRequest)
	}

	req := &protocol.CanonicalRequest{
		RequestID: requestID,
		Protocol:  protocol.ProtocolGemini,
		Operation: protocol.OperationChat,
		Model:     model,
		Stream:    stream,
		RawBody:   append(json.RawMessage(nil), body...),
	}

	if raw := object["systemInstruction"]; nonNull(raw) {
		parts, err := decodePartsContainer(raw)
		if err != nil {
			return nil, fmt.Errorf("%w: systemInstruction: %v", ErrInvalidGenerateContentRequest, err)
		}
		req.Messages = append(req.Messages, protocol.Message{Role: protocol.RoleSystem, Content: parts})
	}

	for index, raw := range contents {
		messages, err := decodeContent(raw)
		if err != nil {
			return nil, fmt.Errorf("%w: contents[%d]: %v", ErrInvalidGenerateContentRequest, index, err)
		}
		req.Messages = append(req.Messages, messages...)
	}

	if raw := object["generationConfig"]; nonNull(raw) {
		if err := decodeGenerationConfig(req, raw); err != nil {
			return nil, fmt.Errorf("%w: generationConfig: %v", ErrInvalidGenerateContentRequest, err)
		}
	}
	if raw := object["tools"]; nonNull(raw) {
		tools, err := decodeTools(raw)
		if err != nil {
			return nil, fmt.Errorf("%w: tools: %v", ErrInvalidGenerateContentRequest, err)
		}
		req.Tools = tools
	}
	if raw := object["toolConfig"]; nonNull(raw) {
		choice, err := decodeToolConfig(raw)
		if err != nil {
			return nil, fmt.Errorf("%w: toolConfig: %v", ErrInvalidGenerateContentRequest, err)
		}
		req.ToolChoice = choice
	}
	return req, nil
}

func EncodeGenerateContentResponse(response *protocol.CanonicalResponse, publicModel string) ([]byte, error) {
	if response == nil {
		return nil, errors.New("encode Gemini response: response is nil")
	}
	parts := make([]any, 0)
	for _, message := range response.Messages {
		if message.Role != protocol.RoleAssistant {
			continue
		}
		for _, part := range message.Content {
			encoded, err := encodeResponsePart(part)
			if err != nil {
				return nil, err
			}
			if encoded != nil {
				parts = append(parts, encoded)
			}
		}
	}

	candidate := map[string]any{
		"content":      map[string]any{"role": "model", "parts": parts},
		"finishReason": geminiFinishReason(response.FinishReason),
		"index":        0,
	}
	object := map[string]any{
		"candidates":   []any{candidate},
		"modelVersion": publicModel,
	}
	if response.ResponseID != "" {
		object["responseId"] = response.ResponseID
	}
	if response.Usage != nil {
		object["usageMetadata"] = encodeUsage(response.Usage)
	}
	return json.Marshal(object)
}

func decodeContent(raw json.RawMessage) ([]protocol.Message, error) {
	object, err := decodeObject(raw)
	if err != nil {
		return nil, err
	}
	roleText := jsonString(object["role"])
	role := protocol.RoleUser
	if roleText == "model" {
		role = protocol.RoleAssistant
	} else if roleText != "" && roleText != "user" {
		return nil, fmt.Errorf("unsupported role %q", roleText)
	}

	var rawParts []json.RawMessage
	if err := json.Unmarshal(object["parts"], &rawParts); err != nil {
		return nil, errors.New("parts must be an array")
	}
	if role == protocol.RoleAssistant {
		parts := make([]protocol.ContentPart, 0, len(rawParts))
		for _, rawPart := range rawParts {
			part, toolResult, err := decodePart(rawPart)
			if err != nil {
				return nil, err
			}
			if toolResult != nil {
				return nil, errors.New("functionResponse is not valid in model content")
			}
			parts = append(parts, part)
		}
		return []protocol.Message{{Role: role, Content: parts}}, nil
	}

	messages := make([]protocol.Message, 0)
	current := make([]protocol.ContentPart, 0)
	flushUser := func() {
		if len(current) == 0 {
			return
		}
		messages = append(messages, protocol.Message{
			Role:    protocol.RoleUser,
			Content: append([]protocol.ContentPart(nil), current...),
		})
		current = current[:0]
	}
	for _, rawPart := range rawParts {
		part, toolResult, err := decodePart(rawPart)
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

func decodePartsContainer(raw json.RawMessage) ([]protocol.ContentPart, error) {
	object, err := decodeObject(raw)
	if err != nil {
		return nil, err
	}
	var rawParts []json.RawMessage
	if err := json.Unmarshal(object["parts"], &rawParts); err != nil {
		return nil, errors.New("parts must be an array")
	}
	parts := make([]protocol.ContentPart, 0, len(rawParts))
	for _, rawPart := range rawParts {
		part, toolResult, err := decodePart(rawPart)
		if err != nil {
			return nil, err
		}
		if toolResult != nil {
			return nil, errors.New("functionResponse is invalid in systemInstruction")
		}
		parts = append(parts, part)
	}
	return parts, nil
}

func decodePart(raw json.RawMessage) (protocol.ContentPart, *protocol.ToolResult, error) {
	object, err := decodeObject(raw)
	if err != nil {
		return protocol.ContentPart{}, nil, err
	}
	part := protocol.ContentPart{Raw: append(json.RawMessage(nil), raw...)}

	if rawText, ok := object["text"]; ok {
		text := jsonString(rawText)
		var thought bool
		_ = json.Unmarshal(object["thought"], &thought)
		if thought {
			part.Type = "reasoning"
			part.Reasoning = &protocol.ReasoningBlock{
				Text:      text,
				Signature: append(json.RawMessage(nil), object["thoughtSignature"]...),
			}
		} else {
			part.Type = "text"
			part.Text = &text
		}
		return part, nil, nil
	}
	if rawData, ok := object["inlineData"]; ok {
		data, err := decodeObject(rawData)
		if err != nil {
			return protocol.ContentPart{}, nil, errors.New("inlineData must be an object")
		}
		decoded, err := base64.StdEncoding.DecodeString(jsonString(data["data"]))
		if err != nil {
			return protocol.ContentPart{}, nil, fmt.Errorf("decode inlineData: %w", err)
		}
		part.Type = "inline_data"
		part.InlineData = &protocol.InlineData{MIMEType: jsonString(data["mimeType"]), Data: decoded}
		return part, nil, nil
	}
	if rawFile, ok := object["fileData"]; ok {
		file, err := decodeObject(rawFile)
		if err != nil {
			return protocol.ContentPart{}, nil, errors.New("fileData must be an object")
		}
		part.Type = "image_url"
		part.ImageURL = &protocol.ImageURL{URL: jsonString(file["fileUri"])}
		return part, nil, nil
	}
	if rawCall, ok := object["functionCall"]; ok {
		call, err := decodeObject(rawCall)
		if err != nil {
			return protocol.ContentPart{}, nil, errors.New("functionCall must be an object")
		}
		args := append(json.RawMessage(nil), call["args"]...)
		if !nonNull(args) {
			args = json.RawMessage("{}")
		}
		part.Type = "tool_call"
		part.ToolCall = &protocol.ToolCall{
			ID:        jsonString(call["id"]),
			Type:      "function",
			Name:      jsonString(call["name"]),
			Arguments: args,
		}
		return part, nil, nil
	}
	if rawResponse, ok := object["functionResponse"]; ok {
		response, err := decodeObject(rawResponse)
		if err != nil {
			return protocol.ContentPart{}, nil, errors.New("functionResponse must be an object")
		}
		callID := jsonString(response["id"])
		if callID == "" {
			callID = jsonString(response["name"])
		}
		content := response["response"]
		text := ""
		if nonNull(content) {
			var value string
			if json.Unmarshal(content, &value) == nil {
				text = value
			} else {
				text = string(content)
			}
		}
		textPart := protocol.ContentPart{Type: "text", Text: &text}
		return protocol.ContentPart{}, &protocol.ToolResult{
			ToolCallID: callID,
			Content:    []protocol.ContentPart{textPart},
		}, nil
	}

	part.Type = "gemini_raw"
	return part, nil, nil
}

func decodeGenerationConfig(req *protocol.CanonicalRequest, raw json.RawMessage) error {
	object, err := decodeObject(raw)
	if err != nil {
		return errors.New("must be an object")
	}
	decodeOptional(object, "maxOutputTokens", &req.Parameters.MaxOutputTokens)
	decodeOptional(object, "temperature", &req.Parameters.Temperature)
	decodeOptional(object, "topP", &req.Parameters.TopP)
	decodeOptional(object, "topK", &req.Parameters.TopK)
	decodeOptional(object, "stopSequences", &req.Parameters.Stop)

	if jsonString(object["responseMimeType"]) == "application/json" {
		schema := object["responseJsonSchema"]
		if nonNull(schema) {
			encoded, _ := json.Marshal(map[string]any{"type": "json_schema", "schema": json.RawMessage(schema)})
			req.ResponseFormat = encoded
		} else {
			req.ResponseFormat = json.RawMessage("{\"type\":\"json_object\"}")
		}
	}
	if rawThinking := object["thinkingConfig"]; nonNull(rawThinking) {
		thinking, err := decodeObject(rawThinking)
		if err != nil {
			return errors.New("thinkingConfig must be an object")
		}
		config := &protocol.ReasoningConfig{}
		decodeOptional(thinking, "thinkingBudget", &config.MaxTokens)
		decodeOptional(thinking, "includeThoughts", &config.IncludeInOut)
		if level := jsonString(thinking["thinkingLevel"]); level != "" {
			value := strings.ToLower(level)
			config.Effort = &value
		}
		req.Reasoning = config
	}
	return nil
}

func decodeTools(raw json.RawMessage) ([]protocol.ToolDefinition, error) {
	var groups []json.RawMessage
	if err := json.Unmarshal(raw, &groups); err != nil {
		return nil, errors.New("must be an array")
	}
	tools := make([]protocol.ToolDefinition, 0)
	for _, rawGroup := range groups {
		group, err := decodeObject(rawGroup)
		if err != nil {
			return nil, err
		}
		var declarations []json.RawMessage
		if rawDeclarations := group["functionDeclarations"]; nonNull(rawDeclarations) {
			if err := json.Unmarshal(rawDeclarations, &declarations); err != nil {
				return nil, errors.New("functionDeclarations must be an array")
			}
		}
		for _, rawDeclaration := range declarations {
			declaration, err := decodeObject(rawDeclaration)
			if err != nil {
				return nil, err
			}
			name := jsonString(declaration["name"])
			if name == "" {
				return nil, errors.New("function declaration name is required")
			}
			schema := append(json.RawMessage(nil), declaration["parameters"]...)
			if !nonNull(schema) {
				schema = json.RawMessage("{}")
			}
			tools = append(tools, protocol.ToolDefinition{
				Name:        name,
				Description: jsonString(declaration["description"]),
				InputSchema: schema,
			})
		}
	}
	return tools, nil
}

func decodeToolConfig(raw json.RawMessage) (json.RawMessage, error) {
	object, err := decodeObject(raw)
	if err != nil {
		return nil, errors.New("must be an object")
	}
	config, err := decodeObject(object["functionCallingConfig"])
	if err != nil {
		return nil, errors.New("functionCallingConfig must be an object")
	}
	mode := strings.ToUpper(jsonString(config["mode"]))
	switch mode {
	case "", "AUTO":
		return json.RawMessage("\"auto\""), nil
	case "NONE":
		return json.RawMessage("\"none\""), nil
	case "ANY":
		var names []string
		_ = json.Unmarshal(config["allowedFunctionNames"], &names)
		if len(names) == 1 {
			return json.Marshal(map[string]any{
				"type":     "function",
				"function": map[string]any{"name": names[0]},
			})
		}
		return json.RawMessage("\"required\""), nil
	default:
		return nil, fmt.Errorf("unsupported mode %q", mode)
	}
}

func encodeResponsePart(part protocol.ContentPart) (any, error) {
	switch {
	case part.Text != nil:
		return map[string]any{"text": *part.Text}, nil
	case part.Reasoning != nil:
		block := map[string]any{"text": part.Reasoning.Text, "thought": true}
		if nonNull(part.Reasoning.Signature) {
			var signature string
			if json.Unmarshal(part.Reasoning.Signature, &signature) == nil && signature != "" {
				block["thoughtSignature"] = signature
			}
		}
		return block, nil
	case part.ToolCall != nil:
		args := any(map[string]any{})
		if nonNull(part.ToolCall.Arguments) {
			if err := json.Unmarshal(part.ToolCall.Arguments, &args); err != nil {
				return nil, err
			}
		}
		return map[string]any{"functionCall": map[string]any{
			"id": part.ToolCall.ID, "name": part.ToolCall.Name, "args": args,
		}}, nil
	case part.InlineData != nil:
		return map[string]any{"inlineData": map[string]any{
			"mimeType": part.InlineData.MIMEType,
			"data":     base64.StdEncoding.EncodeToString(part.InlineData.Data),
		}}, nil
	default:
		return nil, nil
	}
}

func geminiFinishReason(reason string) string {
	switch reason {
	case "", "stop":
		return "STOP"
	case "length":
		return "MAX_TOKENS"
	case "content_filter":
		return "SAFETY"
	case "tool_calls":
		return "STOP"
	default:
		return strings.ToUpper(reason)
	}
}

func encodeUsage(usage *protocol.Usage) map[string]any {
	return map[string]any{
		"promptTokenCount":        usage.InputTokens,
		"candidatesTokenCount":    usage.OutputTokens,
		"totalTokenCount":         usage.TotalTokens,
		"cachedContentTokenCount": usage.CachedInputTokens,
		"thoughtsTokenCount":      usage.ReasoningTokens,
	}
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
