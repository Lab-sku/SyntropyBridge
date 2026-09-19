package gemini

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

func (a *Adapter) DecodeStream(ctx context.Context, resp *http.Response, emit func(protocol.StreamEvent) error) error {
	if resp == nil {
		return errors.New("decode Gemini stream: response is nil")
	}
	if emit == nil {
		return errors.New("decode Gemini stream: emit is nil")
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		body, err := readLimited(resp.Body, maxResponseBodyBytes)
		if err != nil {
			return err
		}
		return a.NormalizeError(ctx, resp, body)
	}

	reader := bufio.NewReaderSize(resp.Body, 64<<10)
	var sequence uint64
	var sourceSequence uint64
	started := false
	messageStarted := false
	messageEnded := false
	var latestUsage *protocol.Usage
	var responseID string
	var model string

	emitEvent := func(event protocol.StreamEvent) error {
		sequence++
		event.Sequence = sequence
		event.SourceSequence = sourceSequence
		if event.ResponseID == "" {
			event.ResponseID = responseID
		}
		if event.Model == "" {
			event.Model = model
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
					if !messageEnded {
						if err := emitEvent(protocol.StreamEvent{Type: protocol.StreamEventMessageEnd}); err != nil {
							return err
						}
					}
					return emitEvent(protocol.StreamEvent{Type: protocol.StreamEventResponseEnd})
				}
				return nil
			}
			return fmt.Errorf("read Gemini stream: %w", err)
		}
		if len(data) == 0 {
			continue
		}
		sourceSequence++

		var envelope map[string]json.RawMessage
		if err := json.Unmarshal(data, &envelope); err != nil {
			return fmt.Errorf("decode Gemini stream JSON: %w", err)
		}
		if id := jsonString(envelope["responseId"]); id != "" {
			responseID = id
		}
		if value := jsonString(envelope["modelVersion"]); value != "" {
			model = value
		}
		if !started {
			started = true
			if err := emitEvent(protocol.StreamEvent{Type: protocol.StreamEventResponseStart, Raw: append(json.RawMessage(nil), data...)}); err != nil {
				return err
			}
		}
		if !messageStarted {
			messageStarted = true
			if err := emitEvent(protocol.StreamEvent{Type: protocol.StreamEventMessageStart, Raw: append(json.RawMessage(nil), data...)}); err != nil {
				return err
			}
		}

		var candidates []json.RawMessage
		_ = json.Unmarshal(envelope["candidates"], &candidates)
		for _, rawCandidate := range candidates {
			var candidate map[string]json.RawMessage
			if json.Unmarshal(rawCandidate, &candidate) != nil {
				continue
			}
			var content map[string]json.RawMessage
			_ = json.Unmarshal(candidate["content"], &content)
			var parts []json.RawMessage
			_ = json.Unmarshal(content["parts"], &parts)
			for index, rawPart := range parts {
				var part map[string]json.RawMessage
				if json.Unmarshal(rawPart, &part) != nil {
					continue
				}
				if rawText, ok := part["text"]; ok {
					text := jsonString(rawText)
					var thought bool
					_ = json.Unmarshal(part["thought"], &thought)
					if thought {
						if err := emitEvent(protocol.StreamEvent{Type: protocol.StreamEventReasoningDelta, ReasoningDelta: &text, Raw: append(json.RawMessage(nil), data...)}); err != nil {
							return err
						}
					} else if text != "" {
						if err := emitEvent(protocol.StreamEvent{Type: protocol.StreamEventTextDelta, TextDelta: &text, Raw: append(json.RawMessage(nil), data...)}); err != nil {
							return err
						}
					}
				}
				if rawCall, ok := part["functionCall"]; ok {
					var call map[string]json.RawMessage
					if json.Unmarshal(rawCall, &call) == nil {
						args := call["args"]
						fragment := ""
						if len(bytes.TrimSpace(args)) > 0 {
							fragment = string(args)
						}
						delta := &protocol.ToolCallDelta{
							Index: index, ID: jsonString(call["id"]), Type: "function",
							Name: jsonString(call["name"]), ArgumentsFragment: fragment,
						}
						if err := emitEvent(protocol.StreamEvent{Type: protocol.StreamEventToolCallStart, ToolCallDelta: delta, Raw: append(json.RawMessage(nil), data...)}); err != nil {
							return err
						}
						if err := emitEvent(protocol.StreamEvent{Type: protocol.StreamEventToolCallEnd, ToolCallDelta: delta, Raw: append(json.RawMessage(nil), data...)}); err != nil {
							return err
						}
					}
				}
			}
			finish := normalizeFinishReason(jsonString(candidate["finishReason"]))
			if finish != "" && !messageEnded {
				messageEnded = true
				if err := emitEvent(protocol.StreamEvent{Type: protocol.StreamEventMessageEnd, FinishReason: &finish, Raw: append(json.RawMessage(nil), data...)}); err != nil {
					return err
				}
			}
		}

		var usage usageMetadata
		if json.Unmarshal(envelope["usageMetadata"], &usage) == nil && (usage.TotalTokenCount != 0 || usage.PromptTokenCount != 0 || usage.CandidatesTokenCount != 0) {
			latestUsage = usage.canonical()
			if err := emitEvent(protocol.StreamEvent{Type: protocol.StreamEventUsage, Usage: latestUsage, Raw: append(json.RawMessage(nil), data...)}); err != nil {
				return err
			}
		}
		if err := emitEvent(protocol.StreamEvent{Type: protocol.StreamEventWireChunk, Raw: append(json.RawMessage(nil), data...)}); err != nil {
			return err
		}
	}
}

func readSSEData(reader *bufio.Reader, limit int) ([]byte, error) {
	var data bytes.Buffer
	seen := false
	for {
		line, err := reader.ReadString('\n')
		if err != nil && len(line) == 0 {
			if errors.Is(err, io.EOF) && seen {
				return data.Bytes(), nil
			}
			return nil, err
		}
		seen = true
		line = strings.TrimSuffix(strings.TrimSuffix(line, "\n"), "\r")
		if line == "" {
			return data.Bytes(), nil
		}
		if strings.HasPrefix(line, "data:") {
			value := strings.TrimSpace(strings.TrimPrefix(line, "data:"))
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
