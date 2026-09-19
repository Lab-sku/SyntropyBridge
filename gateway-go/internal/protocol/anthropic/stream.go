package anthropic

import (
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"

	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/protocol"
)

type streamBlock struct {
	contentIndex int
	kind         string
	id           string
	name         string
	open         bool
}

type StreamEncoder struct {
	messageID string
	model     string
	started   bool
	stopped   bool
	nextIndex int
	text      *streamBlock
	thinking  *streamBlock
	tools     map[int]*streamBlock
	usage     *protocol.Usage
}

func NewStreamEncoder(requestID, model string) *StreamEncoder {
	return &StreamEncoder{
		messageID: normalizeMessageID(requestID),
		model:     model,
		tools:     make(map[int]*streamBlock),
	}
}

func (e *StreamEncoder) Encode(event protocol.StreamEvent) ([][]byte, error) {
	if e == nil {
		return nil, errors.New("encode Anthropic stream: encoder is nil")
	}
	if e.stopped || event.Type == protocol.StreamEventWireChunk {
		return nil, nil
	}
	if event.ResponseID != "" && !e.started {
		e.messageID = normalizeMessageID(event.ResponseID)
	}

	frames := make([][]byte, 0, 6)
	if !e.started {
		frame, err := e.eventFrame("message_start", map[string]any{
			"type": "message_start",
			"message": map[string]any{
				"id": e.messageID, "type": "message", "role": "assistant", "model": e.model,
				"content": []any{}, "stop_reason": nil, "stop_sequence": nil,
				"usage": map[string]any{"input_tokens": int64(0), "output_tokens": int64(0)},
			},
		})
		if err != nil {
			return nil, err
		}
		frames = append(frames, frame)
		e.started = true
	}

	switch event.Type {
	case protocol.StreamEventResponseStart, protocol.StreamEventMessageStart:
	case protocol.StreamEventTextDelta:
		if event.TextDelta != nil {
			start, err := e.ensureTextBlock()
			if err != nil {
				return nil, err
			}
			frames = append(frames, start...)
			frame, err := e.eventFrame("content_block_delta", map[string]any{
				"type": "content_block_delta", "index": e.text.contentIndex,
				"delta": map[string]any{"type": "text_delta", "text": *event.TextDelta},
			})
			if err != nil {
				return nil, err
			}
			frames = append(frames, frame)
		}
	case protocol.StreamEventReasoningDelta:
		start, err := e.ensureThinkingBlock()
		if err != nil {
			return nil, err
		}
		frames = append(frames, start...)
		if event.ReasoningDelta != nil {
			frame, err := e.eventFrame("content_block_delta", map[string]any{
				"type": "content_block_delta", "index": e.thinking.contentIndex,
				"delta": map[string]any{"type": "thinking_delta", "thinking": *event.ReasoningDelta},
			})
			if err != nil {
				return nil, err
			}
			frames = append(frames, frame)
		}
		if raw := event.Extensions["anthropic.signature_delta"]; nonNull(raw) {
			var signature string
			if json.Unmarshal(raw, &signature) == nil && signature != "" {
				frame, err := e.eventFrame("content_block_delta", map[string]any{
					"type": "content_block_delta", "index": e.thinking.contentIndex,
					"delta": map[string]any{"type": "signature_delta", "signature": signature},
				})
				if err != nil {
					return nil, err
				}
				frames = append(frames, frame)
			}
		}
	case protocol.StreamEventToolCallStart, protocol.StreamEventToolCallDelta:
		if event.ToolCallDelta != nil {
			toolFrames, err := e.toolDelta(event.ToolCallDelta)
			if err != nil {
				return nil, err
			}
			frames = append(frames, toolFrames...)
		}
	case protocol.StreamEventToolCallEnd:
		if event.ToolCallDelta != nil {
			if block := e.tools[event.ToolCallDelta.Index]; block != nil && block.open {
				frame, err := e.stopBlock(block)
				if err != nil {
					return nil, err
				}
				frames = append(frames, frame)
			}
		}
	case protocol.StreamEventUsage:
		if event.Usage != nil {
			copyUsage := *event.Usage
			e.usage = &copyUsage
		}
	case protocol.StreamEventMessageEnd:
		done, err := e.closeOpenBlocks()
		if err != nil {
			return nil, err
		}
		frames = append(frames, done...)
		stopReason := "end_turn"
		if event.FinishReason != nil {
			stopReason = stopReasonString(*event.FinishReason)
		}
		outputTokens := int64(0)
		if e.usage != nil {
			outputTokens = e.usage.OutputTokens
		}
		frame, err := e.eventFrame("message_delta", map[string]any{
			"type":  "message_delta",
			"delta": map[string]any{"stop_reason": stopReason, "stop_sequence": nil},
			"usage": map[string]any{"output_tokens": outputTokens},
		})
		if err != nil {
			return nil, err
		}
		frames = append(frames, frame)
	case protocol.StreamEventResponseEnd:
		done, err := e.closeOpenBlocks()
		if err != nil {
			return nil, err
		}
		frames = append(frames, done...)
		frame, err := e.eventFrame("message_stop", map[string]any{"type": "message_stop"})
		if err != nil {
			return nil, err
		}
		frames = append(frames, frame)
		e.stopped = true
	case protocol.StreamEventError:
		if event.Error != nil {
			frames = append(frames, EncodeErrorEvent(event.Error.Code, event.Error.Message))
		}
	}
	return frames, nil
}

