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

type usageMetadata struct {
	PromptTokenCount        int64 `json:"promptTokenCount"`
	CandidatesTokenCount    int64 `json:"candidatesTokenCount"`
	TotalTokenCount         int64 `json:"totalTokenCount"`
	CachedContentTokenCount int64 `json:"cachedContentTokenCount"`
	ThoughtsTokenCount      int64 `json:"thoughtsTokenCount"`
}

func (u usageMetadata) canonical() *protocol.Usage {
	return &protocol.Usage{
		InputTokens:       u.PromptTokenCount,
		OutputTokens:      u.CandidatesTokenCount,
		TotalTokens:       u.TotalTokenCount,
		CachedInputTokens: u.CachedContentTokenCount,
		ReasoningTokens:   u.ThoughtsTokenCount,
	}
}

func decodeGenerateContentResponse(body []byte) (*protocol.CanonicalResponse, error) {
	var envelope map[string]json.RawMessage
	if err := json.Unmarshal(body, &envelope); err != nil {
		return nil, fmt.Errorf("decode Gemini response JSON: %w", err)
	}
	var candidates []json.RawMessage
	if err := json.Unmarshal(envelope["candidates"], &candidates); err != nil || len(candidates) == 0 {
		return nil, errors.New("decode Gemini response: candidates are empty")
	}
	result := &protocol.CanonicalResponse{
		ProviderID: Name,
		RawBody:    append(json.RawMessage(nil), body...),
	}
	for _, rawCandidate := range candidates {
		var candidate map[string]json.RawMessage
		if json.Unmarshal(rawCandidate, &candidate) != nil {
			continue
		}
		var content map[string]json.RawMessage
		if json.Unmarshal(candidate["content"], &content) == nil {
			var parts []json.RawMessage
			_ = json.Unmarshal(content["parts"], &parts)
			message := protocol.Message{Role: protocol.RoleAssistant}
			for _, rawPart := range parts {
				part, err := decodePart(rawPart)
				if err != nil {
					return nil, err
				}
				message.Content = append(message.Content, part)
			}
			result.Messages = append(result.Messages, message)
		}
		if result.FinishReason == "" {
			var finish string
			_ = json.Unmarshal(candidate["finishReason"], &finish)
			result.FinishReason = normalizeFinishReason(finish)
		}
	}
	var usage usageMetadata
	if json.Unmarshal(envelope["usageMetadata"], &usage) == nil {
		result.Usage = usage.canonical()
	}
	var modelVersion string
	_ = json.Unmarshal(envelope["modelVersion"], &modelVersion)
	result.Model = modelVersion
	var responseID string
	_ = json.Unmarshal(envelope["responseId"], &responseID)
	result.ResponseID = responseID
	return result, nil
}

func decodePart(raw json.RawMessage) (protocol.ContentPart, error) {
	var object map[string]json.RawMessage
	if err := json.Unmarshal(raw, &object); err != nil {
		return protocol.ContentPart{}, err
	}
	part := protocol.ContentPart{Raw: append(json.RawMessage(nil), raw...)}
	if textRaw, ok := object["text"]; ok {
		var text string
		_ = json.Unmarshal(textRaw, &text)
		var thought bool
		_ = json.Unmarshal(object["thought"], &thought)
		if thought {
			signature := append(json.RawMessage(nil), object["thoughtSignature"]...)
			part.Type = "reasoning"
			part.Reasoning = &protocol.ReasoningBlock{Text: text, Signature: signature}
		} else {
			part.Type = "text"
			part.Text = &text
		}
		return part, nil
	}
	if rawCall, ok := object["functionCall"]; ok {
		var call map[string]json.RawMessage
		if err := json.Unmarshal(rawCall, &call); err != nil {
			return protocol.ContentPart{}, err
		}
		args := append(json.RawMessage(nil), call["args"]...)
		if len(bytes.TrimSpace(args)) == 0 {
			args = json.RawMessage("{}")
		}
		part.Type = "tool_call"
		part.ToolCall = &protocol.ToolCall{
			ID: jsonString(call["id"]), Type: "function", Name: jsonString(call["name"]), Arguments: args,
		}
		return part, nil
	}
	if rawData, ok := object["inlineData"]; ok {
		var data map[string]json.RawMessage
		if err := json.Unmarshal(rawData, &data); err != nil {
			return protocol.ContentPart{}, err
		}
		decoded, err := base64.StdEncoding.DecodeString(jsonString(data["data"]))
		if err != nil {
			return protocol.ContentPart{}, err
		}
		part.Type = "inline_data"
		part.InlineData = &protocol.InlineData{MIMEType: jsonString(data["mimeType"]), Data: decoded}
		return part, nil
	}
	part.Type = "gemini_raw"
	return part, nil
}

func normalizeFinishReason(reason string) string {
	switch strings.ToUpper(reason) {
	case "", "FINISH_REASON_UNSPECIFIED":
		return ""
	case "STOP":
		return "stop"
	case "MAX_TOKENS":
		return "length"
	case "SAFETY", "RECITATION", "PROHIBITED_CONTENT", "SPII":
		return "content_filter"
	case "MALFORMED_FUNCTION_CALL", "UNEXPECTED_TOOL_CALL":
		return "tool_error"
	default:
		return strings.ToLower(reason)
	}
}

func jsonString(raw json.RawMessage) string {
	var value string
	_ = json.Unmarshal(raw, &value)
	return value
}
