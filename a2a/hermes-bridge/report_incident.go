package hermesbridge

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/nats-io/nats.go"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The incident route's answering half (lib.IncidentSubject): the gateway
// posted a scheduled report to the home channel and asks for it to be kept
// against its thread, and the bridge, the one bus participant in this pod
// holding session-kv's bearer, stores it with POST /v1/incidents, the
// endpoint the kanban notifier writes, under the address getIncident reads.
// A reply in that thread is then answered with the report as
// <untrusted_report>, so the bridge writes only into a channel this install's
// chat backend serves: a write naming any other would let the gateway attach
// text to a thread it never posted in. Nothing here starts a turn.
const (
	// IncidentSlackHomeEnv, IncidentGchatHomeEnv and IncidentSlackHomeFileEnv
	// name the served channels (Config.IncidentSlackHome and the rest), set
	// by the operator.
	IncidentSlackHomeEnv     = "A2A_BRIDGE_SLACK_HOME_CHANNEL"
	IncidentGchatHomeEnv     = "A2A_BRIDGE_GCHAT_HOME_CHANNEL"
	IncidentSlackHomeFileEnv = "A2A_BRIDGE_SLACK_HOME_FILE"
	// incidentStorePath is session-kv's write, beside its by-thread read.
	incidentStorePath = "/v1/incidents"
	// incidentStoreTimeout bounds the POST, inside the gateway's own wait.
	incidentStoreTimeout = 2 * time.Second
	// incidentKeepMaxBytes bounds a request read: the gateway cuts a report
	// at 24KiB, and JSON escaping can grow it.
	incidentKeepMaxBytes = 256 << 10
	// incidentHomeFileMax bounds the projected home file read.
	incidentHomeFileMax = 256
)

// errIncidentNotHosted is a write naming a channel this install's chat
// backend does not serve.
var errIncidentNotHosted = errors.New("the incident names a channel this install's chat backend does not serve")

// serveIncidents binds the incident route, when the bridge has a session-kv
// to write. It returns the subscription to stop, or nil when nothing was
// bound.
func (b *Bridge) serveIncidents() lib.Subscription {
	if b.cfg.RouteURL == "" || strings.TrimSpace(b.cfg.RouteKey) == "" {
		b.cfg.Logger.Info("incident route not bound: no session-kv URL or key; a reply under a scheduled report will not carry it",
			"env", RouteKeyEnv)
		return nil
	}
	sub, err := b.c.SubscribeCore(lib.IncidentSubject, b.answerIncident)
	if err != nil {
		b.cfg.Logger.Warn("incident route not bound; a reply under a scheduled report will not carry it", "err", err)
		return nil
	}
	return sub
}

// answerIncident keeps one report. A request whose reply subject is not in
// the gateway's namespace is dropped: the grant would refuse the answer
// anyway. The POST runs off the subscription's goroutine.
func (b *Bridge) answerIncident(m *nats.Msg) {
	if !strings.HasPrefix(m.Reply, lib.IncidentReplyPrefix) {
		return
	}
	go func() {
		result := lib.IncidentKeepResult{OK: true}
		if err := b.keepIncident(m.Data); err != nil {
			b.cfg.Logger.Warn("incident not kept", "err", err)
			result = lib.IncidentKeepResult{Error: err.Error()}
		}
		data, _ := json.Marshal(result)
		if err := m.Respond(data); err != nil {
			b.cfg.Logger.Warn("incident answer not sent", "err", err)
		}
	}()
}

// keepIncident checks one request and stores it.
func (b *Bridge) keepIncident(data []byte) error {
	if len(data) > incidentKeepMaxBytes {
		return fmt.Errorf("the request is %d bytes; the limit is %d", len(data), incidentKeepMaxBytes)
	}
	var keep lib.IncidentKeep
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&keep); err != nil {
		return fmt.Errorf("the request is not an incident: %w", err)
	}
	if strings.TrimSpace(keep.Report) == "" {
		return errors.New("the incident has no report")
	}
	addr, err := incidentStoreAddress(keep.Platform, keep.ChatID, keep.ThreadID)
	if err != nil {
		return err
	}
	if !b.hostsIncidentChat(keep.Platform, addr.chatID) {
		b.cfg.Logger.Warn("incident refused: not a channel this install serves",
			"platform", keep.Platform, "chat", addr.chatID, "thread", addr.threadID)
		return errIncidentNotHosted
	}
	return b.storeIncident(addr, keep.Report)
}

