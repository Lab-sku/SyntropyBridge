package openai

import (
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/protocol"
)

type responsesStreamTool struct {
	OutputIndex int
	ItemID      string
	CallID      string
	Name        string
	Arguments   strings.Builder
	Done        bool
}

type ResponsesStreamEncoder struct {
	responseID string
	model      string
	createdAt  int64
	sequence   int64
	created    bool
	completed  bool

	messageID    string
	messageIndex int
	messageAdded bool
	textAdded    bool
	textDone     bool
	text         strings.Builder

	nextOutputIndex int
	tools           map[int]*responsesStreamTool
	output          []any
	usage           *protocol.Usage
}

func NewResponsesStreamEncoder(requestID, model string) *ResponsesStreamEncoder {
	return &ResponsesStreamEncoder{
		responseID: normalizeResponsesID(requestID),
		model:      model,
		createdAt:  time.Now().Unix(),
		tools:      make(map[int]*responsesStreamTool),
	}
}

func (e *ResponsesStreamEncoder) Encode(event protocol.StreamEvent) ([][]byte, error) {
	if e == nil {
		return nil, errors.New("encode OpenAI Responses stream: encoder is nil")
	}
	if e.completed {
		return nil, nil
	}
	if event.Type == protocol.StreamEventWireChunk {
		return nil, nil
	}
	if event.ResponseID != "" && !e.created {
		e.responseID = normalizeResponsesID(event.ResponseID)
	}

	frames := make([][]byte, 0, 6)
	if !e.created {
		frame, err := e.eventFrame("response.created", map[string]any{
			"response": e.responseObject("in_progress"),
		})
		if err != nil {
			return nil, err
		}
		frames = append(frames, frame)
		e.created = true
	}

	switch event.Type {
	case protocol.StreamEventResponseStart:
		return frames, nil
	case protocol.StreamEventMessageStart:
		added, err := e.ensureMessageAdded()
		if err != nil {
			return nil, err
		}
		frames = append(frames, added...)
	case protocol.StreamEventTextDelta:
		if event.TextDelta == nil {
			return frames, nil
		}
		added, err := e.ensureTextAdded()
		if err != nil {
			return nil, err
		}
		frames = append(frames, added...)
		e.text.WriteString(*event.TextDelta)
		frame, err := e.eventFrame("response.output_text.delta", map[string]any{
			"response_id":   e.responseID,
			"item_id":       e.messageID,
			"output_index":  e.messageIndex,
			"content_index": 0,
			"delta":         *event.TextDelta,
		})
		if err != nil {
			return nil, err
		}
		frames = append(frames, frame)
	case protocol.StreamEventToolCallStart, protocol.StreamEventToolCallDelta:
		if event.ToolCallDelta != nil {
			toolFrames, err := e.toolDeltaFrames(event.ToolCallDelta)
			if err != nil {
				return nil, err
			}
			frames = append(frames, toolFrames...)
		}
	case protocol.StreamEventUsage:
		if event.Usage != nil {
			copyUsage := *event.Usage
			e.usage = &copyUsage
		}
	case protocol.StreamEventMessageEnd:
		done, err := e.finalizeOpenItems()
		if err != nil {
			return nil, err
		}
		frames = append(frames, done...)
	case protocol.StreamEventResponseEnd:
		done, err := e.finalizeOpenItems()
		if err != nil {
			return nil, err
		}
		frames = append(frames, done...)
		completed, err := e.eventFrame("response.completed", map[string]any{
			"response": e.responseObject("completed"),
		})
		if err != nil {
			return nil, err
		}
		frames = append(frames, completed)
		e.completed = true
	case protocol.StreamEventError:
		if event.Error != nil {
			frame, err := e.eventFrame("error", map[string]any{
				"error": map[string]any{
					"code":    event.Error.Code,
					"message": event.Error.Message,
					"type":    "server_error",
				},
			})
			if err != nil {
				return nil, err
			}
			frames = append(frames, frame)
		}
	}
	return frames, nil
}

func (e *ResponsesStreamEncoder) responseObject(status string) map[string]any {
	object := map[string]any{
		"id":                 e.responseID,
		"object":             "response",
		"created_at":         e.createdAt,
		"status":             status,
		"error":              nil,
		"incomplete_details": nil,
		"model":              e.model,
		"output":             e.output,
	}
	if e.usage != nil {
		object["usage"] = encodeResponsesUsage(e.usage)
	} else {
		object["usage"] = nil
	}
	return object
}

func (e *ResponsesStreamEncoder) ensureMessageAdded() ([][]byte, error) {
	if e.messageAdded {
		return nil, nil
	}
	e.messageIndex = e.nextOutputIndex
	e.nextOutputIndex++
	e.messageID = responseItemID("msg", e.responseID, e.messageIndex)
	e.messageAdded = true
	frame, err := e.eventFrame("response.output_item.added", map[string]any{
		"response_id":  e.responseID,
		"output_index": e.messageIndex,
		"item": map[string]any{
			"id":      e.messageID,
			"type":    "message",
			"status":  "in_progress",
			"role":    "assistant",
			"content": []any{},
		},
	})
	if err != nil {
		return nil, err
	}
	return [][]byte{frame}, nil
}

func (e *ResponsesStreamEncoder) ensureTextAdded() ([][]byte, error) {
	frames, err := e.ensureMessageAdded()
	if err != nil {
		return nil, err
	}
	if e.textAdded {
		return frames, nil
	}
	e.textAdded = true
	frame, err := e.eventFrame("response.content_part.added", map[string]any{
		"response_id":   e.responseID,
		"item_id":       e.messageID,
		"output_index":  e.messageIndex,
		"content_index": 0,
		"part": map[string]any{
			"type":        "output_text",
			"text":        "",
			"annotations": []any{},
		},
	})
	if err != nil {
		return nil, err
	}
	return append(frames, frame), nil
}

