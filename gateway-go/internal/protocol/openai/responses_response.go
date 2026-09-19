package openai

import (
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/protocol"
)

func EncodeResponsesResponse(response *protocol.CanonicalResponse) ([]byte, error) {
	if response == nil {
		return nil, errors.New("encode OpenAI Responses response: response is nil")
	}
	responseID := normalizeResponsesID(response.ResponseID)
	if response.ResponseID == "" {
		responseID = normalizeResponsesID(response.RequestID)
	}
	created := response.CreatedAt
	if created == 0 {
		created = time.Now().Unix()
	}

	output := make([]any, 0)
	itemIndex := 0
	for _, message := range response.Messages {
		if message.Role != protocol.RoleAssistant {
			continue
		}
		content := make([]any, 0)
		toolItems := make([]any, 0)
		for _, part := range message.Content {
			switch {
			case part.Text != nil:
				content = append(content, map[string]any{
					"type":        "output_text",
					"text":        *part.Text,
					"annotations": []any{},
				})
			case part.ToolCall != nil:
				call := part.ToolCall
				callID := call.ID
				if callID == "" {
					callID = responseItemID("call", responseID, itemIndex)
				}
				toolItems = append(toolItems, map[string]any{
					"id":        responseItemID("fc", responseID, itemIndex),
					"type":      "function_call",
					"status":    "completed",
					"call_id":   callID,
					"name":      call.Name,
					"arguments": rawArgumentsString(call.Arguments),
				})
				itemIndex++
			}
		}
		if message.Refusal != nil {
			content = append(content, map[string]any{
				"type":    "refusal",
				"refusal": *message.Refusal,
			})
		}
		if len(content) > 0 {
			output = append(output, map[string]any{
				"id":      responseItemID("msg", responseID, itemIndex),
				"type":    "message",
				"status":  "completed",
				"role":    "assistant",
				"content": content,
			})
			itemIndex++
		}
		output = append(output, toolItems...)
	}

	status := "completed"
	var incomplete any
	if response.FinishReason == "length" {
		status = "incomplete"
		incomplete = map[string]any{"reason": "max_output_tokens"}
	}
	object := map[string]any{
		"id":                 responseID,
		"object":             "response",
		"created_at":         created,
		"status":             status,
		"error":              nil,
		"incomplete_details": incomplete,
		"model":              response.Model,
		"output":             output,
	}
	if response.Usage != nil {
		object["usage"] = encodeResponsesUsage(response.Usage)
	}
	return json.Marshal(object)
}

func encodeResponsesUsage(usage *protocol.Usage) map[string]any {
	return map[string]any{
		"input_tokens": usage.InputTokens,
		"input_tokens_details": map[string]any{
			"cached_tokens": usage.CachedInputTokens,
		},
		"output_tokens": usage.OutputTokens,
		"output_tokens_details": map[string]any{
			"reasoning_tokens": usage.ReasoningTokens,
		},
		"total_tokens": usage.TotalTokens,
	}
}

func responseItemID(prefix, responseID string, index int) string {
	base := strings.TrimPrefix(strings.TrimSpace(responseID), "resp_")
	if base == "" {
		base = "gateway"
	}
	var builder strings.Builder
	for _, char := range base {
		if (char >= 'a' && char <= 'z') || (char >= 'A' && char <= 'Z') || (char >= '0' && char <= '9') || char == '_' || char == '-' {
			builder.WriteRune(char)
		}
		if builder.Len() >= 40 {
			break
		}
	}
	if builder.Len() == 0 {
		builder.WriteString("gateway")
	}
	return fmt.Sprintf("%s_%s_%d", prefix, builder.String(), index)
}

func rawArgumentsString(raw json.RawMessage) string {
	if len(raw) == 0 {
		return "{}"
	}
	return string(raw)
}

func normalizeResponsesID(value string) string {
	value = strings.TrimSpace(value)
	if value == "" {
		return "resp_gateway"
	}
	if strings.HasPrefix(value, "resp_") {
		return value
	}
	value = strings.TrimPrefix(value, "chatcmpl_")
	value = strings.TrimPrefix(value, "req_")
	return "resp_" + value
}
