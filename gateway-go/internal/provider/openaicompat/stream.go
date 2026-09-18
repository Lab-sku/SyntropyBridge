package openaicompat

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"

	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/protocol"
)

const maxSSEEventBytes = 4 << 20

func (*Adapter) DecodeStream(
	ctx context.Context,
	resp *http.Response,
	emit func(protocol.StreamEvent) error,
) error {
	if resp == nil {
		return errors.New("decode OpenAI-compatible stream: response is nil")
	}
	if emit == nil {
		return errors.New("decode OpenAI-compatible stream: emit is nil")
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		body, err := readLimited(resp.Body, maxResponseBodyBytes)
		if err != nil {
			return err
		}
		return New().NormalizeError(ctx, resp, body)
	}

	reader := bufio.NewReaderSize(resp.Body, 64<<10)
	sequence := uint64(0)
	sourceSequence := uint64(0)
	started := false
	toolStarted := make(map[int]bool)

	emitEvent := func(event protocol.StreamEvent) error {
		sequence++
		event.Sequence = sequence
		if event.SourceSequence == 0 {
			event.SourceSequence = sourceSequence
		}
		return emit(event)
	}

	for {
		if err := ctx.Err(); err != nil {
			return err
		}
		data, err := readSSEData(reader, maxSSEEventBytes)
		if err != nil {
			if errors.Is(err, io.EOF) {
				if started {
					sourceSequence++
					return emitEvent(protocol.StreamEvent{
						Type:           protocol.StreamEventResponseEnd,
						SourceSequence: sourceSequence,
					})
				}
				return nil
			}
			return fmt.Errorf("read OpenAI-compatible stream: %w", err)
		}
		if len(data) == 0 {
			continue
		}
		sourceSequence++
		if bytes.Equal(bytes.TrimSpace(data), []byte("[DONE]")) {
			return emitEvent(protocol.StreamEvent{
				Type:           protocol.StreamEventResponseEnd,
				SourceSequence: sourceSequence,
			})
		}

		var chunk chatChunk
		if err := json.Unmarshal(data, &chunk); err != nil {
			return fmt.Errorf("decode OpenAI-compatible stream JSON: %w", err)
		}
		if chunk.Error != nil {
			return &protocol.GatewayError{
				Code:         valueOrDefault(chunk.Error.Code, "upstream_error"),
				Message:      chunk.Error.Message,
				ProviderCode: chunk.Error.Type,
				Retryable:    false,
				Raw:          append(json.RawMessage(nil), data...),
			}
		}
		if !started {
			started = true
			if err := emitEvent(protocol.StreamEvent{
				Type:       protocol.StreamEventResponseStart,
				ResponseID: chunk.ID,
				Model:      chunk.Model,
			}); err != nil {
				return err
			}
		}
		if err := emitEvent(protocol.StreamEvent{
			Type:       protocol.StreamEventWireChunk,
			ResponseID: chunk.ID,
			Model:      chunk.Model,
			Raw:        append(json.RawMessage(nil), data...),
		}); err != nil {
			return err
		}

		for _, choice := range chunk.Choices {
			if choice.Delta.Role != "" {
				if err := emitEvent(protocol.StreamEvent{
					Type:       protocol.StreamEventMessageStart,
					ResponseID: chunk.ID,
					Model:      chunk.Model,
				}); err != nil {
					return err
				}
			}
			if choice.Delta.Content != nil {
				text := *choice.Delta.Content
				if err := emitEvent(protocol.StreamEvent{
					Type:       protocol.StreamEventTextDelta,
					ResponseID: chunk.ID,
					Model:      chunk.Model,
					TextDelta:  &text,
				}); err != nil {
					return err
				}
			}
			if reasoning := choice.Delta.reasoningDelta(); reasoning != nil {
				if err := emitEvent(protocol.StreamEvent{
					Type:           protocol.StreamEventReasoningDelta,
					ResponseID:     chunk.ID,
					Model:          chunk.Model,
					ReasoningDelta: reasoning,
				}); err != nil {
					return err
				}
			}
			for _, tool := range choice.Delta.ToolCalls {
				eventType := protocol.StreamEventToolCallDelta
				if !toolStarted[tool.Index] {
					toolStarted[tool.Index] = true
					eventType = protocol.StreamEventToolCallStart
				}
				delta := &protocol.ToolCallDelta{
					Index:             tool.Index,
					ID:                tool.ID,
					Type:              tool.Type,
					Name:              tool.Function.Name,
					ArgumentsFragment: tool.Function.Arguments,
				}
				if err := emitEvent(protocol.StreamEvent{
					Type:          eventType,
					ResponseID:    chunk.ID,
					Model:         chunk.Model,
					ToolCallDelta: delta,
				}); err != nil {
					return err
				}
			}
			if choice.FinishReason != nil {
				finish := *choice.FinishReason
				if err := emitEvent(protocol.StreamEvent{
					Type:         protocol.StreamEventMessageEnd,
					ResponseID:   chunk.ID,
					Model:        chunk.Model,
					FinishReason: &finish,
				}); err != nil {
					return err
				}
			}
		}
		if chunk.Usage != nil {
			if err := emitEvent(protocol.StreamEvent{
				Type:       protocol.StreamEventUsage,
				ResponseID: chunk.ID,
				Model:      chunk.Model,
				Usage:      chunk.Usage.canonical(),
			}); err != nil {
				return err
			}
		}
	}
}

func readSSEData(reader *bufio.Reader, limit int) ([]byte, error) {
	var data bytes.Buffer
	seenLine := false
	for {
		line, err := reader.ReadString('\n')
		if err != nil && len(line) == 0 {
			if errors.Is(err, io.EOF) && seenLine {
				return data.Bytes(), nil
			}
			return nil, err
		}
		seenLine = true
		line = strings.TrimSuffix(line, "\n")
		line = strings.TrimSuffix(line, "\r")
		if line == "" {
			return data.Bytes(), nil
		}
		if strings.HasPrefix(line, ":") {
			if err != nil {
				return data.Bytes(), nil
			}
			continue
		}
		if strings.HasPrefix(line, "data:") {
			value := strings.TrimPrefix(line, "data:")
			value = strings.TrimPrefix(value, " ")
			if data.Len() > 0 {
				data.WriteByte('\n')
			}
			data.WriteString(value)
			if data.Len() > limit {
				return nil, fmt.Errorf("SSE event exceeds %d bytes", limit)
			}
		}
		if err != nil {
			return data.Bytes(), nil
		}
	}
}

func valueOrDefault(value, fallback string) string {
	if value == "" {
		return fallback
	}
	return value
}
