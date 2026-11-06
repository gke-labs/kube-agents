package gateway

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/version"
	"k8s.io/client-go/discovery"
	"k8s.io/client-go/dynamic"
	"k8s.io/client-go/rest"
)

// "/feedback summary" (docs/designs/chat-feedback-command.md, part 7; issue
// #3428): the gateway posts the install's usage and feedback summary itself,
// deterministically, so a person in a restricted network can paste one block
// into an email or a chat message after reading it. The same sections as
// scripts/feedback-summary.sh prints from a workstation, from what the
// gateway can see: the PlatformAgent resource (one read-only grant on its
// Role) and its own task records, which count chat turns, not the kanban
// cards the script's tasks line counts; the line is labelled for that. No model is in the loop, and the block
// names no cluster, project, namespace, task or person: the audit below
// refuses a block that does, outside the model line and the proposals-by-origin
// line, which are printed by design.

const (
	usageSummaryTitle = "kube-agents usage & feedback summary"
	//: The block's line cap. The script's SUMMARY_MAX_LINES differs because each cap
	//: matches its own block's shape (the script adds the live LiteLLM figures and
	//: task rows this block does not carry); nothing holds the two in step, by design.
	usageSummaryMaxLines = 56
	hoursPerDay          = 24
	//: The task roll-up's window, as the script's; the gateway clamps it to
	//: SessionTTL, past which idle records (and their tasks) are deleted.
	usageSummaryWindowDays = 14
	usageSummaryWindow     = usageSummaryWindowDays * hoursPerDay * time.Hour
	//: The record scan runs inside the conversation's turn; it gets this much
	//: of the turn budget, after which the block says the tasks are unavailable.
	usageSummaryScanTimeout = 10 * time.Second
	//: The roll-up's buckets: what the gateway's records hold is a start, a
	//: cancel and whether the task is the conversation's running one, not the
	//: outcome, so a task that is neither cancelled nor running is "ended".
	usageBucketEnded     = "ended"
	usageBucketCancelled = "cancelled"
	usageBucketActive    = "active"
	//: One clock on every API call the summary makes: the PlatformAgent get and
	//: the /version read both run under the turn's context and this client
	//: timeout, so a server that accepts and never answers cannot hold the
	//: conversation's worker.
	usageSummaryAPITimeout = 10 * time.Second
	serverVersionPath      = "/version"
	//: The characters a block token is made of; the audit's boundary is the
	//: complement, as the script's IDENTIFIER_BOUNDARY is.
	usageSummaryTokenClass = `[A-Za-z0-9_.-]`
	//: The window never drops below one day, so the label never reads "0 days"
	//: on an install with a sub-day SessionTTL.
	usageSummaryWindowFloor = hoursPerDay * time.Hour
	//: The performance block: the token and spend totals since install, which
	//: the operator keeps on status.usage from LiteLLM's counters across its
	//: restarts, and the latency figure the script reads in-pod, which is not
	//: the gateway's to see; that line says where it is.
	usageSummaryPerformanceHeader = "performance:"
	usageSummaryTokensLabel       = "  tokens (since install): "
	usageSummarySpendLabel        = "  spend (since install): "
	usageSummaryLatencyNote       = "  latency: in scripts/feedback-summary.sh's block"
	usageSummarySpendCurrency     = "USD"
	//: status.usage's three inference totals, read in the order the script's
	//: PERFORMANCE_TOTAL_FIELDS names them, which its GatewayCopyTest holds to
	//: the render below; the spend is in micro-dollars (usageSummaryMicrosPerUSD).
	usageSummaryInputTokensField  = "inputTokensTotal"
	usageSummaryOutputTokensField = "outputTokensTotal"
	usageSummarySpendMicrosField  = "spendMicrosUSDTotal"
	usageSummaryMicrosPerUSD      = 1_000_000.0
	usageSummaryUnavailableNotice = "⚠️ The usage summary is unavailable: the PlatformAgent resource could not be read. From a workstation, scripts/feedback-summary.sh prints the workstation block."
	usageSummaryRefusedNotice     = "⚠️ The usage summary was not posted: the block would have named an identifier of this install. From a workstation, scripts/feedback-summary.sh prints the workstation block with the same check."
	//: What the roll-up counts. The script's line with the same window counts
	//: kanban cards; this one counts the gateway's task records, one per chat
	//: turn, so the label says so and a reader of both pastes is not misled.
	usageSummaryTurnsLabel     = "chat turns"
	usageSummaryFence          = "```"
	usageSummaryUnavailable    = "unavailable"
	usageSummaryUnknown        = "unknown"
	usageSummaryNever          = "never"
	usageSummaryNone           = "none"
	usageSummaryDefaultMode    = "today"
	usageSummaryReadyCondition = "Ready"
	usageSummaryMinIdentifier  = 3
	usageSummaryDateLayout     = "2006-01-02"
	platformAgentGroup         = "kubeagents.x-k8s.io"
	platformAgentVersion       = "v1alpha1"
	platformAgentResource      = "platformagents"
	//: The gateway's Deployment is <agent>-a2a-gateway (the operator's
	//: a2aGatewayName); the resource name is what precedes the suffix.
	a2aGatewayNameSuffix = "-a2a-gateway"
	platformAgentNameEnv = "PLATFORM_AGENT_NAME"
	//: The model line's start: the audit runs over the block without this line.
	usageSummaryModelLinePrefix = "  model: "
	//: The origin line's start: the audit runs over the block without this
	//: line, which carries only fixed words and digits.
	usageSummaryOriginLinePrefix = "  proposals by origin: "
	//: What precedes the origin's name in its field.
	usageSummaryOriginFieldPrefix = "proposalsFrom"
	//: status.inference, the operator's reading of the install's default model.
	inferenceStatusKey        = "inference"
	inferenceProviderField    = "provider"
	inferenceModelField       = "model"
	inferenceSeparator        = "/"
	usageNoReading            = "no reading"
	usageForgeReadingSuffix   = " (as the forge reports)"
	usageGatewayReadingSuffix = " (as the gateway reports)"
	usageAgentReadingSuffix   = " (as the agent reports)"
	//: status.usage.topTools: the operator's reading of the broker's five
	//: most-invoked tool and subcommand pairs since the broker pod started,
	//: and the row fields it carries. The script's TOP_TOOLS_* names.
	usageTopToolsKey         = "topTools"
	usageTopToolsShown       = 5
	usageToolField           = "tool"
	usageToolSubcommandField = "subcommand"
	usageToolTotalField      = "total"
	//: A bare tool's subcommand, printed as the tool alone; the pair's
	//: separator otherwise. The broker's SUBCOMMAND_NONE and the script's NONE.
	usageToolSubcommandNone = "none"
	usageToolPairSeparator  = "/"
	//: The broker's LABEL_OTHER: the tool it refused to run, and a word
	//: outside a tool's vocabulary; the script's OTHER_BUCKET.
	usageToolOther = "other"
	//: The block's label, the script's line verbatim.
	usageTopToolsLabel = "tools (top 5, since broker start)"
)