// incidentStoreAddress is the (chat_id, thread_id) getIncident reads for a
// reply in the thread a post landed in (incidentThread): on Slack the
// channel and the bare ts, on Google Chat the space and the thread's full
// name.
func incidentStoreAddress(platform, chatID, threadID string) (incidentAddress, error) {
	chatID, threadID = strings.TrimSpace(chatID), strings.TrimSpace(threadID)
	switch platform {
	case lib.NotifyPlatformSlack:
		ts := threadID
		if channel, bare, qualified := strings.Cut(threadID, "/"); qualified {
			if channel != chatID {
				return incidentAddress{}, fmt.Errorf("slack thread %q is not in channel %q", threadID, chatID)
			}
			ts = bare
		}
		if chatID == "" || !slackTS(ts) {
			return incidentAddress{}, fmt.Errorf("slack thread %q in %q is not a channel and a ts", threadID, chatID)
		}
		return incidentAddress{chatID: chatID, threadID: ts}, nil
	case lib.NotifyPlatformGchat:
		if !strings.HasPrefix(chatID, "spaces/") || strings.Contains(chatID[len("spaces/"):], "/") {
			return incidentAddress{}, fmt.Errorf("google chat space %q is not spaces/<id>", chatID)
		}
		thread := threadID
		if !strings.Contains(thread, "/") {
			thread = chatID + incidentGchatThreads + thread
		}
		rest, ok := strings.CutPrefix(thread, chatID+incidentGchatThreads)
		if !ok || rest == "" || strings.Contains(rest, "/") {
			return incidentAddress{}, fmt.Errorf("google chat thread %q is not a thread of %q", threadID, chatID)
		}
		return incidentAddress{chatID: chatID, threadID: thread}, nil
	}
	return incidentAddress{}, fmt.Errorf("platform %q keeps no incidents", platform)
}

// slackTS reports whether s is a Slack message ts: digits, a dot, digits.
func slackTS(s string) bool {
	whole, frac, ok := strings.Cut(s, ".")
	return ok && whole != "" && frac != "" && strings.Trim(whole, "0123456789") == "" && strings.Trim(frac, "0123456789") == ""
}

// hostsIncidentChat reports whether chatID is a channel this install's chat
// backend serves on platform: its home channel, set on the CR or, on Slack,
// learned by the gateway and projected into the pod.
func (b *Bridge) hostsIncidentChat(platform, chatID string) bool {
	if chatID == "" {
		return false
	}
	switch platform {
	case lib.NotifyPlatformSlack:
		if home := strings.TrimSpace(b.cfg.IncidentSlackHome); home != "" && home == chatID {
			return true
		}
		return b.cfg.IncidentSlackHomeFile != "" && readHomeFile(b.cfg.IncidentSlackHomeFile) == chatID
	case lib.NotifyPlatformGchat:
		home := strings.TrimSpace(b.cfg.IncidentGchatHome)
		return home != "" && home == chatID
	}
	return false
}

// readHomeFile is the projected home channel, "" when the file is absent,
// unreadable or empty.
func readHomeFile(path string) string {
	f, err := os.Open(path)
	if err != nil {
		return ""
	}
	defer f.Close()
	raw, err := io.ReadAll(io.LimitReader(f, incidentHomeFileMax))
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(raw))
}

// storeIncident writes report at addr with session-kv's POST /v1/incidents.
func (b *Bridge) storeIncident(addr incidentAddress, report string) error {
	body, err := json.Marshal(map[string]string{"chat_id": addr.chatID, "thread_id": addr.threadID, "report": report})
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), incidentStoreTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimRight(b.cfg.RouteURL, "/")+incidentStorePath, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+b.cfg.RouteKey)
	req.Header.Set("Content-Type", "application/json")
	resp, err := b.routeClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		raw, _ := io.ReadAll(io.LimitReader(resp.Body, routeErrorTailBytes))
		return fmt.Errorf("session-kv answered HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(raw)))
	}
	return nil
}
