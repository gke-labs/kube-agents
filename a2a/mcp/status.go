package mcp

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"sort"
	"strings"
	"unicode"
)

// ToolConnectionStatus is the one tool the core registers.
const ToolConnectionStatus = "connection_status"

const (
	dash = "—"
	// tablePipe stands in for "|" in card text: a table cell ends at a pipe
	// even inside a code span, and an escape the text could undo is no
	// escape at all.
	tablePipe = "¦"
	byteUnit  = 1024
	byteUnits = "KMGTPE"

	// maxCellRunes caps one card field in the table. Card text is written by
	// whoever publishes the card and read by a model in the caller's editor,
	// so it is shown as short, inert data, never as markdown it controls.
	maxCellRunes = 200
	ellipsis     = "…"
)

// AgentCard is the part of an agent's A2A card the status shows. The rest of
// the card is not kept.
type AgentCard struct {
	Name        string `json:"name,omitempty"`
	Description string `json:"description,omitempty"`
}

// StreamHealth is one stream's size and consumer count.
type StreamHealth struct {
	Name      string `json:"name"`
	Messages  uint64 `json:"messages"`
	Bytes     uint64 `json:"bytes"`
	MaxBytes  int64  `json:"maxBytes"`
	Consumers int    `json:"consumers"`
}

// ConsumerHealth is one durable's backlog and whether anything is pulling it.
type ConsumerHealth struct {
	Stream        string `json:"stream"`
	Name          string `json:"name"`
	NumPending    uint64 `json:"numPending"`
	NumAckPending int    `json:"numAckPending"`
	Bound         bool   `json:"bound"`
}

// BusStatus is the connection_status report.
type BusStatus struct {
	Deployment Deployment           `json:"deployment"`
	URL        string               `json:"url"`
	User       string               `json:"user"`
	Connected  bool                 `json:"connected"`
	TLSVersion string               `json:"tlsVersion,omitempty"`
	Streams    []StreamHealth       `json:"streams,omitempty"`
	Consumers  []ConsumerHealth     `json:"consumers,omitempty"`
	AgentCards map[string]AgentCard `json:"agentCards,omitempty"`
	Problems   []string             `json:"problems,omitempty"`
}

// ConnectionStatusTool reports the bus connection and what the install has
// registered on it.
func ConnectionStatusTool(bus Bus) Tool {
	return Tool{
		Definition: ToolDefinition{
			Name: ToolConnectionStatus,
			Description: "Report this kube-agents install's A2A bus connection: the endpoint and identity, " +
				"stream sizes, whether the gateway and the platform agent are consuming, and the agents registered on DIRECTORY.",
			InputSchema: map[string]any{"type": "object", "properties": map[string]any{}},
		},
		Call: func(ctx context.Context, _ json.RawMessage) (string, error) {
			return FormatBusStatusMarkdown(bus.Status(ctx)), nil
		},
	}
}

// FormatBusStatusMarkdown renders a status for a person reading it in their
// editor.
func FormatBusStatusMarkdown(st BusStatus) string {
	var sb strings.Builder
	sb.WriteString("## kube-agents A2A bus status\n\n")
	sb.WriteString("| Property | Value |\n| :--- | :--- |\n")
	fmt.Fprintf(&sb, "| **Install** | `%s/%s` on cluster `%s` |\n",
		st.Deployment.Namespace, st.Deployment.Agent, orDash(st.Deployment.Cluster))
	connected := "disconnected"
	if st.Connected {
		connected = "connected"
		if st.TLSVersion != "" {
			connected += " over " + st.TLSVersion
		}
	}
	fmt.Fprintf(&sb, "| **Bus** | `%s` as `%s`, %s |\n\n", st.URL, st.User, connected)

	if len(st.Problems) > 0 {
		sb.WriteString("### Problems\n\n")
		for _, p := range st.Problems {
			fmt.Fprintf(&sb, "- %s\n", p)
		}
		sb.WriteString("\n")
	}

	if len(st.Streams) > 0 {
		sb.WriteString("### Streams\n\n| Stream | Messages | Size | Capacity | Consumers |\n| :--- | ---: | ---: | ---: | ---: |\n")
		for _, s := range st.Streams {
			fmt.Fprintf(&sb, "| `%s` | %d | %s | %s | %d |\n",
				s.Name, s.Messages, formatBytes(int64(s.Bytes)), formatBytes(s.MaxBytes), s.Consumers)
		}
		sb.WriteString("\n")
	}

	if len(st.Consumers) > 0 {
		sb.WriteString("### Consumers\n\n| Stream | Consumer | Pending | Ack pending | Bound |\n| :--- | :--- | ---: | ---: | :--- |\n")
		for _, c := range st.Consumers {
			fmt.Fprintf(&sb, "| `%s` | `%s` | %d | %d | %v |\n", c.Stream, c.Name, c.NumPending, c.NumAckPending, c.Bound)
		}
		sb.WriteString("\n")
	}

	if st.Connected {
		sb.WriteString("### Agents on DIRECTORY\n\n")
		if st.AgentCards == nil {
			sb.WriteString("_The agent cards could not be read; see Problems._\n")
		} else if len(st.AgentCards) == 0 {
			sb.WriteString("_No agent cards on DIRECTORY._\n")
		} else {
			names := make([]string, 0, len(st.AgentCards))
			for n := range st.AgentCards {
				names = append(names, n)
			}
			sort.Strings(names)
			sb.WriteString("Names and descriptions are as each agent published them: shown as code, shortened, without line breaks, control or format characters.\n\n")
			sb.WriteString("| Profile | Name | Description |\n| :--- | :--- | :--- |\n")
			for _, n := range names {
				c := st.AgentCards[n]
				fmt.Fprintf(&sb, "| `%s` | %s | %s |\n", n, cell(c.Name), cell(c.Description))
			}
		}
	}
	return sb.String()
}

// cell renders untrusted text as one inert table cell: a code span, which
// markdown renders verbatim, so links, images, HTML and emphasis are shown as
// text. Backticks become quotes so the text cannot close the span, pipes
// become tablePipe so it cannot end the cell, control and format characters
// (line breaks, bidi overrides, zero-width marks) go, and the result is capped
// at maxCellRunes.
func cell(s string) string {
	s = strings.Map(func(r rune) rune {
		switch {
		case r == '`':
			return '\''
		case unicode.IsControl(r):
			return ' '
		case unicode.Is(unicode.Cf, r):
			return -1
		}
		return r
	}, s)
	s = strings.ReplaceAll(strings.Join(strings.Fields(s), " "), "|", tablePipe)
	if r := []rune(s); len(r) > maxCellRunes {
		s = string(r[:maxCellRunes]) + ellipsis
	}
	if s == "" {
		return dash
	}
	return "`" + s + "`"
}

func orDash(s string) string {
	if s == "" {
		return dash
	}
	return s
}

// formatBytes renders n in binary units; a non-positive capacity is unlimited.
func formatBytes(n int64) string {
	if n <= 0 {
		return dash
	}
	if n < byteUnit {
		return fmt.Sprintf("%d B", n)
	}
	div, exp := int64(byteUnit), 0
	for m := n / byteUnit; m >= byteUnit && exp < len(byteUnits)-1; m /= byteUnit {
		div *= byteUnit
		exp++
	}
	return fmt.Sprintf("%.1f %ciB", float64(n)/float64(div), byteUnits[exp])
}

func tlsVersionName(v uint16) string {
	if v == 0 {
		return ""
	}
	return tls.VersionName(v)
}