// : The broker's outcome vocabulary in the order a row prints its breakdown:
// : the script's TOOL_STATUS_ORDER verbatim, and the status row's field names.
var usageSummaryToolStatusOrder = []string{"success", "error", "blocked", "busy", "abandoned"}

// : The broker's tool vocabulary and, per tool, its subcommand vocabulary:
// : the script's BROKER_SUBCOMMANDS verbatim (GatewayCopyTest holds the two
// : equal, and the script's to the broker's). Not a filter: the operator
// : writes the status from its own copy. It is the block's vocabulary, so
// : that an install whose spec spells one of these words (a cluster named
// : "compute") is not refused for a row the broker's counter named.
var usageSummaryBrokerSubcommands = map[string][]string{
	"kubectl": {
		"annotate", "api-resources", "api-versions", "apply", "attach", "auth",
		"autoscale", "cluster-info", "config", "cordon", "cp", "create", "debug",
		"delete", "describe", "diff", "drain", "edit", "events", "exec", "explain",
		"expose", "get", "label", "logs", "patch", "port-forward", "proxy", "replace",
		"rollout", "run", "scale", "set", "taint", "top", "uncordon", "version", "wait",
	},
	"gcloud": {
		"artifacts", "asset", "auth", "billing", "components", "compute", "config",
		"container", "iam", "info", "init", "logging", "monitoring", "projects",
		"resource-manager", "services", "version",
	},
	"git": {
		"add", "am", "apply", "archive", "blame", "branch", "bundle", "cat-file",
		"checkout", "cherry-pick", "clean", "clone", "commit", "describe", "fetch",
		"init", "log", "ls-files", "ls-remote", "ls-tree", "merge", "merge-base", "mv",
		"pull", "push", "rebase", "remote", "reset", "restore", "rev-list", "rev-parse",
		"revert", "rm", "show", "sparse-checkout", "stash", "status", "submodule",
		"switch", "symbolic-ref", "tag", "update-ref", "version", "worktree",
	},
	"gh": {
		"api", "auth", "browse", "gist", "issue", "label", "pr", "project", "release",
		"repo", "run", "search", "secret", "ssh-key", "status", "variable", "version",
		"workflow",
	},
	"feedback": {"submit"},
}