func (e *StreamEncoder) ensureTextBlock() ([][]byte, error) {
	if e.text != nil {
		return nil, nil
	}
	e.text = &streamBlock{contentIndex: e.nextIndex, kind: "text", open: true}
	e.nextIndex++
	frame, err := e.eventFrame("content_block_start", map[string]any{
		"type": "content_block_start", "index": e.text.contentIndex,
		"content_block": map[string]any{"type": "text", "text": ""},
	})
	if err != nil {
		return nil, err
	}
	return [][]byte{frame}, nil
}

func (e *StreamEncoder) ensureThinkingBlock() ([][]byte, error) {
	if e.thinking != nil {
		return nil, nil
	}
	e.thinking = &streamBlock{contentIndex: e.nextIndex, kind: "thinking", open: true}
	e.nextIndex++
	frame, err := e.eventFrame("content_block_start", map[string]any{
		"type": "content_block_start", "index": e.thinking.contentIndex,
		"content_block": map[string]any{"type": "thinking", "thinking": ""},
	})
	if err != nil {
		return nil, err
	}
	return [][]byte{frame}, nil
}

func (e *StreamEncoder) toolDelta(delta *protocol.ToolCallDelta) ([][]byte, error) {
	block := e.tools[delta.Index]
	frames := make([][]byte, 0, 2)
	if block == nil {
		id := delta.ID
		if id == "" {
			id = fmt.Sprintf("toolu_%s_%d", strings.TrimPrefix(e.messageID, "msg_"), delta.Index)
		}
		block = &streamBlock{
			contentIndex: e.nextIndex, kind: "tool_use", id: id, name: delta.Name, open: true,
		}
		e.nextIndex++
		e.tools[delta.Index] = block
		frame, err := e.eventFrame("content_block_start", map[string]any{
			"type": "content_block_start", "index": block.contentIndex,
			"content_block": map[string]any{
				"type": "tool_use", "id": block.id, "name": block.name, "input": map[string]any{},
			},
		})
		if err != nil {
			return nil, err
		}
		frames = append(frames, frame)
	}
	if delta.ID != "" {
		block.id = delta.ID
	}
	if delta.Name != "" {
		block.name = delta.Name
	}
	if delta.ArgumentsFragment != "" {
		frame, err := e.eventFrame("content_block_delta", map[string]any{
			"type": "content_block_delta", "index": block.contentIndex,
			"delta": map[string]any{"type": "input_json_delta", "partial_json": delta.ArgumentsFragment},
		})
		if err != nil {
			return nil, err
		}
		frames = append(frames, frame)
	}
	return frames, nil
}

func (e *StreamEncoder) closeOpenBlocks() ([][]byte, error) {
	frames := make([][]byte, 0, len(e.tools)+2)
	if e.text != nil && e.text.open {
		frame, err := e.stopBlock(e.text)
		if err != nil {
			return nil, err
		}
		frames = append(frames, frame)
	}
	if e.thinking != nil && e.thinking.open {
		frame, err := e.stopBlock(e.thinking)
		if err != nil {
			return nil, err
		}
		frames = append(frames, frame)
	}
	indexes := make([]int, 0, len(e.tools))
	for index := range e.tools {
		indexes = append(indexes, index)
	}
	sort.Ints(indexes)
	for _, index := range indexes {
		block := e.tools[index]
		if !block.open {
			continue
		}
		frame, err := e.stopBlock(block)
		if err != nil {
			return nil, err
		}
		frames = append(frames, frame)
	}
	return frames, nil
}

func (e *StreamEncoder) stopBlock(block *streamBlock) ([]byte, error) {
	block.open = false
	return e.eventFrame("content_block_stop", map[string]any{
		"type": "content_block_stop", "index": block.contentIndex,
	})
}

func (e *StreamEncoder) eventFrame(name string, payload map[string]any) ([]byte, error) {
	body, err := json.Marshal(payload)
	if err != nil {
		return nil, err
	}
	return []byte("event: " + name + "\ndata: " + string(body) + "\n\n"), nil
}

func EncodeErrorEvent(errorType, message string) []byte {
	if errorType == "" {
		errorType = "api_error"
	}
	if message == "" {
		message = "gateway request failed"
	}
	body, _ := json.Marshal(map[string]any{
		"type":  "error",
		"error": map[string]any{"type": errorType, "message": message},
	})
	return []byte("event: error\ndata: " + string(body) + "\n\n")
}

func stopReasonString(reason string) string {
	value := anthropicStopReason(reason)
	if text, ok := value.(string); ok {
		return text
	}
	return "end_turn"
}
