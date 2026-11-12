/*
Copyright 2026 Google LLC

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

// PlatformAgent status.usage, as the console reads it: the operator's
// counters, readings, proposal origins, token and spend totals since install,
// the broker's top tools and the two activity timestamps.
//
// GET /api/environment carries the block for the feedback modal, so a bug
// report drafted here says the same about the install as "/feedback summary"
// in chat, and GET /api/insights reads the three totals so the banner's
// token and spend figures survive a LiteLLM restart. The field lists below are
// a copy of a2a/gateway/feedback_summary.go's, and
// tests/test_web_console_usage_parity.py holds the two equal, as the gateway's
// own pin test holds it to agents/platform/scripts/feedback_summary.py. The
// decoder copies only the fields named here off the status: a field the
// operator adds later reaches the response once it is added to a list, never
// before, so a status field that one day holds a name cannot leak into a
// report. The render is the gateway block's usage, tools and performance
// lines, verbatim, less the latency note that points at the workstation
// script.

package main

import (
	"fmt"
	"sort"
	"strconv"
	"strings"
)

const (
	// status.usage's three inference totals, in the order the gateway's
	// render reads them; the spend is in micro-dollars (usageMicrosPerUSD).
	usageInputTokensField  = "inputTokensTotal"
	usageOutputTokensField = "outputTokensTotal"
	usageSpendMicrosField  = "spendMicrosUSDTotal"
	usageMicrosPerUSD      = 1_000_000.0

	// status.usage.topTools, the broker's five most-invoked tool and
	// subcommand pairs since its pod started, and the row fields it carries.
	usageTopToolsKey         = "topTools"
	usageTopToolsShown       = 5
	usageToolField           = "tool"
	usageToolSubcommandField = "subcommand"
	usageToolTotalField      = "total"
	// A bare tool's subcommand, printed as the tool alone; the pair's
	// separator otherwise.
	usageToolSubcommandNone = "none"
	usageToolPairSeparator  = "/"
	// The tools block's label, the gateway's line verbatim.
	usageTopToolsLabel = "tools (top 5, since broker start)"

	// The timestamps and the interface list the block reads by name.
	usageLastActiveField       = "lastActiveTime"
	usageLastHumanActiveField  = "lastHumanActiveTime"
	usageActiveInterfacesField = "activeInterfaces"

	// The block's words, the gateway's verbatim.
	usageHeader               = "usage:"
	usagePerformanceHeader    = "performance:"
	usageInferenceLabel       = "  tokens (since install): " // named without the word gosec G101 reads as a credential
	usageSpendLabel           = "  spend (since install): "
	usageSpendCurrency        = "USD"
	usageOriginLinePrefix     = "  proposals by origin: "
	usageOriginFieldPrefix    = "proposalsFrom"
	usageLastActiveLabel      = "  lastActive: "
	usageLastHumanActiveLabel = "  lastHumanActive: "
	usageNoReading            = "no reading"
	usageNever                = "never"
	usageNone                 = "none"
	usageUnavailable          = "unavailable"
	usageForgeReadingSuffix   = " (as the forge reports)"
	usageGatewayReadingSuffix = " (as the gateway reports)"
	usageAgentReadingSuffix   = " (as the agent reports)"

	// What GET /api/insights says the banner's headline figures are:
	// the operator's totals since install off status.usage, or the sum of
	// the LiteLLM replicas' counters since each last restarted.
	usageSourcePlatformAgent = "platformagent"
	usageSourceLiteLLM       = "litellm"

	// What /api/environment says when the status carries no usage block:
	// the served CRD predates it, or the operator has not written it yet.
	usageAbsentReason = "the PlatformAgent status carries no usage block"
)

// usageCounters are the status.usage fields the block prints in this order;
// a missing counter prints 0, a missing reading (usageReadings) prints
// usageNoReading. The gateway's usageSummaryCounters verbatim.
var usageCounters = []string{
	"clustersRegistered", "clustersMonitored", "chatMessagesInboundTotal", "sessionsTotal",
	"chatSessionsTotal", "toolExecutionsTotal", "toolExecutionsBlockedTotal", "brokerFailuresTotal",
	"eventsIngestedTotal", "chatTurnsErroredTotal", "backgroundTasksGaveUpTotal", "negativeFeedbackTotal",
	"remediationsProposedTotal", "remediationsMergedTotal", "proposalsAuthored", "remediationsOpen",
	"remediationsClosedUnmergedTotal", "proposalHumanTouches", "proposalsMergedUntouched",
	"peopleSeen7d", "peopleSeen30d",
}

// usageReadings are the counters read off a source rather than counted, each
// to the suffix naming that source: absent means no reading yet, which is
// not 0. The gateway's usageSummaryReadings verbatim.
var usageReadings = map[string]string{
	"remediationsMergedTotal":         usageForgeReadingSuffix,
	"proposalsAuthored":               usageForgeReadingSuffix,
	"remediationsOpen":                usageForgeReadingSuffix,
	"remediationsClosedUnmergedTotal": usageForgeReadingSuffix,
	"proposalHumanTouches":            usageForgeReadingSuffix,
	"proposalsMergedUntouched":        usageForgeReadingSuffix,
	"peopleSeen7d":                    usageGatewayReadingSuffix,
	"peopleSeen30d":                   usageGatewayReadingSuffix,
}

// usageOriginFields are the nine proposal-origin readings, in the origin
// table's order; the block prints them as one line. The gateway's
// usageSummaryOriginFields verbatim.
var usageOriginFields = []string{
	"proposalsFromChat", "proposalsFromConsole", "proposalsFromApi", "proposalsFromEvent", "proposalsFromDrift",
	"proposalsFromAudit", "proposalsFromScheduled", "proposalsFromIssue", "proposalsFromOther",
}

// usageToolStatusOrder is the broker's outcome vocabulary in the order a
// tools row prints its breakdown, and the row's field names. The gateway's
// usageSummaryToolStatusOrder verbatim.
var usageToolStatusOrder = []string{"success", "error", "blocked", "busy", "abandoned"}

// usageTotalFields are the three totals, in the gateway's render order.
var usageTotalFields = []string{usageInputTokensField, usageOutputTokensField, usageSpendMicrosField}

// usageTimestampFields are the two activity timestamps the block prints.
var usageTimestampFields = []string{usageLastActiveField, usageLastHumanActiveField}

// usageStatus is status.usage as the console carries it.
type usageStatus struct {
	// Fields holds the allowlisted status.usage fields as the status spells
	// them: integers, the two RFC 3339 timestamps, the interface words and the
	// topTools rows. Nothing else is copied.
	Fields map[string]any `json:"fields"`
	// Lines is the gateway block's usage, tools and performance lines.
	Lines []string `json:"lines"`
}

// usageTotals are the three totals read off status.usage, when present.
type usageTotals struct {
	InputTokens    int64
	OutputTokens   int64
	SpendMicrosUSD int64
}

// usageAsInt reads a status number as the gateway does: the API serves
// integers, a JSON decode yields float64, and a string is a quoted number.
func usageAsInt(v any) int64 {
	switch n := v.(type) {
	case int64:
		return n
	case int:
		return int64(n)
	case float64:
		return int64(n)
	case string:
		i, _ := strconv.ParseInt(n, 10, 64)
		return i
	}
	return 0
}

func usageAsString(v any) string {
	s, _ := v.(string)
	return s
}

// decodeUsage copies the allowlisted fields off a decoded status.usage. A
// counter, reading, origin or total is kept as an integer when present; a
// timestamp as a string; activeInterfaces as its string items; topTools as
// rows of the tool, subcommand, total and the outcome fields. A key outside
// the lists, whatever it holds, is dropped. A nil status (the key was absent)
// yields nil.
func decodeUsage(raw map[string]any) map[string]any {
	if raw == nil {
		return nil
	}
	out := map[string]any{}
	for _, lists := range [][]string{usageCounters, usageOriginFields, usageTotalFields} {
		for _, field := range lists {
			if v, ok := raw[field]; ok && v != nil {
				out[field] = usageAsInt(v)
			}
		}
	}
	for _, field := range usageTimestampFields {
		if s := usageAsString(raw[field]); s != "" {
			out[field] = s
		}
	}
	if items, ok := raw[usageActiveInterfacesField].([]any); ok {
		interfaces := []string{}
		for _, item := range items {
			if s := usageAsString(item); s != "" {
				interfaces = append(interfaces, s)
			}
		}
		out[usageActiveInterfacesField] = interfaces
	}
	if items, ok := raw[usageTopToolsKey].([]any); ok {
		rows := []map[string]any{}
		for _, item := range items {
			entry, ok := item.(map[string]any)
			if !ok {
				continue
			}
			row := map[string]any{
				usageToolField:           usageAsString(entry[usageToolField]),
				usageToolSubcommandField: usageAsString(entry[usageToolSubcommandField]),
				usageToolTotalField:      usageAsInt(entry[usageToolTotalField]),
			}
			for _, status := range usageToolStatusOrder {
				if v, ok := entry[status]; ok && v != nil {
					row[status] = usageAsInt(v)
				}
			}
			rows = append(rows, row)
		}
		out[usageTopToolsKey] = rows
	}
	return out
}

// usageTotalsOf returns the three totals off a decoded status and whether any
// of them is present. The operator omits a zero, so an install that has not
// been polled yet, or has used no tokens, has none.
func usageTotalsOf(fields map[string]any) (usageTotals, bool) {
	var t usageTotals
	present := false
	for _, field := range usageTotalFields {
		if _, ok := fields[field]; ok {
			present = true
		}
	}
	if !present {
		return t, false
	}
	t.InputTokens = usageAsInt(fields[usageInputTokensField])
	t.OutputTokens = usageAsInt(fields[usageOutputTokensField])
	t.SpendMicrosUSD = usageAsInt(fields[usageSpendMicrosField])
	return t, true
}

// usageOriginLine lists the origins whose reading is above zero in the
// table's order, "none" when every reading is zero, and "no reading" when the
// readings are absent, as the gateway's line.
func usageOriginLine(fields map[string]any) string {
	var parts []string
	present := false
	for _, field := range usageOriginFields {
		value, ok := fields[field]
		if !ok {
			continue
		}
		present = true
		if n := usageAsInt(value); n != 0 {
			parts = append(parts, fmt.Sprintf("%s %d", strings.ToLower(strings.TrimPrefix(field, usageOriginFieldPrefix)), n))
		}
	}
	shown := strings.Join(parts, ", ")
	switch {
	case !present:
		shown = usageNoReading
	case shown == "":
		shown = usageNone
	}
	return usageOriginLinePrefix + shown + usageAgentReadingSuffix
}

// renderTopTools is the tools block from the decoded topTools rows, the
// gateway's lines verbatim: the label and unavailable when the status carries
// no list, the label and none for an empty one, else the label and one row
// per entry in the status's order, at most usageTopToolsShown of them: the
// tool, or tool/subcommand for a subcommand other than none, its total, and
// in parentheses each outcome above zero in usageToolStatusOrder.
func renderTopTools(fields map[string]any) []string {
	rows, ok := fields[usageTopToolsKey].([]map[string]any)
	if !ok {
		return []string{usageTopToolsLabel + ": " + usageUnavailable}
	}
	var lines []string
	for _, entry := range rows {
		if len(lines) >= usageTopToolsShown {
			break
		}
		pair := usageAsString(entry[usageToolField])
		if subcommand := usageAsString(entry[usageToolSubcommandField]); subcommand != usageToolSubcommandNone {
			pair += usageToolPairSeparator + subcommand
		}
		var parts []string
		for _, status := range usageToolStatusOrder {
			if n := usageAsInt(entry[status]); n > 0 {
				parts = append(parts, fmt.Sprintf("%s %d", status, n))
			}
		}
		line := fmt.Sprintf("  %s: %d", pair, usageAsInt(entry[usageToolTotalField]))
		if len(parts) > 0 {
			line += " (" + strings.Join(parts, ", ") + ")"
		}
		lines = append(lines, line)
	}
	if len(lines) == 0 {
		return []string{usageTopToolsLabel + ": " + usageNone}
	}
	return append([]string{usageTopToolsLabel + ":"}, lines...)
}

// renderUsageLines prints the decoded status as the gateway block's usage,
// tools and performance sections.
func renderUsageLines(fields map[string]any) []string {
	lines := []string{usageHeader}
	for _, counter := range usageCounters {
		value, present := fields[counter]
		suffix, isReading := usageReadings[counter]
		if !isReading {
			lines = append(lines, fmt.Sprintf("  %s: %d", counter, usageAsInt(value)))
			continue
		}
		shown := usageNoReading
		if present {
			shown = strconv.FormatInt(usageAsInt(value), 10)
		}
		lines = append(lines, fmt.Sprintf("  %s: %s%s", counter, shown, suffix))
	}
	lines = append(lines, usageOriginLine(fields))
	last := usageAsString(fields[usageLastActiveField])
	if last == "" {
		last = usageNever
	}
	lastHuman := usageAsString(fields[usageLastHumanActiveField])
	if lastHuman == "" {
		lastHuman = usageNever
	}
	lines = append(lines, usageLastActiveLabel+last, usageLastHumanActiveLabel+lastHuman)
	lines = append(lines, renderTopTools(fields)...)
	totals, _ := usageTotalsOf(fields)
	lines = append(lines, usagePerformanceHeader,
		fmt.Sprintf("%sin %d / out %d", usageInferenceLabel, totals.InputTokens, totals.OutputTokens),
		fmt.Sprintf("%s%.2f %s", usageSpendLabel, float64(totals.SpendMicrosUSD)/usageMicrosPerUSD, usageSpendCurrency))
	return lines
}

// newUsageStatus decodes and renders status.usage; nil when the status
// carries no usage block.
func newUsageStatus(raw map[string]any) *usageStatus {
	fields := decodeUsage(raw)
	if fields == nil {
		return nil
	}
	if interfaces, ok := fields[usageActiveInterfacesField].([]string); ok {
		sort.Strings(interfaces)
	}
	return &usageStatus{Fields: fields, Lines: renderUsageLines(fields)}
}