// : The words of the tools block beyond what the degraded render prints:
// : the tools (the broker's own "other" included), every subcommand, the
// : outcomes and the two fallback words. The script's TEMPLATE_EXTRA_TOKENS.
var usageSummaryToolTokens = func() []string {
	tokens := []string{usageToolOther, usageToolSubcommandNone}
	for tool, subcommands := range usageSummaryBrokerSubcommands {
		tokens = append(tokens, tool)
		tokens = append(tokens, subcommands...)
	}
	tokens = append(tokens, usageSummaryToolStatusOrder...)
	return tokens
}()

// : status.usage fields, in the order the block prints them; a missing counter
// : prints 0, a missing reading (usageSummaryReadings) prints usageNoReading.
// : The script's USAGE_COUNTERS verbatim.
var usageSummaryCounters = []string{
	"clustersRegistered", "clustersMonitored", "chatMessagesInboundTotal", "sessionsTotal",
	"chatSessionsTotal", "toolExecutionsTotal", "toolExecutionsBlockedTotal", "brokerFailuresTotal",
	"eventsIngestedTotal", "chatTurnsErroredTotal", "backgroundTasksGaveUpTotal", "negativeFeedbackTotal",
	"remediationsProposedTotal", "remediationsMergedTotal", "proposalsAuthored", "remediationsOpen",
	"remediationsClosedUnmergedTotal", "proposalHumanTouches", "proposalsMergedUntouched",
	"peopleSeen7d", "peopleSeen30d",
}

// : The fields read off a source rather than counted, each to the suffix naming
// : that source: absent means no reading yet, which is not 0. The broker reads
// : the six off the forge; the operator reads the two people readings off this
// : gateway. The script's USAGE_READINGS.
var usageSummaryReadings = map[string]string{
	"remediationsMergedTotal":         usageForgeReadingSuffix,
	"proposalsAuthored":               usageForgeReadingSuffix,
	"remediationsOpen":                usageForgeReadingSuffix,
	"remediationsClosedUnmergedTotal": usageForgeReadingSuffix,
	"proposalHumanTouches":            usageForgeReadingSuffix,
	"proposalsMergedUntouched":        usageForgeReadingSuffix,
	"peopleSeen7d":                    usageGatewayReadingSuffix,
	"peopleSeen30d":                   usageGatewayReadingSuffix,
}

// : The nine status.usage proposal-origin readings, in the origin table's order
// : (docs/designs/proposal-origin.md); the block prints them as one line. The
// : script's USAGE_ORIGIN_FIELDS verbatim.
var usageSummaryOriginFields = []string{
	"proposalsFromChat", "proposalsFromConsole", "proposalsFromApi", "proposalsFromEvent", "proposalsFromDrift",
	"proposalsFromAudit", "proposalsFromScheduled", "proposalsFromIssue", "proposalsFromOther",
}

// : The lines printed by design, each removed (its first occurrence) before the audit.
var usageSummaryFixedLinePrefixes = []string{usageSummaryModelLinePrefix, usageSummaryOriginLinePrefix}

// : The environment lines, in the order the block prints them: the script's
// : ENVIRONMENT_KEYS verbatim.
var usageSummaryEnvironmentKeys = []string{"version", "mode", "model", "kubernetes", "installed", "interfaces"}

