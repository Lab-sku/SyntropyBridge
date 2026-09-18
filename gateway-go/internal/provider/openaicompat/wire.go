package openaicompat

import (
	"encoding/json"

	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/protocol"
)

type chatResponse struct {
	ID      string `json:"id"`
	Model   string `json:"model"`
	Created int64  `json:"created"`
	Choices []struct {
		Index        int             `json:"index"`
		Message      json.RawMessage `json:"message"`
		FinishReason *string         `json:"finish_reason"`
	} `json:"choices"`
	Usage *wireUsage `json:"usage"`
}

type wireUsage struct {
	PromptTokens     int64 `json:"prompt_tokens"`
	CompletionTokens int64 `json:"completion_tokens"`
	TotalTokens      int64 `json:"total_tokens"`
	PromptDetails    struct {
		CachedTokens int64 `json:"cached_tokens"`
	} `json:"prompt_tokens_details"`
	CompletionDetails struct {
		ReasoningTokens int64 `json:"reasoning_tokens"`
	} `json:"completion_tokens_details"`
}

func (u *wireUsage) canonical() *protocol.Usage {
	if u == nil {
		return nil
	}
	return &protocol.Usage{
		InputTokens:       u.PromptTokens,
		OutputTokens:      u.CompletionTokens,
		CachedInputTokens: u.PromptDetails.CachedTokens,
		ReasoningTokens:   u.CompletionDetails.ReasoningTokens,
		TotalTokens:       u.TotalTokens,
	}
}

type chatToolCallDelta struct {
	Index    int    `json:"index"`
	ID       string `json:"id"`
	Type     string `json:"type"`
	Function struct {
		Name      string `json:"name"`
		Arguments string `json:"arguments"`
	} `json:"function"`
}

type chatDelta struct {
	Role             string              `json:"role"`
	Content          *string             `json:"content"`
	ReasoningContent *string             `json:"reasoning_content"`
	Reasoning        *string             `json:"reasoning"`
	Thinking         *string             `json:"thinking"`
	ToolCalls        []chatToolCallDelta `json:"tool_calls"`
}

func (d chatDelta) reasoningDelta() *string {
	if d.ReasoningContent != nil {
		return d.ReasoningContent
	}
	if d.Reasoning != nil {
		return d.Reasoning
	}
	return d.Thinking
}

type chatChunk struct {
	ID      string `json:"id"`
	Model   string `json:"model"`
	Choices []struct {
		Index        int       `json:"index"`
		Delta        chatDelta `json:"delta"`
		FinishReason *string   `json:"finish_reason"`
	} `json:"choices"`
	Usage *wireUsage `json:"usage"`
	Error *struct {
		Message string `json:"message"`
		Type    string `json:"type"`
		Code    string `json:"code"`
	} `json:"error"`
}
