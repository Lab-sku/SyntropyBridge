package gemini

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"

	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/protocol"
)

type toolAccumulator struct {
	id        string
	name      string
	arguments strings.Builder
	done      bool
}

type StreamEncoder struct {
	responseID string
	model      string
	tools      map[int]*toolAccumulator
	usage      *protocol.Usage
}

func NewStreamEncoder(requestID, model string) *StreamEncoder {
	return &StreamEncoder{
		responseID: requestID,
		model:      model,
		tools:      make(map[int]*toolAccumulator),
	}
}

func (e *StreamEncoder) Encode(event protocol.StreamEvent) ([][]byte, error) {
	if e == nil {
		return nil, errors.New("encode Gemini stream: encoder is nil")
	}
	if event.ResponseID != "" && e.responseID == "" {
		e.responseID = event.ResponseID
	}

	switch event.Type {
	case protocol.StreamEventWireChunk, protocol.StreamEventResponseStart, protocol.StreamEventMessageStart:
		return nil, nil
	case protocol.StreamEventTextDelta:
		if event.TextDelta == nil {
			return nil, nil
		}
		return e.chunk([]any{map[string]any{"text": *event.TextDelta}}, "", nil)
	case protocol.StreamEventReasoningDelta:
		if event.ReasoningDelta == nil {
			return nil, nil
		}
		part := map[string]any{"text": *event.ReasoningDelta, "thought": true}
		if raw := event.Extensions["anthropic.signature_delta"]; len(raw) > 0 {
			var signature string
			if json.Unmarshal(raw, &signature) == nil && signature != "" {
				part["thoughtSignature"] = signature
			}
		}
		return e.chunk([]any{part}, "", nil)
	case protocol.StreamEventToolCallStart, protocol.StreamEventToolCallDelta:
		if event.ToolCallDelta != nil {
			e.accumulateTool(event.ToolCallDelta)
		}
		return nil, nil
	case protocol.StreamEventToolCallEnd:
		if event.ToolCallDelta == nil {
			return nil, nil
		}
		tool := e.accumulateTool(event.ToolCallDelta)
		return e.flushTool(event.ToolCallDelta.Index, tool)
	case protocol.StreamEventUsage:
		if event.Usage != nil {
			copyUsage := *event.Usage
			e.usage = &copyUsage
		}
		return nil, nil
	case protocol.StreamEventMessageEnd:
		frames, err := e.flushTools()
		if err != nil {
			return nil, err
		}
		finish := ""
		if event.FinishReason != nil {
			finish = geminiFinishReason(*event.FinishReason)
		}
		finishFrames, err := e.chunk(nil, finish, e.usage)
		if err != nil {
			return nil, err
		}
		return append(frames, finishFrames...), nil
	case protocol.StreamEventResponseEnd:
		return nil, nil
	case protocol.StreamEventError:
		if event.Error != nil {
			return [][]byte{EncodeErrorEvent(500, event.Error.Message, event.Error.Code)}, nil
		}
	}
	return nil, nil
}

func (e *StreamEncoder) accumulateTool(delta *protocol.ToolCallDelta) *toolAccumulator {
	tool := e.tools[delta.Index]
	if tool == nil {
		tool = &toolAccumulator{}
		e.tools[delta.Index] = tool
	}
	if delta.ID != "" {
		tool.id = delta.ID
	}
	if delta.Name != "" {
		tool.name = delta.Name
	}
	if delta.ArgumentsFragment != "" {
		tool.arguments.WriteString(delta.ArgumentsFragment)
	}
	return tool
}

func (e *StreamEncoder) flushTool(index int, tool *toolAccumulator) ([][]byte, error) {
	if tool == nil || tool.done {
		return nil, nil
	}
	args := any(map[string]any{})
	raw := strings.TrimSpace(tool.arguments.String())
	if raw != "" {
		if err := json.Unmarshal([]byte(raw), &args); err != nil {
			return nil, fmt.Errorf("decode streamed function arguments: %w", err)
		}
	}
	tool.done = true
	return e.chunk([]any{map[string]any{"functionCall": map[string]any{
		"id": tool.id, "name": tool.name, "args": args,
	}}}, "", nil)
}

func (e *StreamEncoder) flushTools() ([][]byte, error) {
	indexes := make([]int, 0, len(e.tools))
	for index := range e.tools {
		indexes = append(indexes, index)
	}
	sort.Ints(indexes)

	frames := make([][]byte, 0)
	for _, index := range indexes {
		more, err := e.flushTool(index, e.tools[index])
		if err != nil {
			return nil, err
		}
		frames = append(frames, more...)
	}
	return frames, nil
}

func (e *StreamEncoder) chunk(parts []any, finish string, usage *protocol.Usage) ([][]byte, error) {
	object := map[string]any{"modelVersion": e.model}
	if e.responseID != "" {
		object["responseId"] = e.responseID
	}
	if parts != nil || finish != "" {
		candidate := map[string]any{"index": 0}
		if parts != nil {
			candidate["content"] = map[string]any{"role": "model", "parts": parts}
		}
		if finish != "" {
			candidate["finishReason"] = finish
		}
		object["candidates"] = []any{candidate}
	}
	if usage != nil {
		object["usageMetadata"] = encodeUsage(usage)
	}
	body, err := json.Marshal(object)
	if err != nil {
		return nil, err
	}
	return [][]byte{[]byte("data: " + string(body) + "\n\n")}, nil
}

func EncodeErrorEvent(code int, message, status string) []byte {
	if code == 0 {
		code = 500
	}
	if message == "" {
		message = "gateway request failed"
	}
	if status == "" {
		status = "INTERNAL"
	}
	status = strings.ToUpper(strings.ReplaceAll(status, " ", "_"))
	body, _ := json.Marshal(map[string]any{"error": map[string]any{
		"code": code, "message": message, "status": status,
	}})
	return []byte("data: " + string(bytes.TrimSpace(body)) + "\n\n")
}