// : Spec subtrees whose string leaves name the install and must not reach the
// : block: the script's list verbatim, so the two audits read the same (chat
// : names no field of today's CRD and yields nothing). deployment is left out
// : on purpose: its env values, annotations and sidecar args are settings, and
// : one spelled "true" would collide with the block's own "ready: true" (the
// : script makes the same choice).
var usageSummaryIdentifierSubtrees = []string{"harness", "scope", "integration", "security", "chat", "telemetry", "networkPolicy"}

// : The values the operator writes to status.usage.activeInterfaces (the
// : interfaceDashboard.. constants in k8s-operator/internal/controller/
// : manifest_helpers.go): block vocabulary, so a spec string spelled like one
// : is not refused for appearing on the interfaces line.
var usageSummaryInterfaceWords = []string{"dashboard", "googlechat", "slack", "teams"}

// : Spec strings that are settings in any subtree.
var usageSummaryStopwords = map[string]bool{"true": true, "false": true, "yes": true, "no": true, "on": true, "off": true, "none": true, "null": true}

var (
	usageSummaryNumeric  = regexp.MustCompile(`^[0-9][0-9._%:-]*$`)
	usageSummaryToken    = regexp.MustCompile(usageSummaryTokenClass + `+`)
	usageSummaryBoundary = `[^` + usageSummaryTokenClass[1:]
)

var usageSummaryFeedbackTemplate = []string{
	"feedback:",
	"  Rating: [ 👍 / 👎 / 😐 ]",
	"  What worked well:",
	"  What failed or was slow:",
}

// usageSummaryTemplateTokens is the block's own vocabulary: the words of the
// degraded render and of a full one (every bucket present, every interface the
// operator can write), so that an install whose spec spells one of them is
// not refused for a line the template prints anyway.
var usageSummaryTemplateTokens = func() map[string]bool {
	var interfaces []any
	for _, word := range usageSummaryInterfaceWords {
		interfaces = append(interfaces, word)
	}
	full := map[string]any{"status": map[string]any{"usage": map[string]any{"activeInterfaces": interfaces}}}
	tokens := map[string]bool{}
	for _, text := range []string{
		renderUsageSummary(time.Unix(0, 0).UTC(), map[string]any{}, "", usageSummaryWindow, taskRollup{}, false),
		renderUsageSummary(time.Unix(0, 0).UTC(), full, "", usageSummaryWindow, taskRollup{Total: 3, Ended: 1, Cancelled: 1, Active: 1}, true),
	} {
		for _, tok := range usageSummaryToken.FindAllString(text, -1) {
			tokens[tok] = true
		}
	}
	for _, tok := range usageSummaryToolTokens {
		tokens[tok] = true
	}
	return tokens
}()

// usageSource is what the summary reads from the cluster: the PlatformAgent
// resource as unstructured content, and the server version.
type usageSource interface {
	PlatformAgent(ctx context.Context) (map[string]any, error)
	ServerVersion(ctx context.Context) (string, error)
}

// k8sUsageSource reads through the gateway's own ServiceAccount: the Role
// grants get on the one named PlatformAgent, and /version needs no grant.
type k8sUsageSource struct {
	dyn       dynamic.Interface
	disc      discovery.DiscoveryInterface
	namespace string
	name      string
}

// newK8sUsageSource builds the in-cluster reader. The resource name is
// PLATFORM_AGENT_NAME when set, else the gateway's own Deployment name less
// its suffix.
func newK8sUsageSource(cfg *Config) (*k8sUsageSource, error) {
	name := strings.TrimSpace(os.Getenv(platformAgentNameEnv))
	if name == "" {
		name = strings.TrimSuffix(cfg.OwnerDeployment, a2aGatewayNameSuffix)
	}
	if name == "" {
		return nil, fmt.Errorf("the PlatformAgent name is unknown: set %s or A2A_OWNER_DEPLOYMENT", platformAgentNameEnv)
	}
	rc, err := rest.InClusterConfig()
	if err != nil {
		return nil, err
	}
	rc.Timeout = usageSummaryAPITimeout
	dyn, err := dynamic.NewForConfig(rc)
	if err != nil {
		return nil, err
	}
	disc, err := discovery.NewDiscoveryClientForConfig(rc)
	if err != nil {
		return nil, err
	}
	return &k8sUsageSource{dyn: dyn, disc: disc, namespace: cfg.Namespace, name: name}, nil
}

