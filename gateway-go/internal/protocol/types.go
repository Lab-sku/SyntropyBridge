package protocol

import (
	"encoding/json"
	"fmt"
)

// Protocol identifies the client-facing or provider-native wire protocol.
type Protocol string

const (
	ProtocolOpenAIChat      Protocol = "openai.chat_completions"
	ProtocolOpenAIResponses Protocol = "openai.responses"
	ProtocolAnthropic       Protocol = "anthropic.messages"
	ProtocolGemini          Protocol = "gemini.generate_content"
)

// Operation identifies the model operation independently from its wire protocol.
type Operation string

const (
	OperationChat      Operation = "chat"
	OperationEmbedding Operation = "embedding"
	OperationRerank    Operation = "rerank"
	OperationImage     Operation = "image"
	OperationAudio     Operation = "audio"
	OperationRealtime  Operation = "realtime"
	OperationBatch     Operation = "batch"
)

// Role is intentionally extensible; provider-specific roles may be retained in Raw.
type Role string

const (
	RoleSystem    Role = "system"
	RoleDeveloper Role = "developer"
	RoleUser      Role = "user"
	RoleAssistant Role = "assistant"
	RoleTool      Role = "tool"
)

// ContentPart keeps heterogeneous message blocks intact. Raw and Extensions prevent
// the canonical layer from silently flattening fields it does not yet understand.
type ContentPart struct {
	Type       string                     `json:"type"`
	Text       *string                    `json:"text,omitempty"`
	ImageURL   *ImageURL                  `json:"image_url,omitempty"`
	InlineData *InlineData                `json:"inline_data,omitempty"`
	ToolCall   *ToolCall                  `json:"tool_call,omitempty"`
	ToolResult *ToolResult                `json:"tool_result,omitempty"`
	Reasoning  *ReasoningBlock            `json:"reasoning,omitempty"`
	Extensions map[string]json.RawMessage `json:"extensions,omitempty"`
	Raw        json.RawMessage            `json:"raw,omitempty"`
}

type ImageURL struct {
	URL    string  `json:"url"`
	Detail *string `json:"detail,omitempty"`
}

type InlineData struct {
	MIMEType string `json:"mime_type"`
	Data     []byte `json:"data"`
}

type ToolCall struct {
	ID        string          `json:"id"`
	Type      string          `json:"type,omitempty"`
	Name      string          `json:"name"`
	Arguments json.RawMessage `json:"arguments"`
}

type ToolResult struct {
	ToolCallID string        `json:"tool_call_id"`
	Content    []ContentPart `json:"content"`
	IsError    bool          `json:"is_error,omitempty"`
}

type ReasoningBlock struct {
	Text      string          `json:"text,omitempty"`
	Signature json.RawMessage `json:"signature,omitempty"`
}

type Message struct {
	Role        Role                       `json:"role"`
	Name        *string                    `json:"name,omitempty"`
	Content     []ContentPart              `json:"content,omitempty"`
	ToolCallID  *string                    `json:"tool_call_id,omitempty"`
	Refusal     *string                    `json:"refusal,omitempty"`
	Annotations []json.RawMessage          `json:"annotations,omitempty"`
	Extensions  map[string]json.RawMessage `json:"extensions,omitempty"`
	Raw         json.RawMessage            `json:"raw,omitempty"`
}

type ToolDefinition struct {
	Name        string          `json:"name"`
	Description string          `json:"description,omitempty"`
	InputSchema json.RawMessage `json:"input_schema"`
	Strict      *bool           `json:"strict,omitempty"`
}

type ReasoningConfig struct {
	Effort       *string `json:"effort,omitempty"`
	MaxTokens    *int    `json:"max_tokens,omitempty"`
	IncludeInOut bool    `json:"include_in_output,omitempty"`
}

type GenerationParameters struct {
	MaxOutputTokens *int     `json:"max_output_tokens,omitempty"`
	Temperature     *float64 `json:"temperature,omitempty"`
	TopP            *float64 `json:"top_p,omitempty"`
	TopK            *int     `json:"top_k,omitempty"`
	Stop            []string `json:"stop,omitempty"`
	Seed            *int64   `json:"seed,omitempty"`
}

// CanonicalRequest is the internal protocol boundary. It is not exposed as a public
// API and therefore can evolve without pretending every provider has identical
// semantics.
type CanonicalRequest struct {
	RequestID      string                     `json:"request_id"`
	Protocol       Protocol                   `json:"protocol"`
	Operation      Operation                  `json:"operation"`
	Model          string                     `json:"model"`
	Messages       []Message                  `json:"messages,omitempty"`
	Tools          []ToolDefinition           `json:"tools,omitempty"`
	ToolChoice     json.RawMessage            `json:"tool_choice,omitempty"`
	Reasoning      *ReasoningConfig           `json:"reasoning,omitempty"`
	ResponseFormat json.RawMessage            `json:"response_format,omitempty"`
	Parameters     GenerationParameters       `json:"parameters"`
	Stream         bool                       `json:"stream"`
	Metadata       map[string]string          `json:"metadata,omitempty"`
	Extensions     map[string]json.RawMessage `json:"extensions,omitempty"`
	RawBody        json.RawMessage            `json:"raw_body,omitempty"`
}

type Usage struct {
	InputTokens       int64 `json:"input_tokens"`
	OutputTokens      int64 `json:"output_tokens"`
	CachedInputTokens int64 `json:"cached_input_tokens,omitempty"`
	ReasoningTokens   int64 `json:"reasoning_tokens,omitempty"`
	TotalTokens       int64 `json:"total_tokens"`
}

type CanonicalResponse struct {
	RequestID    string                     `json:"request_id"`
	ResponseID   string                     `json:"response_id,omitempty"`
	ProviderID   string                     `json:"provider_id,omitempty"`
	Model        string                     `json:"model"`
	CreatedAt    int64                      `json:"created_at,omitempty"`
	Messages     []Message                  `json:"messages,omitempty"`
	Usage        *Usage                     `json:"usage,omitempty"`
	FinishReason string                     `json:"finish_reason,omitempty"`
	Extensions   map[string]json.RawMessage `json:"extensions,omitempty"`
	RawBody      json.RawMessage            `json:"raw_body,omitempty"`
}

// GatewayError is the normalized error form recorded in request attempts.
type GatewayError struct {
	Code         string          `json:"code"`
	Message      string          `json:"message"`
	HTTPStatus   int             `json:"http_status"`
	Retryable    bool            `json:"retryable"`
	ProviderCode string          `json:"provider_code,omitempty"`
	Raw          json.RawMessage `json:"raw,omitempty"`
}

func (e *GatewayError) Error() string {
	if e == nil {
		return "<nil>"
	}
	return fmt.Sprintf("%s: %s", e.Code, e.Message)
}