func (e *ResponsesStreamEncoder) toolDeltaFrames(delta *protocol.ToolCallDelta) ([][]byte, error) {
	tool := e.tools[delta.Index]
	frames := make([][]byte, 0, 2)
	if tool == nil {
		outputIndex := e.nextOutputIndex
		e.nextOutputIndex++
		callID := delta.ID
		if callID == "" {
			callID = responseItemID("call", e.responseID, outputIndex)
		}
		tool = &responsesStreamTool{
			OutputIndex: outputIndex,
			ItemID:      responseItemID("fc", e.responseID, outputIndex),
			CallID:      callID,
			Name:        delta.Name,
		}
		e.tools[delta.Index] = tool
		frame, err := e.eventFrame("response.output_item.added", map[string]any{
			"response_id":  e.responseID,
			"output_index": outputIndex,
			"item": map[string]any{
				"id":        tool.ItemID,
				"type":      "function_call",
				"status":    "in_progress",
				"call_id":   tool.CallID,
				"name":      tool.Name,
				"arguments": "",
			},
		})
		if err != nil {
			return nil, err
		}
		frames = append(frames, frame)
	}
	if delta.Name != "" {
		tool.Name = delta.Name
	}
	if delta.ID != "" {
		tool.CallID = delta.ID
	}
	if delta.ArgumentsFragment != "" {
		tool.Arguments.WriteString(delta.ArgumentsFragment)
		frame, err := e.eventFrame("response.function_call_arguments.delta", map[string]any{
			"response_id":  e.responseID,
			"item_id":      tool.ItemID,
			"output_index": tool.OutputIndex,
			"delta":        delta.ArgumentsFragment,
		})
		if err != nil {
			return nil, err
		}
		frames = append(frames, frame)
	}
	return frames, nil
}

func (e *ResponsesStreamEncoder) finalizeOpenItems() ([][]byte, error) {
	frames := make([][]byte, 0, 8)
	if e.textAdded && !e.textDone {
		text := e.text.String()
		done, err := e.eventFrame("response.output_text.done", map[string]any{
			"response_id":   e.responseID,
			"item_id":       e.messageID,
			"output_index":  e.messageIndex,
			"content_index": 0,
			"text":          text,
		})
		if err != nil {
			return nil, err
		}
		part := map[string]any{"type": "output_text", "text": text, "annotations": []any{}}
		partDone, err := e.eventFrame("response.content_part.done", map[string]any{
			"response_id":   e.responseID,
			"item_id":       e.messageID,
			"output_index":  e.messageIndex,
			"content_index": 0,
			"part":          part,
		})
		if err != nil {
			return nil, err
		}
		item := map[string]any{
			"id":      e.messageID,
			"type":    "message",
			"status":  "completed",
			"role":    "assistant",
			"content": []any{part},
		}
		itemDone, err := e.eventFrame("response.output_item.done", map[string]any{
			"response_id":  e.responseID,
			"output_index": e.messageIndex,
			"item":         item,
		})
		if err != nil {
			return nil, err
		}
		e.output = append(e.output, item)
		e.textDone = true
		frames = append(frames, done, partDone, itemDone)
	}

	indexes := make([]int, 0, len(e.tools))
	for index := range e.tools {
		indexes = append(indexes, index)
	}
	sort.Ints(indexes)
	for _, index := range indexes {
		tool := e.tools[index]
		if tool.Done {
			continue
		}
		arguments := tool.Arguments.String()
		done, err := e.eventFrame("response.function_call_arguments.done", map[string]any{
			"response_id":  e.responseID,
			"item_id":      tool.ItemID,
			"output_index": tool.OutputIndex,
			"arguments":    arguments,
		})
		if err != nil {
			return nil, err
		}
		item := map[string]any{
			"id":        tool.ItemID,
			"type":      "function_call",
			"status":    "completed",
			"call_id":   tool.CallID,
			"name":      tool.Name,
			"arguments": arguments,
		}
		itemDone, err := e.eventFrame("response.output_item.done", map[string]any{
			"response_id":  e.responseID,
			"output_index": tool.OutputIndex,
			"item":         item,
		})
		if err != nil {
			return nil, err
		}
		e.output = append(e.output, item)
		tool.Done = true
		frames = append(frames, done, itemDone)
	}
	return frames, nil
}

func (e *ResponsesStreamEncoder) eventFrame(eventType string, payload map[string]any) ([]byte, error) {
	e.sequence++
	payload["type"] = eventType
	payload["sequence_number"] = e.sequence
	body, err := json.Marshal(payload)
	if err != nil {
		return nil, fmt.Errorf("encode Responses stream event %s: %w", eventType, err)
	}
	return []byte("event: " + eventType + "\ndata: " + string(body) + "\n\n"), nil
}

func EncodeResponsesErrorEvent(code, message, errorType string) []byte {
	if code == "" {
		code = "gateway_error"
	}
	if message == "" {
		message = "gateway request failed"
	}
	if errorType == "" {
		errorType = "server_error"
	}
	body, _ := json.Marshal(map[string]any{
		"type": "error",
		"error": map[string]any{
			"code":    code,
			"message": message,
			"type":    errorType,
		},
	})
	return []byte("event: error\ndata: " + string(body) + "\n\n")
}
