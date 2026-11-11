/*
Copyright 2026.

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

package controller

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"slices"
	"strconv"
	"strings"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	"github.com/prometheus/common/expfmt"
	"github.com/prometheus/common/model"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The bus-stream check (#4290): whether the streams and KV buckets the
// provision Job created are still on the A2A bus, read from the NATS exporter
// sidecar's metrics port, and a re-run of the Job when they are not.
//
// BusProvisioned is a provisioned-once record, so before this a bus that lost
// its streams after provisioning (server 0's data volume replaced, say) left
// the CR reading as before, with nothing re-provisioning until the completed
// Job's 24-hour TTL ran out, and nothing ever restarting the two clients that
// hold a TASKS durable. The operator has no bus identity that may read stream
// info, and the monitor port 8222 has no pod-network peer by design, so the
// read goes through the exporter, which reads 8222 over loopback inside the
// NATS pod (a2aNATSExporterContainer). The bus fence admits the operator to
// the exporter's port and nothing else (buildA2ANATSNetworkPolicy).
//
// Every scrape of the exporter makes it fetch /jsz?all from its server again,
// whose cost grows with the bus's consumer count (#5339; about 12 s of CPU at
// 2,000 consumers, where the exporter is already CPU-bound), so the poll is
// slow on purpose: every five minutes, three polls in a row before a stream is
// called missing. A lost volume is rare, and recovery in about twenty minutes
// is what it is traded for.

const (
	busStreamsPresentConditionType = "BusStreamsPresent"

	busStreamsReasonAllPresent           = "AllPresent"
	busStreamsReasonMissing              = "StreamsMissing"
	busStreamsReasonReprovisioning       = "Reprovisioning"
	busStreamsReasonDidNotRestore        = "ReprovisionDidNotRestore"
	busStreamsReasonDetectionUnavailable = "DetectionUnavailable"
	busStreamsReasonExporterUnreachable  = "ExporterUnreachable"

	// conditionReasonA2ABusStreamsMissing is Degraded's reason while
	// BusStreamsPresent is False.
	conditionReasonA2ABusStreamsMissing = "A2ABusStreamsMissing"

	busStreamsPollerLogName = "bus-streams"

	// busStreamsPollInterval is the poll's period, and busStreamsConfirmPolls
	// how many in a row must find a stream missing (or find nothing to judge
	// by, for ExporterUnreachable) before the condition says so. The file's
	// header gives the cost these are sized against.
	busStreamsPollInterval   = 5 * time.Minute
	busStreamsFirstPollDelay = 2 * time.Minute
	busStreamsConfirmPolls   = 3

	// A re-run is a delete of the completed provision Job, at most one per
	// busStreamsReprovisionMinGap and busStreamsReprovisionMax per
	// busStreamsReprovisionWindow, and none for a set a completed re-run
	// already failed to restore.
	busStreamsReprovisionMinGap = 15 * time.Minute
	busStreamsReprovisionWindow = 6 * time.Hour
	busStreamsReprovisionMax    = 3

	// busStreamsReprovisionAnnotation carries the re-run record on the CR
	// (busReprovisionRecord, JSON), so the rate limit outlives an operator
	// restart.
	busStreamsReprovisionAnnotation = "kubeagents.x-k8s.io/a2a-bus-reprovision"

	busStreamsMissingEventReason          = "A2ABusStreamsMissing"
	busStreamsRestoredEventReason         = "A2ABusStreamsRestored"
	busStreamsClientsRestartedEventReason = "A2ABusClientsRestarted"

	// busStreamsScrapeTimeout bounds one scrape from connect to the last byte,
	// and busStreamsMaxLineBytes the one line of the body held at once. A
	// scrape makes the exporter read /jsz?all, about 12 s of its CPU at 2,000
	// consumers, so the bound leaves a throttled exporter room. The body runs
	// to megabytes at a few thousand consumers (every consumer is a dozen
	// series), so it is read line by line and kept nowhere.
	busStreamsScrapeTimeout = 45 * time.Second
	busStreamsMaxLineBytes  = 64 * 1024

	// The exporter's series this reads (prometheus-nats-exporter v0.20.3,
	// collector/jsz.go). The jsz collector writes nothing for a server whose
	// /jsz or /varz read failed, so a jetstream_server_* series in the body is
	// the proof this scrape read the server's stream list.
	busStreamsServerStreamsSeries  = "jetstream_server_total_streams"
	busStreamsServerDisabledSeries = "jetstream_server_jetstream_disabled"
	busStreamsStreamBytesSeries    = "jetstream_stream_total_bytes"
	busStreamsStreamNameLabel      = "stream_name"
	busStreamsMetaLeaderLabel      = "meta_leader"
)

// busServerReading is what one scrape of one server's exporter says.
type busServerReading struct {
	// jsz is true when the body carried the server-level JetStream series,
	// which the exporter writes only after reading /jsz.
	jsz bool
	// disabled is true when the server reports JetStream off.
	disabled bool
	// metaLeader is the JetStream meta leader the server names, empty while
	// a cluster has none (and on a single server, which has no meta group).
	metaLeader string
	// streams is every stream the server lists, in any account.
	streams map[string]bool
}

// busStreamSource reads one exporter. The pod scraper is its one
// implementation; tests supply a stub.
type busStreamSource interface {
	Scrape(ctx context.Context, addr string) (busServerReading, error)
}

// podBusStreamSource GETs /metrics at a pod IP and port over the pod network,
// with the usage scraper's transport rules: no proxy, no redirect, no
// keep-alive, bounded headers.
type podBusStreamSource struct {
	client *http.Client
}

func newPodBusStreamSource() *podBusStreamSource {
	transport := &http.Transport{
		Proxy:                  nil,
		DialContext:            (&net.Dialer{Timeout: busStreamsScrapeTimeout}).DialContext,
		DisableKeepAlives:      true,
		MaxResponseHeaderBytes: usageScrapeMaxHeaderBytes,
	}
	return &podBusStreamSource{client: &http.Client{
		Timeout:       busStreamsScrapeTimeout,
		Transport:     transport,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}}
}

func (s *podBusStreamSource) Scrape(ctx context.Context, addr string) (busServerReading, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, usageScrapeScheme+addr+usageMetricsPath, nil)
	if err != nil {
		return busServerReading{}, err
	}
	resp, err := s.client.Do(req)
	if err != nil {
		return busServerReading{}, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return busServerReading{}, fmt.Errorf("exporter answered %d", resp.StatusCode)
	}
	return foldBusStreamsBody(resp.Body)
}

// foldBusStreamsBody reads the exporter's body line by line for the three
// series busServerReading needs and skips the rest unread. A candidate line is
// parsed on its own with expfmt, so the parser, not a hand-read, names the
// metric and its labels. A candidate line that is too long or doesn't parse
// fails the scrape; any other overlong line is skipped.
func foldBusStreamsBody(body io.Reader) (busServerReading, error) {
	reading := busServerReading{streams: map[string]bool{}}
	reader := bufio.NewReaderSize(body, 4*1024)
	for {
		line, tooLong, err := readBusStreamsLine(reader)
		if len(line) > 0 || tooLong {
			name := busStreamsCandidate(line)
			if name != "" && tooLong {
				return busServerReading{}, fmt.Errorf("a %s line is longer than %d bytes", name, busStreamsMaxLineBytes)
			}
			if name != "" {
				if perr := foldBusStreamsLine(&reading, name, line); perr != nil {
					return busServerReading{}, perr
				}
			}
		}
		if errors.Is(err, io.EOF) {
			return reading, nil
		}
		if err != nil {
			return busServerReading{}, err
		}
	}
}

// readBusStreamsLine returns the next line without its newline, holding at
// most busStreamsMaxLineBytes of it; a longer line is drained and reported as
// tooLong with its first bytes, enough to name its series.
func readBusStreamsLine(reader *bufio.Reader) (line []byte, tooLong bool, err error) {
	for {
		chunk, isPrefix, rerr := reader.ReadLine()
		if len(line)+len(chunk) > busStreamsMaxLineBytes {
			if !tooLong {
				line = append(line, chunk[:min(len(chunk), 256)]...)
			}
			tooLong = true
		} else if !tooLong {
			line = append(line, chunk...)
		}
		if rerr != nil {
			return line, tooLong, rerr
		}
		if !isPrefix {
			return line, tooLong, nil
		}
	}
}

// busStreamsCandidate names the wanted series a line starts with, empty for
// any other line (comments included).
func busStreamsCandidate(line []byte) string {
	for _, name := range []string{busStreamsServerStreamsSeries, busStreamsServerDisabledSeries, busStreamsStreamBytesSeries} {
		if len(line) > len(name) && string(line[:len(name)]) == name && (line[len(name)] == '{' || line[len(name)] == ' ') {
			return name
		}
	}
	return ""
}

func foldBusStreamsLine(reading *busServerReading, want string, line []byte) error {
	parser := expfmt.NewTextParser(model.UTF8Validation)
	families, err := parser.TextToMetricFamilies(strings.NewReader(string(line) + "\n"))
	if err != nil {
		return fmt.Errorf("a %s line does not parse: %w", want, err)
	}
	family, ok := families[want]
	if !ok {
		return fmt.Errorf("a %s line parsed as another series", want)
	}
	for _, metric := range family.GetMetric() {
		labels := map[string]string{}
		for _, pair := range metric.GetLabel() {
			labels[pair.GetName()] = pair.GetValue()
		}
		value := metric.GetUntyped().GetValue()
		switch want {
		case busStreamsServerStreamsSeries:
			reading.jsz = true
			reading.metaLeader = labels[busStreamsMetaLeaderLabel]
		case busStreamsServerDisabledSeries:
			if value != 0 {
				reading.disabled = true
			}
		case busStreamsStreamBytesSeries:
			if name := labels[busStreamsStreamNameLabel]; name != "" {
				reading.streams[name] = true
			}
		}
	}
	return nil
}

// busStreamsVerdict is one poll's answer across the bus's servers.
type busStreamsVerdict struct {
	// known is false when too few servers answered to judge by.
	known bool
	// missing is every provisioned stream no answering server lists, sorted.
	missing []string
}

// busStreamsVerdictFor judges one poll. A server counts only when its scrape
// proves /jsz was read, it has JetStream on, and, on a cluster, it names a
// meta leader: a member with none (mid-election, or cut off) may list nothing
// for reasons that have nothing to do with the streams. One server must answer
// on a single-server bus and a majority on a cluster. A stream is missing only
// when no answering server lists it: every stream is replicated to every
// server (a2aStreamReplicas), so a member still catching up may lack one the
// others have, and that is not a loss.
func busStreamsVerdictFor(replicas int, readings []busServerReading) busStreamsVerdict {
	answered := 0
	seen := map[string]bool{}
	for _, reading := range readings {
		if !reading.jsz || reading.disabled {
			continue
		}
		if replicas > 1 && reading.metaLeader == "" {
			continue
		}
		answered++
		for name := range reading.streams {
			seen[name] = true
		}
	}
	need := 1
	if replicas > 1 {
		need = replicas/2 + 1
	}
	if answered < need {
		return busStreamsVerdict{}
	}
	var missing []string
	for _, name := range a2aProvisionedStreams {
		if !seen[name] {
			missing = append(missing, name)
		}
	}
	slices.Sort(missing)
	return busStreamsVerdict{known: true, missing: missing}
}

// busReprovisionRecord is the re-run history on the CR's annotation, attempts
// inside busStreamsReprovisionWindow only.
type busReprovisionRecord struct {
	Attempts []busReprovisionAttempt `json:"attempts"`
}

type busReprovisionAttempt struct {
	At      time.Time `json:"at"`
	Missing []string  `json:"missing"`
	// JobUID is the completed provision Job the attempt deleted; a Complete
	// Job with another UID is the attempt's re-run.
	JobUID types.UID `json:"jobUID,omitempty"`
	// Settled is set by the first poll that judges the bus after the re-run,
	// and Restored says whether every stream the attempt named has been seen
	// back since: on that poll, or on any later one that finds none of them
	// missing, such as after a fix by hand. An unrestored settled attempt
	// stops the re-runs for its names; seeing them back lifts the stop.
	Settled  bool `json:"settled,omitempty"`
	Restored bool `json:"restored,omitempty"`
	// RestartOwed is set on the settling poll when the re-run brought TASKS
	// back, and Restarted once the clients holding a TASKS durable have been
	// restarted. Owed and not yet restarted is retried on every poll that
	// finds TASKS present, and holds the condition back from AllPresent.
	RestartOwed bool `json:"restartOwed,omitempty"`
	Restarted   bool `json:"restarted,omitempty"`
}

func readBusReprovisionRecord(agent *agentv1alpha1.PlatformAgent) busReprovisionRecord {
	var record busReprovisionRecord
	if raw := agent.Annotations[busStreamsReprovisionAnnotation]; raw != "" {
		// An unreadable record reads as none: the worst it costs is one
		// re-run more than the limit, of a Job that only creates what is
		// missing.
		_ = json.Unmarshal([]byte(raw), &record)
	}
	return record
}

func (rec busReprovisionRecord) last() *busReprovisionAttempt {
	if len(rec.Attempts) == 0 {
		return nil
	}
	return &rec.Attempts[len(rec.Attempts)-1]
}

// allows reports whether a re-run may start at now, and when one may if not.
func (rec busReprovisionRecord) allows(now time.Time) (bool, time.Time) {
	var inWindow []time.Time
	for _, attempt := range rec.Attempts {
		if now.Sub(attempt.At) < busStreamsReprovisionWindow {
			inWindow = append(inWindow, attempt.At)
		}
	}
	if len(inWindow) > 0 {
		if next := inWindow[len(inWindow)-1].Add(busStreamsReprovisionMinGap); now.Before(next) {
			return false, next
		}
	}
	if len(inWindow) >= busStreamsReprovisionMax {
		return false, inWindow[len(inWindow)-busStreamsReprovisionMax].Add(busStreamsReprovisionWindow)
	}
	return true, time.Time{}
}

func (rec busReprovisionRecord) pruned(now time.Time) busReprovisionRecord {
	var kept []busReprovisionAttempt
	for _, attempt := range rec.Attempts {
		if now.Sub(attempt.At) < busStreamsReprovisionWindow {
			kept = append(kept, attempt)
		}
	}
	return busReprovisionRecord{Attempts: kept}
}

// busStreamStreak is one CR's run of polls, per process.
type busStreamStreak struct {
	// missing is the set every poll in the current run found missing.
	missing map[string]bool
	misses  int
	// unknown counts polls in a row that had nothing to judge by.
	unknown int
}

// BusStreamPoller runs the check on the leader, off the reconcile path.
type BusStreamPoller struct {
	r              *PlatformAgentReconciler
	source         busStreamSource
	now            func() time.Time
	firstPollDelay time.Duration
	pollInterval   time.Duration
	streaks        map[types.UID]*busStreamStreak
}

// NewBusStreamPoller returns the poller for r's PlatformAgents.
func NewBusStreamPoller(r *PlatformAgentReconciler) *BusStreamPoller {
	return &BusStreamPoller{
		r:              r,
		source:         newPodBusStreamSource(),
		now:            time.Now,
		firstPollDelay: busStreamsFirstPollDelay,
		pollInterval:   busStreamsPollInterval,
		streaks:        map[types.UID]*busStreamStreak{},
	}
}

func (p *BusStreamPoller) NeedLeaderElection() bool { return true }

// Start polls once after firstPollDelay and then every pollInterval until ctx
// is cancelled.
func (p *BusStreamPoller) Start(ctx context.Context) error {
	first := time.NewTimer(p.firstPollDelay)
	defer first.Stop()
	select {
	case <-ctx.Done():
		return nil
	case <-first.C:
		p.pollOnce(ctx)
	}
	ticker := time.NewTicker(p.pollInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
			p.pollOnce(ctx)
		}
	}
}

func (p *BusStreamPoller) pollOnce(ctx context.Context) {
	log := logf.FromContext(ctx).WithName(busStreamsPollerLogName)
	if p.streaks == nil {
		p.streaks = map[types.UID]*busStreamStreak{}
	}
	var list agentv1alpha1.PlatformAgentList
	if err := p.r.podReader().List(ctx, &list); err != nil {
		log.Error(err, "failed to list PlatformAgents")
		return
	}
	live := map[types.UID]bool{}
	for i := range list.Items {
		agent := &list.Items[i]
		live[agent.UID] = true
		if err := p.pollAgent(ctx, agent); err != nil {
			log.Error(err, "bus stream check failed", "platformagent", client.ObjectKeyFromObject(agent))
		}
	}
	for uid := range p.streaks {
		if !live[uid] {
			delete(p.streaks, uid)
		}
	}
}

func (p *BusStreamPoller) streak(agent *agentv1alpha1.PlatformAgent) *busStreamStreak {
	s := p.streaks[agent.UID]
	if s == nil {
		s = &busStreamStreak{}
		p.streaks[agent.UID] = s
	}
	return s
}

// pollAgent is one CR's poll. It writes BusStreamsPresent and nothing else of
// the status, re-runs the provision Job when a loss is confirmed, and restarts
// the TASKS clients once a re-run has restored TASKS.
func (p *BusStreamPoller) pollAgent(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	if agent.DeletionTimestamp != nil || !a2aStackRendering(agent) || !busProvisioned(agent) {
		delete(p.streaks, agent.UID)
		return p.removeCondition(ctx, agent)
	}
	if a2aNATSExporterOmitted() {
		delete(p.streaks, agent.UID)
		return p.writeCondition(ctx, agent, metav1.ConditionUnknown, busStreamsReasonDetectionUnavailable,
			fmt.Sprintf("The A2A bus runs without its metrics exporter (%s is set and %s is not), so the operator cannot see whether the provisioned streams are still on the bus. Set %s to a copy of the exporter image to turn the check on.",
				a2aNATSImageEnvVar, a2aNATSExporterImageEnvVar, a2aNATSExporterImageEnvVar))
	}
	if _, _, ok := operatorBusPrincipal(); !ok {
		// The bus fence admits the operator to the exporter by its namespace
		// and its pod's bus-client label (a2aOperatorNATSPeers), and renders
		// that peer only when the operator knows its namespace and service
		// account; without them there is no such peer, and every scrape
		// would fail.
		delete(p.streaks, agent.UID)
		return p.writeCondition(ctx, agent, metav1.ConditionUnknown, busStreamsReasonDetectionUnavailable,
			fmt.Sprintf("The operator does not know its own namespace and service account (%s, %s), so the A2A bus fence admits it to no exporter and it cannot see whether the provisioned streams are still on the bus.",
				operatorNamespaceEnvVar, operatorServiceAccountEnvVar))
	}
	streak := p.streak(agent)
	now := p.now()
	record := readBusReprovisionRecord(agent)

	sts, settled, err := p.busSettled(ctx, agent)
	if err != nil {
		return err
	}
	job, jobState, err := p.provisionJob(ctx, agent)
	if err != nil {
		return err
	}
	if !settled || jobState == busJobAbsent || jobState == busJobRunning {
		// No verdict while the bus rolls, converts or provisions: a stream
		// the Job is about to create, or a server still joining, is not a
		// loss. The run starts again afterwards.
		streak.missing, streak.misses, streak.unknown = nil, 0, 0
		return nil
	}

	readings := p.scrapeServers(ctx, agent, sts)
	verdict := busStreamsVerdictFor(int(busServers(sts)), readings)
	if !verdict.known {
		streak.missing, streak.misses = nil, 0
		streak.unknown++
		if streak.unknown < busStreamsConfirmPolls {
			return nil
		}
		return p.writeCondition(ctx, agent, metav1.ConditionUnknown, busStreamsReasonExporterUnreachable,
			"No usable answer from the A2A bus's metrics exporter on the last polls (too few servers answered with their JetStream stream list), so whether the provisioned streams are on the bus is not known.")
	}
	streak.unknown = 0

	// The attempt in flight, if any, is settled on the first poll that judges
	// the bus after its re-run, the provision Job that replaced the one the
	// attempt deleted (told apart by UID: a creation time in whole seconds
	// can't be ordered against the attempt's own clock). The settlement is
	// written before the restart it owes, and the restart is marked done
	// only after its deletes succeed: a write that fails leaves the pods
	// alone, and a restart that fails stays owed and is retried below on the
	// next poll, before anything can read AllPresent.
	last := record.last()
	reRan := last != nil && !last.Settled && jobState == busJobComplete && job.UID != last.JobUID
	if reRan {
		// TASKS is back whatever else still isn't: its two durable readers
		// need restarting now, not once every stream returns.
		last.RestartOwed = slices.Contains(last.Missing, a2aTasksStream) && !slices.Contains(verdict.missing, a2aTasksStream)
		last.Restored = !overlaps(last.Missing, verdict.missing)
		last.Settled = true
		if err := p.writeRecord(ctx, agent, record.pruned(now)); err != nil {
			return err
		}
		if last.Restored {
			p.r.recordEvent(agent, corev1.EventTypeNormal, busStreamsRestoredEventReason,
				fmt.Sprintf("Re-running the A2A provision Job restored %s.", strings.Join(last.Missing, ", ")))
		}
	} else if last != nil && last.Settled && !last.Restored && !overlaps(last.Missing, verdict.missing) {
		// The names a re-run did not bring back are back now (a fix by hand,
		// say): lift the stop, so a later loss of them is re-provisioned.
		last.Restored = true
		if err := p.writeRecord(ctx, agent, record.pruned(now)); err != nil {
			return err
		}
	}
	if last != nil && last.Settled && last.RestartOwed && !last.Restarted && !slices.Contains(verdict.missing, a2aTasksStream) {
		if err := p.restartTasksClients(ctx, agent); err != nil {
			return err
		}
		last.Restarted = true
		if err := p.writeRecord(ctx, agent, record.pruned(now)); err != nil {
			return err
		}
	}

	if len(verdict.missing) == 0 {
		streak.missing, streak.misses = nil, 0
		return p.writeCondition(ctx, agent, metav1.ConditionTrue, busStreamsReasonAllPresent,
			fmt.Sprintf("All %d streams and KV buckets the provision Job creates were seen on the A2A bus.", len(a2aProvisionedStreams)))
	}

	// Missing on this poll: confirm across busStreamsConfirmPolls in a row,
	// keeping only what every poll in the run found missing. A poll that
	// shares nothing with the run so far starts a new run from itself.
	shared := map[string]bool{}
	for _, name := range verdict.missing {
		if streak.misses == 0 || streak.missing[name] {
			shared[name] = true
		}
	}
	if len(shared) == 0 {
		streak.misses = 0
		for _, name := range verdict.missing {
			shared[name] = true
		}
	}
	streak.missing = shared
	streak.misses++
	if streak.misses < busStreamsConfirmPolls {
		return nil
	}
	confirmed := make([]string, 0, len(streak.missing))
	for name := range streak.missing {
		confirmed = append(confirmed, name)
	}
	slices.Sort(confirmed)
	names := strings.Join(confirmed, ", ")

	if last != nil && last.Settled && !last.Restored && overlaps(last.Missing, confirmed) {
		// The last re-run completed without bringing these back. Stop:
		// whatever keeps them off the bus, another delete of the Job will
		// not fix it. A restore, or removing the annotation, clears this.
		return p.writeCondition(ctx, agent, metav1.ConditionFalse, busStreamsReasonDidNotRestore,
			fmt.Sprintf("Missing from the A2A bus: %s. The operator re-ran the provision Job at %s and they were still missing after it, so it will not re-run it again for them. Read the provision Job's logs and the NATS servers' logs; once fixed, remove the %s annotation to let the operator re-run it.",
				names, last.At.UTC().Format(time.RFC3339), busStreamsReprovisionAnnotation))
	}
	if jobState != busJobComplete {
		return p.writeCondition(ctx, agent, metav1.ConditionFalse, busStreamsReasonMissing,
			fmt.Sprintf("Missing from the A2A bus: %s. The provision Job %s failed, so the operator does not re-run it; see the PlatformAgent's events.",
				names, job.Name))
	}
	if ok, next := record.allows(now); !ok {
		return p.writeCondition(ctx, agent, metav1.ConditionFalse, busStreamsReasonMissing,
			fmt.Sprintf("Missing from the A2A bus: %s. The provision Job's next re-run is allowed at %s.",
				names, next.UTC().Format(time.RFC3339)))
	}

	restart := ""
	if slices.Contains(confirmed, a2aTasksStream) {
		restart = " Once TASKS is back, the A2A gateway and the agent pod are restarted, so their TASKS consumers are recreated."
	}
	record = record.pruned(now)
	record.Attempts = append(record.Attempts, busReprovisionAttempt{At: now, Missing: confirmed, JobUID: job.UID})
	if err := p.writeRecord(ctx, agent, record); err != nil {
		return err
	}
	if err := p.writeCondition(ctx, agent, metav1.ConditionFalse, busStreamsReasonReprovisioning,
		fmt.Sprintf("Missing from the A2A bus: %s. The provision Job is running again to recreate them.%s", names, restart)); err != nil {
		return err
	}
	uid := job.UID
	if err := p.r.Delete(ctx, job, client.Preconditions{UID: &uid}, client.PropagationPolicy(metav1.DeletePropagationBackground)); err != nil && !apierrors.IsNotFound(err) {
		return fmt.Errorf("failed to delete the completed A2A provision Job %s: %w", job.Name, err)
	}
	streak.missing, streak.misses = nil, 0
	logf.FromContext(ctx).WithName(busStreamsPollerLogName).Info("re-running the A2A provision Job: provisioned streams are missing from the bus",
		"platformagent", client.ObjectKeyFromObject(agent), "missing", confirmed, "job", job.Name)
	p.r.recordEvent(agent, corev1.EventTypeWarning, busStreamsMissingEventReason,
		fmt.Sprintf("Missing from the A2A bus on %d polls in a row: %s. Deleted the completed provision Job %s so it runs again and recreates them.%s",
			busStreamsConfirmPolls, names, job.Name, restart))
	return nil
}

func overlaps(a, b []string) bool {
	for _, name := range a {
		if slices.Contains(b, name) {
			return true
		}
	}
	return false
}

type busJobState int

const (
	busJobAbsent busJobState = iota
	busJobRunning
	busJobComplete
	busJobFailed
)

// provisionJob reads agent's provision Jobs, live, and returns the newest with
// its state; running when any is still running. The Jobs are found by the
// provision labels and the CR's controller reference, not by the name the
// reconcile renders: the name digests the spec, which reads the bus's server
// count, and a CR read here has no in-memory resolution of that count
// (resolveA2ABusReplicas), so a CRD that prunes status.busReplicas would name
// a Job that doesn't exist. Absent covers a Job the TTL removed or a re-run
// not yet created, both of which the reconcile creates.
func (p *BusStreamPoller) provisionJob(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (*batchv1.Job, busJobState, error) {
	var jobs batchv1.JobList
	if err := p.r.a2aReader().List(ctx, &jobs, client.InNamespace(agent.Namespace),
		client.MatchingLabels{a2aComponentLabel: "provision", labelPartOf: a2aPartOf}); err != nil {
		return nil, busJobAbsent, err
	}
	var newest *batchv1.Job
	newestState := busJobAbsent
	for i := range jobs.Items {
		job := &jobs.Items[i]
		if owner := metav1.GetControllerOf(job); owner == nil || owner.UID != agent.UID || job.DeletionTimestamp != nil {
			continue
		}
		state := busJobRunning
		for _, cond := range job.Status.Conditions {
			if cond.Status != corev1.ConditionTrue {
				continue
			}
			switch cond.Type {
			case batchv1.JobComplete:
				state = busJobComplete
			case batchv1.JobFailed:
				state = busJobFailed
			}
		}
		if state == busJobRunning {
			return job, busJobRunning, nil
		}
		if newest == nil || newest.CreationTimestamp.Before(&job.CreationTimestamp) ||
			(newest.CreationTimestamp.Equal(&job.CreationTimestamp) && job.Name > newest.Name) {
			newest, newestState = job, state
		}
	}
	return newest, newestState, nil
}

// busServers is the bus StatefulSet's server count, the API default of one
// when unset.
func busServers(sts *appsv1.StatefulSet) int32 {
	if sts.Spec.Replicas == nil {
		return 1
	}
	return *sts.Spec.Replicas
}

// busSettled reads the bus StatefulSet and reports whether it is at rest: the
// controller has seen its spec, every replica is updated and ready on one
// revision, and it runs the server count the CR resolves to (not
// mid-conversion). Only a StatefulSet this CR controls counts.
func (p *BusStreamPoller) busSettled(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (*appsv1.StatefulSet, bool, error) {
	sts := &appsv1.StatefulSet{}
	if err := p.r.a2aReader().Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: a2aNATSName(agent)}, sts); err != nil {
		return nil, false, client.IgnoreNotFound(err)
	}
	if owner := metav1.GetControllerOf(sts); owner == nil || owner.UID != agent.UID {
		return nil, false, nil
	}
	replicas := busServers(sts)
	// The count the CR records for the running bus, when it records one. A
	// CRD that predates status.busReplicas prunes it, and a2aNATSReplicas
	// would then answer its default for a new bus, not the running one; the
	// StatefulSet is the count then.
	want := replicas
	if recorded := agent.Status.BusReplicas; recorded == 1 || recorded == 3 {
		want = recorded
	}
	return sts, sts.Status.ObservedGeneration >= sts.Generation &&
		replicas == want &&
		sts.Status.Replicas == replicas &&
		sts.Status.ReadyReplicas == replicas &&
		sts.Status.UpdatedReplicas == replicas &&
		sts.Status.CurrentRevision == sts.Status.UpdateRevision, nil
}

// scrapeServers reads each bus server's exporter. The pods are read by the
// names the StatefulSet gives them, <name>-0 to <name>-(replicas-1), one per
// ordinal, rather than listed by label: the label is one any pod in the
// namespace can carry, and what a pod serves on the port decides whether the
// operator deletes the provision Job and restarts the gateway pods. A name
// can't be shared, so no other pod can stand in for a server or crowd one out.
// A pod also counts only when the StatefulSet controls it (by UID), it runs
// the StatefulSet's current revision, and it isn't on the host network. A pod
// that is missing or can't be read gives no reading, which the verdict doesn't
// count.
func (p *BusStreamPoller) scrapeServers(ctx context.Context, agent *agentv1alpha1.PlatformAgent, sts *appsv1.StatefulSet) []busServerReading {
	log := logf.FromContext(ctx).WithName(busStreamsPollerLogName)
	replicas := busServers(sts)
	var readings []busServerReading
	for ordinal := int32(0); ordinal < replicas; ordinal++ {
		pod := &corev1.Pod{}
		name := fmt.Sprintf("%s-%d", sts.Name, ordinal)
		if err := p.r.podReader().Get(ctx, types.NamespacedName{Namespace: sts.Namespace, Name: name}, pod); err != nil {
			if !apierrors.IsNotFound(err) {
				log.Error(err, "failed to read an A2A bus pod", "pod", name)
			}
			continue
		}
		if owner := metav1.GetControllerOf(pod); owner == nil || owner.UID != sts.UID {
			continue
		}
		if pod.Labels[appsv1.ControllerRevisionHashLabelKey] != sts.Status.UpdateRevision || pod.Spec.HostNetwork {
			continue
		}
		if pod.DeletionTimestamp != nil || pod.Status.PodIP == "" || !usageContainerReady(pod, a2aNATSExporterContainerName) {
			continue
		}
		port, ok := usagePodPort(pod, a2aNATSExporterContainerName, a2aNATSMetricsPortName)
		if !ok {
			continue
		}
		reading, err := p.source.Scrape(ctx, net.JoinHostPort(pod.Status.PodIP, strconv.Itoa(int(port))))
		if err != nil {
			log.V(1).Info("could not read the A2A bus exporter", "pod", pod.Name, "error", err.Error())
			continue
		}
		readings = append(readings, reading)
	}
	return readings
}

// restartTasksClients deletes the pods of the two clients that hold a fixed
// durable on TASKS, the A2A gateway (its relay) and the agent pod (the bridge
// sidecar), so their Deployments recreate them and they subscribe to the
// recreated consumers. Neither re-creates its subscription on its own once the
// stream it consumed was deleted (lib.Client.SubscribeDurable). Each delete
// names the pod's UID. Only each client's own pods are deleted: those its
// workload controls, as the bus servers' pods are read.
func (p *BusStreamPoller) restartTasksClients(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	// The bridge is always in the agent pod by now: the operator renders it
	// once the bus is provisioned (a2aBridgeInPod), unless the CR declares
	// its own, which is in the same pod.
	clients := []struct {
		name     string
		selector map[string]string
	}{
		{a2aGatewayName(agent), a2aGatewayPodSelector(agent)},
		{agent.Name + gatewayAppSuffix, gatewayPodSelector(agent)},
	}
	var restarted []string
	for _, c := range clients {
		pods, err := p.tasksClientPods(ctx, agent, c.name, c.selector)
		if err != nil {
			return err
		}
		for i := range pods {
			pod := &pods[i]
			if pod.DeletionTimestamp != nil {
				continue
			}
			uid := pod.UID
			if err := p.r.Delete(ctx, pod, client.Preconditions{UID: &uid}); err != nil {
				if apierrors.IsNotFound(err) || apierrors.IsConflict(err) {
					continue
				}
				return fmt.Errorf("failed to restart %s: %w", pod.Name, err)
			}
			restarted = append(restarted, pod.Name)
		}
	}
	if len(restarted) > 0 {
		p.r.recordEvent(agent, corev1.EventTypeNormal, busStreamsClientsRestartedEventReason,
			fmt.Sprintf("Restarted %s after the provision Job recreated TASKS, so their TASKS consumers are recreated.", strings.Join(restarted, ", ")))
	}
	return nil
}

// tasksClientPods lists one TASKS client's pods: those with its app label that
// its workload of that name, controlled by the CR, controls. A StatefulSet
// controls its pods directly, a Deployment through its ReplicaSets. A pod that
// only carries the label is left alone.
func (p *BusStreamPoller) tasksClientPods(ctx context.Context, agent *agentv1alpha1.PlatformAgent, name string, selector map[string]string) ([]corev1.Pod, error) {
	reader := p.r.a2aReader()
	key := types.NamespacedName{Namespace: agent.Namespace, Name: name}
	inNamespace := []client.ListOption{client.InNamespace(agent.Namespace), client.MatchingLabels(selector)}
	owners := map[types.UID]bool{}
	var dep appsv1.Deployment
	if err := reader.Get(ctx, key, &dep); err == nil {
		if metav1.IsControlledBy(&dep, agent) {
			var sets appsv1.ReplicaSetList
			if err := reader.List(ctx, &sets, inNamespace...); err != nil {
				return nil, err
			}
			for i := range sets.Items {
				if metav1.IsControlledBy(&sets.Items[i], &dep) {
					owners[sets.Items[i].UID] = true
				}
			}
		}
	} else if !apierrors.IsNotFound(err) {
		return nil, err
	}
	var sts appsv1.StatefulSet
	if err := reader.Get(ctx, key, &sts); err == nil {
		if metav1.IsControlledBy(&sts, agent) {
			owners[sts.UID] = true
		}
	} else if !apierrors.IsNotFound(err) {
		return nil, err
	}
	if len(owners) == 0 {
		return nil, nil
	}
	var pods corev1.PodList
	if err := p.r.podReader().List(ctx, &pods, inNamespace...); err != nil {
		return nil, err
	}
	var own []corev1.Pod
	for _, pod := range pods.Items {
		if ref := metav1.GetControllerOf(&pod); ref != nil && owners[ref.UID] {
			own = append(own, pod)
		}
	}
	return own, nil
}

// writeRecord stores the re-run record on the CR's annotation, on a fresh read.
func (p *BusStreamPoller) writeRecord(ctx context.Context, agent *agentv1alpha1.PlatformAgent, record busReprovisionRecord) error {
	raw, err := json.Marshal(record)
	if err != nil {
		return err
	}
	base := agent.DeepCopy()
	if agent.Annotations == nil {
		agent.Annotations = map[string]string{}
	}
	agent.Annotations[busStreamsReprovisionAnnotation] = string(raw)
	return p.r.Patch(ctx, agent, client.MergeFrom(base))
}

// writeCondition sets BusStreamsPresent, the one status field this poller
// owns, when it differs. The patch carries the resource version, so a status
// write the reconciler made since the read fails it rather than being
// overwritten; the next poll writes it again.
func (p *BusStreamPoller) writeCondition(ctx context.Context, agent *agentv1alpha1.PlatformAgent, status metav1.ConditionStatus, reason, message string) error {
	if existing := meta.FindStatusCondition(agent.Status.Conditions, busStreamsPresentConditionType); existing != nil &&
		existing.Status == status && existing.Reason == reason && existing.Message == message {
		return nil
	}
	for attempt := 0; ; attempt++ {
		base := agent.DeepCopy()
		meta.SetStatusCondition(&agent.Status.Conditions, metav1.Condition{
			Type:               busStreamsPresentConditionType,
			Status:             status,
			Reason:             reason,
			Message:            message,
			ObservedGeneration: agent.Generation,
		})
		err := p.r.Status().Patch(ctx, agent, client.MergeFromWithOptions(base, client.MergeFromWithOptimisticLock{}))
		if err == nil || !apierrors.IsConflict(err) || attempt == 2 {
			return err
		}
		// The reconciler wrote the status since the read, often on the
		// change this poll just made to the annotation: read it again and
		// set the condition on what it wrote.
		if err := p.r.podReader().Get(ctx, client.ObjectKeyFromObject(agent), agent); err != nil {
			return err
		}
	}
}

func (p *BusStreamPoller) removeCondition(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	if meta.FindStatusCondition(agent.Status.Conditions, busStreamsPresentConditionType) == nil || agent.DeletionTimestamp != nil {
		return nil
	}
	base := agent.DeepCopy()
	meta.RemoveStatusCondition(&agent.Status.Conditions, busStreamsPresentConditionType)
	return p.r.Status().Patch(ctx, agent, client.MergeFromWithOptions(base, client.MergeFromWithOptimisticLock{}))
}

// busStreamsMissingCondition returns BusStreamsPresent while it is False on a
// CR whose stack still renders, for Degraded; nil otherwise.
func busStreamsMissingCondition(agent *agentv1alpha1.PlatformAgent) *metav1.Condition {
	if !a2aStackRendering(agent) {
		return nil
	}
	cond := meta.FindStatusCondition(agent.Status.Conditions, busStreamsPresentConditionType)
	if cond == nil || cond.Status != metav1.ConditionFalse {
		return nil
	}
	return cond
}