func (s *k8sUsageSource) PlatformAgent(ctx context.Context) (map[string]any, error) {
	gvr := schema.GroupVersionResource{Group: platformAgentGroup, Version: platformAgentVersion, Resource: platformAgentResource}
	obj, err := s.dyn.Resource(gvr).Namespace(s.namespace).Get(ctx, s.name, metav1.GetOptions{})
	if err != nil {
		return nil, err
	}
	return obj.UnstructuredContent(), nil
}

// ServerVersion reads /version under ctx. discovery's ServerVersion() takes
// no context, and a stalled server would otherwise hold the turn past its
// deadline.
func (s *k8sUsageSource) ServerVersion(ctx context.Context) (string, error) {
	raw, err := s.disc.RESTClient().Get().AbsPath(serverVersionPath).Do(ctx).Raw()
	if err != nil {
		return "", err
	}
	var info version.Info
	if err := json.Unmarshal(raw, &info); err != nil {
		return "", err
	}
	return info.GitVersion, nil
}

// taskRollup is what the gateway's records say about the window: tasks that
// started, split into the record's running task (ActiveTask names at most one
// per conversation), tasks the gateway published a cancel for, and the rest,
// which ended. No title, requester or text.
type taskRollup struct {
	Total, Ended, Cancelled, Active int
}

// add counts one record's tasks that started inside the window.
func (r *taskRollup) add(rec *SessionRecord, cutoff time.Time) {
	for _, task := range rec.Tasks {
		if task.StartedAt.IsZero() || task.StartedAt.Before(cutoff) {
			continue
		}
		r.Total++
		switch {
		// Running is spelled as every other reader spells it: a detached
		// ActiveTask is a stop already sent, and its TaskRef carries Canceled.
		case rec.ActiveTask != nil && !rec.ActiveTask.Detached && rec.ActiveTask.TaskID == task.ID:
			r.Active++
		case task.Canceled:
			r.Cancelled++
		default:
			r.Ended++
		}
	}
}

// rollupRecords folds every record that each yields into the roll-up; the
// gateway streams the bucket through it, a test hands it a slice.
func rollupRecords(each func(func(*SessionRecord)) error, now time.Time, window time.Duration) (taskRollup, error) {
	cutoff := now.Add(-window)
	var r taskRollup
	if err := each(func(rec *SessionRecord) { r.add(rec, cutoff) }); err != nil {
		return taskRollup{}, err
	}
	return r, nil
}

// usageWindow is the roll-up's window: the script's fourteen days, or
// SessionTTL when that is shorter, since records idle past it are gone;
// never under a day, and rounded down to whole days so the label, which
// prints days, names the window the count used.
func usageWindow(sessionTTL time.Duration) time.Duration {
	switch {
	case sessionTTL <= 0 || sessionTTL >= usageSummaryWindow:
		return usageSummaryWindow
	case sessionTTL < usageSummaryWindowFloor:
		return usageSummaryWindowFloor
	}
	return time.Duration(windowDays(sessionTTL)) * hoursPerDay * time.Hour
}

func windowDays(window time.Duration) int {
	return int(window / (hoursPerDay * time.Hour))
}

func nested(m map[string]any, path ...string) any {
	var cur any = m
	for _, key := range path {
		next, ok := cur.(map[string]any)
		if !ok {
			return nil
		}
		cur = next[key]
	}
	return cur
}

func asString(v any) string {
	s, _ := v.(string)
	return s
}

func asInt(v any) int64 {
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

func installAge(cr map[string]any, now time.Time) string {
	created, err := time.Parse(time.RFC3339, asString(nested(cr, "metadata", "creationTimestamp")))
	if err != nil {
		return usageSummaryUnknown
	}
	return fmt.Sprintf("%d days", windowDays(now.Sub(created)))
}

func readyState(cr map[string]any) string {
	conditions, _ := nested(cr, "status", "conditions").([]any)
	for _, c := range conditions {
		cond, _ := c.(map[string]any)
		if asString(cond["type"]) == usageSummaryReadyCondition {
			return strings.ToLower(asString(cond["status"]))
		}
	}
	return usageSummaryUnknown
}

// inferenceModelLine is status.inference as provider/model, or unavailable when
// the key is absent or lacks either field. The gateway reads no LiteLLM config,
// so unlike the script it has nothing to fall back to.
func inferenceModelLine(cr map[string]any) string {
	inference, _ := nested(cr, "status", inferenceStatusKey).(map[string]any)
	provider, model := asString(inference[inferenceProviderField]), asString(inference[inferenceModelField])
	if provider == "" || model == "" {
		return usageSummaryUnavailable
	}
	return provider + inferenceSeparator + model
}

// usageSummaryOriginLine lists the origins whose reading is above zero in the
// table's order, "none" when every reading is zero, and "no reading" when the
// readings are absent. The operator writes all nine or none; a status carrying only
// some of them, which no operator writes, lists the present non-zero ones and
// ignores the rest.
func usageSummaryOriginLine(usage map[string]any) string {
	var parts []string
	present := false
	for _, field := range usageSummaryOriginFields {
		value, ok := usage[field]
		if !ok || value == nil {
			continue
		}
		present = true
		if n := asInt(value); n != 0 {
			parts = append(parts, fmt.Sprintf("%s %d", strings.ToLower(strings.TrimPrefix(field, usageSummaryOriginFieldPrefix)), n))
		}
	}
	shown := strings.Join(parts, ", ")
	switch {
	case !present:
		shown = usageNoReading
	case shown == "":
		shown = usageSummaryNone
	}
	return usageSummaryOriginLinePrefix + shown + usageAgentReadingSuffix
}

// renderUsageSummary is the block: the script's shape, from what the gateway sees.
func renderUsageSummary(now time.Time, cr map[string]any, serverVersion string, window time.Duration, tasks taskRollup, haveTasks bool) string {
	usage, _ := nested(cr, "status", "usage").(map[string]any)
	lines := []string{usageSummaryFence, fmt.Sprintf("%s (%s)", usageSummaryTitle, now.UTC().Format(usageSummaryDateLayout)), "environment:"}
	tag := asString(nested(cr, "spec", "deployment", "tag"))
	if tag == "" {
		tag = usageSummaryUnknown
	}
	mode := asString(nested(cr, "spec", "mode"))
	if mode == "" {
		mode = usageSummaryDefaultMode
	}
	if serverVersion == "" {
		serverVersion = usageSummaryUnavailable
	}
	var interfaces []string
	if raw, ok := usage["activeInterfaces"].([]any); ok {
		for _, item := range raw {
			interfaces = append(interfaces, asString(item))
		}
	}
	sort.Strings(interfaces)
	joined := strings.Join(interfaces, ", ")
	if joined == "" {
		joined = usageSummaryNone
	}
	environment := map[string]string{
		"version":    tag,
		"mode":       mode,
		"model":      inferenceModelLine(cr),
		"kubernetes": serverVersion,
		"installed":  fmt.Sprintf("%s; ready: %s", installAge(cr, now), readyState(cr)),
		"interfaces": joined,
	}
	for _, key := range usageSummaryEnvironmentKeys {
		lines = append(lines, "  "+key+": "+environment[key])
	}
	lines = append(lines, "usage:")
	for _, counter := range usageSummaryCounters {
		value, present := usage[counter]
		suffix, isReading := usageSummaryReadings[counter]
		if !isReading {
			lines = append(lines, fmt.Sprintf("  %s: %d", counter, asInt(value)))
			continue
		}
		shown := usageNoReading
		if present && value != nil {
			shown = strconv.FormatInt(asInt(value), 10)
		}
		lines = append(lines, fmt.Sprintf("  %s: %s%s", counter, shown, suffix))
	}
	lines = append(lines, usageSummaryOriginLine(usage))
	last := asString(usage["lastActiveTime"])
	if last == "" {
		last = usageSummaryNever
	}
	lastHuman := asString(usage["lastHumanActiveTime"])
	if lastHuman == "" {
		lastHuman = usageSummaryNever
	}
	lines = append(lines, "  lastActive: "+last, "  lastHumanActive: "+lastHuman)
	lines = append(lines, renderTopTools(usage)...)
	lines = append(lines, usageSummaryPerformanceHeader,
		fmt.Sprintf("%sin %d / out %d", usageSummaryTokensLabel, asInt(usage[usageSummaryInputTokensField]), asInt(usage[usageSummaryOutputTokensField])),
		fmt.Sprintf("%s%.2f %s", usageSummarySpendLabel, float64(asInt(usage[usageSummarySpendMicrosField]))/usageSummaryMicrosPerUSD, usageSummarySpendCurrency),
		usageSummaryLatencyNote)
	days := windowDays(window)
	if !haveTasks {
		lines = append(lines, fmt.Sprintf("%s (%d days): %s", usageSummaryTurnsLabel, days, usageSummaryUnavailable))
	} else {
		var parts []string
		for _, bucket := range []struct {
			name string
			n    int
		}{{usageBucketEnded, tasks.Ended}, {usageBucketCancelled, tasks.Cancelled}, {usageBucketActive, tasks.Active}} {
			if bucket.n > 0 {
				parts = append(parts, fmt.Sprintf("%s %d", bucket.name, bucket.n))
			}
		}
		line := fmt.Sprintf("%s (%d days): %d", usageSummaryTurnsLabel, days, tasks.Total)
		if len(parts) > 0 {
			line += " (" + strings.Join(parts, ", ") + ")"
		}
		lines = append(lines, line)
	}
	lines = append(lines, usageSummaryFeedbackTemplate...)
	lines = append(lines, usageSummaryFence)
	return strings.Join(lines, "\n")
}

// renderTopTools is the tools block from status.usage.topTools, the script's
// lines verbatim: the label and unavailable when the status carries no list
// (the operator has no current reading of the broker, or the served CRD
// predates the field), the label and none for an empty one, else the label
// and one row per entry, in the status's order and at most usageTopToolsShown
// of them: the tool, or tool/subcommand for a subcommand other than none, its
// total, and in parentheses each outcome the row carries above zero, in
// usageSummaryToolStatusOrder. The script renders the same list the same way
// when it falls back to the status.
func renderTopTools(usage map[string]any) []string {
	raw, ok := usage[usageTopToolsKey].([]any)
	if !ok {
		return []string{usageTopToolsLabel + ": " + usageSummaryUnavailable}
	}
	var rows []string
	for _, item := range raw {
		entry, ok := item.(map[string]any)
		if !ok || len(rows) >= usageTopToolsShown {
			continue
		}
		pair := asString(entry[usageToolField])
		if subcommand := asString(entry[usageToolSubcommandField]); subcommand != usageToolSubcommandNone {
			pair += usageToolPairSeparator + subcommand
		}
		var parts []string
		for _, status := range usageSummaryToolStatusOrder {
			if n := asInt(entry[status]); n > 0 {
				parts = append(parts, fmt.Sprintf("%s %d", status, n))
			}
		}
		line := fmt.Sprintf("  %s: %d", pair, asInt(entry[usageToolTotalField]))
		if len(parts) > 0 {
			line += " (" + strings.Join(parts, ", ") + ")"
		}
		rows = append(rows, line)
	}
	if len(rows) == 0 {
		return []string{usageTopToolsLabel + ": " + usageSummaryNone}
	}
	return append([]string{usageTopToolsLabel + ":"}, rows...)
}

func stringLeaves(v any, out *[]string) {
	switch x := v.(type) {
	case string:
		*out = append(*out, x)
	case map[string]any:
		for _, item := range x {
			stringLeaves(item, out)
		}
	case []any:
		for _, item := range x {
			stringLeaves(item, out)
		}
	}
}

// usageIdentifiers is what the block must not carry: the resource's name and
// namespace plus the string leaves of the naming subtrees, less settings
// (booleans, numbers, strings shorter than the floor) and the two spec strings
// the block prints by design.
func usageIdentifiers(cr map[string]any) []string {
	spec, _ := cr["spec"].(map[string]any)
	printed := map[string]bool{asString(nested(cr, "spec", "mode")): true, asString(nested(cr, "spec", "deployment", "tag")): true}
	values := []string{asString(nested(cr, "metadata", "name")), asString(nested(cr, "metadata", "namespace"))}
	for _, subtree := range usageSummaryIdentifierSubtrees {
		stringLeaves(spec[subtree], &values)
	}
	seen := map[string]bool{}
	var ids []string
	for _, v := range values {
		if len(v) < usageSummaryMinIdentifier || printed[v] || seen[v] || usageSummaryStopwords[strings.ToLower(v)] || usageSummaryNumeric.MatchString(v) {
			continue
		}
		seen[v] = true
		ids = append(ids, v)
	}
	return ids
}

// usageSummaryWithoutFixedLines is the block less its first model line and its
// first origin line, the lines printed by design: a deployment name can carry
// a spec identifier as a segment, and the origin line prints origin words a
// cluster may be named ("chat", "drift"). Neither line is the reason a block
// is refused; every other line stays audited.
func usageSummaryWithoutFixedLines(text string) string {
	lines := strings.Split(text, "\n")
	for _, prefix := range usageSummaryFixedLinePrefixes {
		for i, line := range lines {
			if strings.HasPrefix(line, prefix) {
				lines = append(lines[:i:i], lines[i+1:]...)
				break
			}
		}
	}
	return strings.Join(lines, "\n")
}

// auditUsageSummary returns the identifiers that appear in text as whole
// tokens, skipping the template's own vocabulary (a cluster named "today" is
// not told apart from the mode line, and is not refused for it).
func auditUsageSummary(text string, identifiers []string) []string {
	var leaked []string
	for _, id := range identifiers {
		if id == "" || usageSummaryTemplateTokens[id] {
			continue
		}
		pattern := regexp.MustCompile(`(?:^|` + usageSummaryBoundary + `)` + regexp.QuoteMeta(id) + `(?:` + usageSummaryBoundary + `|$)`)
		if pattern.MatchString(text) {
			leaked = append(leaked, id)
		}
	}
	return leaked
}

// feedbackSummary answers "/feedback summary" in place: the block, or one of
// the two fixed notices. Nothing about the conversation's task state changes,
// and no error text reaches chat.
func (g *Gateway) feedbackSummary(ctx context.Context, rec *SessionRecord) {
	if g.usage == nil {
		g.log.Warn("feedback summary unavailable: no usage source", "conversation", rec.Key)
		g.post(rec.Key, usageSummaryUnavailableNotice)
		return
	}
	cr, err := g.usage.PlatformAgent(ctx)
	if err != nil {
		g.log.Warn("feedback summary unavailable", "conversation", rec.Key, "err", err)
		g.post(rec.Key, usageSummaryUnavailableNotice)
		return
	}
	version, err := g.usage.ServerVersion(ctx)
	if err != nil {
		g.log.Warn("feedback summary: server version unavailable", "err", err)
		version = ""
	}
	now := time.Now()
	window := usageWindow(g.cfg.SessionTTL)
	tasks, err := g.scanTaskRollup(ctx, now, window)
	haveTasks := err == nil
	if err != nil {
		g.log.Warn("feedback summary: task records unavailable", "err", err)
	}
	text := renderUsageSummary(now, cr, version, window, tasks, haveTasks)
	if leaked := auditUsageSummary(usageSummaryWithoutFixedLines(text), usageIdentifiers(cr)); len(leaked) > 0 {
		g.log.Warn("feedback summary refused: the block named identifiers of the install", "conversation", rec.Key, "count", len(leaked))
		g.post(rec.Key, usageSummaryRefusedNotice)
		return
	}
	g.log.Info("feedback summary", "conversation", rec.Key)
	g.post(rec.Key, text)
}

// scanTaskRollup reads the bucket as the busy count does, through one KV
// watch stream (eachSessionRecord) rather than a Get per record, under its
// own budget: a large bucket costs this conversation at most
// usageSummaryScanTimeout of its turn and then reads "unavailable".
func (g *Gateway) scanTaskRollup(ctx context.Context, now time.Time, window time.Duration) (taskRollup, error) {
	ctx, cancel := context.WithTimeout(ctx, usageSummaryScanTimeout)
	defer cancel()
	return rollupRecords(func(fn func(*SessionRecord)) error { return g.reg.eachSessionRecord(ctx, fn) }, now, window)
}
