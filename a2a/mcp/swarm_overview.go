package mcp

import (
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

func buildSwarmOverview(
	input SwarmProjectionInput,
	depName string,
	members map[string]SwarmMember,
	sessions []sessionWorkerProjection,
	tasks []*TaskRecord,
	taskInSeq map[string]int,
	taskLastSeq map[string]int,
	topics []TopicSnapshot,
	topicSeq map[string]int,
	head int,
	now time.Time,
) SwarmView {
	activeTasks := 0
	completedTasks := 0
	for _, t := range tasks {
		if t.Terminal {
			completedTasks++
		} else {
			activeTasks++
		}
	}
	activeSessions := 0
	for _, s := range sessions {
		if s.sessionMode && !s.terminated {
			activeSessions++
		}
	}

	phaseLabel := "Monitoring Bus"
	phaseIdx := 1
	phaseTotal := 2
	if activeTasks > 0 {
		phaseLabel = fmt.Sprintf("Executing %d Task(s)", activeTasks)
		phaseIdx = 2
	} else if completedTasks > 0 {
		phaseLabel = "Ready · Idle"
		phaseIdx = 2
	}

	headline := fmt.Sprintf(
		"Connected to %s · %d /session worker(s) · %d task(s) on TASKS",
		depName, activeSessions, len(tasks),
	)

	var attention []SwarmAttentionItem
	usedItemIDs := make(map[string]bool)
	allocID := func(prefix, raw string) string {
		slug := sanitizeItemID(prefix + "-" + raw)
		if !usedItemIDs[slug] {
			usedItemIDs[slug] = true
			return slug
		}
		for i := 2; i < 100; i++ {
			cand := sanitizeItemID(fmt.Sprintf("%s-%d", slug, i))
			if !usedItemIDs[cand] {
				usedItemIDs[cand] = true
				return cand
			}
		}
		return slug
	}

	// 1. Attention items: input-required tasks, failed tasks, and openable /session subagents.
	for i := len(tasks) - 1; i >= 0 && len(attention) < maxAttentionItemsInView; i-- {
		t := tasks[i]
		seq := firstPositiveInt(taskLastSeq[t.TaskID], taskInSeq[t.TaskID], head)
		switch {
		case t.State == lib.StateInputRequired:
			attention = append(attention, SwarmAttentionItem{
				ID:       allocID("input", t.TaskID),
				Kind:     "decision",
				Severity: "high",
				Title:    truncateSwarmText(fmt.Sprintf("Input required on task %s (%s)", t.TaskID, t.Addressee), maxViewTitleChars),
				Why:      truncateSwarmText(firstNonEmptyStr(t.StatusText, t.ProgressText, t.PromptText), maxViewWhyChars),
				Refs:     []SwarmRef{{Seq: seq}},
				Actions: []SwarmAction{
					{Type: "board_post", Target: seq, Label: fmt.Sprintf("Jump to #%d", seq)},
				},
			})
		case t.State == lib.StateFailed || t.State == lib.StateRejected:
			attention = append(attention, SwarmAttentionItem{
				ID:       allocID("fail", t.TaskID),
				Kind:     "failure",
				Severity: "med",
				Title:    truncateSwarmText(fmt.Sprintf("Task %s (%s) %s", t.TaskID, t.Addressee, t.State), maxViewTitleChars),
				Why:      truncateSwarmText(firstNonEmptyStr(t.StatusText, t.ResultText, t.PromptText), maxViewWhyChars),
				Refs:     []SwarmRef{{Seq: seq}},
				Actions: []SwarmAction{
					{Type: "board_post", Target: seq, Label: fmt.Sprintf("Jump to #%d", seq)},
				},
			})
		}
	}

	for _, sess := range sessions {
		if len(attention) >= maxAttentionItemsInView {
			break
		}
		if sess.conversationID == "" || (sess.terminated && !sess.sessionMode) {
			continue
		}
		title := fmt.Sprintf("Persistent /session worker @%s is active", sess.memberName)
		if sess.workerID != "" {
			title = fmt.Sprintf("Persistent /session worker @%s (%s) is ready", sess.memberName, sess.workerID)
		}
		why := firstNonEmptyStr(sess.description, "Click 'Open Session Chat' to talk 1-on-1 with this dedicated Claude Code /session pod.")
		if sess.working && sess.activeStep != "" {
			why = "Currently executing: " + sess.activeStep
		}
		attention = append(attention, SwarmAttentionItem{
			ID:       allocID("sess-att", sess.memberName),
			Kind:     "fyi",
			Severity: "low",
			Title:    truncateSwarmText(title, maxViewTitleChars),
			Why:      truncateSwarmText(why, maxViewWhyChars),
			Refs:     []SwarmRef{{Member: sess.memberName}},
			Actions: []SwarmAction{
				{Type: "open_conv", Target: sess.memberName, Label: "Open Session Chat"},
			},
		})
	}

	// 2. Workstreams: one per /session worker + one for Platform Agent.
	var workstreams []SwarmWorkstream
	for _, sess := range sessions {
		if len(workstreams) >= maxWorkstreamsInView {
			break
		}
		wsState := "active"
		if sess.terminated && !sess.sessionMode {
			wsState = "done"
		} else if !sess.working {
			wsState = "active"
		}
		note := firstNonEmptyStr(sess.description, "Persistent /session worker pod")
		if sess.workerID != "" {
			note = fmt.Sprintf("Pod %s · console:%s · %s", sess.workerID, sess.token, note)
		}
		if sess.working && sess.activeStep != "" {
			note = "⚡ " + sess.activeStep + " — " + note
		}
		var actions []SwarmAction
		if sess.conversationID != "" {
			actions = append(actions, SwarmAction{
				Type:   "open_conv",
				Target: sess.memberName,
				Label:  "Open Session Chat",
			})
		}
		workstreams = append(workstreams, SwarmWorkstream{
			ID:      allocID("ws-sess", sess.memberName),
			Title:   truncateSwarmText(fmt.Sprintf("Session Worker: %s", sess.memberName), maxViewTitleChars),
			Owners:  []string{sess.memberName},
			State:   wsState,
			Note:    truncateSwarmText(note, maxViewTextItemChars),
			Refs:    []SwarmRef{{Member: sess.memberName}},
			Actions: actions,
		})
	}

	platformOwners := []string{swarmMemberPlatform}
	platformState := "active"
	platformNote := fmt.Sprintf("Hermes orchestrator on %s (%s/%s)", depName, firstNonEmptyStr(input.Deployment.Namespace, "kubeagents-system"), firstNonEmptyStr(input.Deployment.PlatformAgentName, "platform-agent"))
	latestPlatformSeq := head
	for i := len(tasks) - 1; i >= 0; i-- {
		t := tasks[i]
		if t.Addressee == swarmMemberPlatform {
			latestPlatformSeq = firstPositiveInt(taskLastSeq[t.TaskID], taskInSeq[t.TaskID], head)
			platformNote = fmt.Sprintf("Latest task %s (%s): %s", t.TaskID, t.State, extractTaskResultSummary(t))
			break
		}
	}
	platformActions := []SwarmAction{
		{Type: "board_post", Target: latestPlatformSeq, Label: fmt.Sprintf("Latest Post #%d", latestPlatformSeq)},
	}
	if strings.TrimSpace(input.RootConversationID) != "" {
		platformActions = append(platformActions, SwarmAction{
			Type:   "open_conv",
			Target: swarmMemberCoordinator,
			Label:  "Open Coordinator Chat",
		})
	}
	workstreams = append(workstreams, SwarmWorkstream{
		ID:      allocID("ws", "platform-fleet"),
		Title:   truncateSwarmText(fmt.Sprintf("Platform Agent (%s)", depName), maxViewTitleChars),
		Owners:  platformOwners,
		State:   platformState,
		Note:    truncateSwarmText(platformNote, maxViewTextItemChars),
		Refs:    []SwarmRef{{Member: swarmMemberPlatform}, {Seq: latestPlatformSeq}},
		Actions: platformActions,
	})

	// 3. Milestones & Risks: from TOPICS-STATE snapshots and recent completed tasks.
	var milestones []SwarmTextItem
	var risks []SwarmTextItem

	for i := len(topics) - 1; i >= 0; i-- {
		top := topics[i]
		seq := firstPositiveInt(topicSeq[top.Subject], head)
		if seq <= 0 {
			seq = head
		}
		summary := firstNonEmptyStr(top.Summary, top.Subject)
		if len(milestones) < maxMilestonesInView {
			milestones = append(milestones, SwarmTextItem{
				ID:   allocID("topic", top.Topic),
				Text: truncateSwarmText(fmt.Sprintf("Topic [%s]: %s", top.Topic, firstLine(summary)), maxViewTextItemChars),
				Refs: []SwarmRef{{Seq: seq}},
				Actions: []SwarmAction{
					{Type: "board_post", Target: seq, Label: fmt.Sprintf("Topic #%d", seq)},
				},
			})
		}
		for _, rText := range extractStructuredTopicRisks(top) {
			if len(risks) >= maxRisksInView {
				break
			}
			risks = append(risks, SwarmTextItem{
				ID:   allocID("risk", top.Topic),
				Text: truncateSwarmText(fmt.Sprintf("[%s] %s", top.Topic, rText), maxViewTextItemChars),
				Refs: []SwarmRef{{Seq: seq}},
				Actions: []SwarmAction{
					{Type: "board_post", Target: seq, Label: fmt.Sprintf("Topic #%d", seq)},
				},
			})
		}
	}

	for i := len(tasks) - 1; i >= 0 && len(milestones) < maxMilestonesInView; i-- {
		t := tasks[i]
		if !t.Terminal {
			continue
		}
		seq := firstPositiveInt(taskLastSeq[t.TaskID], taskInSeq[t.TaskID], head)
		milestones = append(milestones, SwarmTextItem{
			ID:   allocID("task", t.TaskID),
			Text: truncateSwarmText(fmt.Sprintf("Task %s (%s -> %s): %s", t.TaskID, t.Addressee, t.State, extractTaskResultSummary(t)), maxViewTextItemChars),
			Refs: []SwarmRef{{Seq: seq}},
			Actions: []SwarmAction{
				{Type: "board_post", Target: seq, Label: fmt.Sprintf("Post #%d", seq)},
			},
		})
	}

	if len(milestones) == 0 {
		milestones = append(milestones, SwarmTextItem{
			ID:   allocID("init", depName),
			Text: truncateSwarmText(fmt.Sprintf("Connected to %s A2A v2 JetStream bus", depName), maxViewTextItemChars),
			Refs: []SwarmRef{{Seq: head}},
		})
	}

	return SwarmView{
		Version:     1,
		AsOfSeq:     head,
		UpdatedAt:   now.UTC().Format(time.RFC3339),
		Headline:    truncateSwarmText(headline, maxViewHeadlineChars),
		Phase:       &SwarmPhase{Label: phaseLabel, Index: phaseIdx, Total: phaseTotal},
		Attention:   attention,
		Workstreams: workstreams,
		Milestones:  milestones,
		Risks:       risks,
	}
}

func extractStructuredTopicRisks(top TopicSnapshot) []string {
	if len(top.Data) == 0 {
		return nil
	}
	var payload map[string]any
	if json.Unmarshal(top.Data, &payload) != nil {
		return nil
	}
	var out []string
	for _, key := range []string{"blockers", "risks", "warnings", "deprecations"} {
		rawList, ok := payload[key].([]any)
		if !ok {
			continue
		}
		for _, item := range rawList {
			switch v := item.(type) {
			case string:
				if strings.TrimSpace(v) != "" {
					out = append(out, strings.TrimSpace(v))
				}
			case map[string]any:
				title := firstNonEmptyAnyStr(v["title"], v["name"], v["kind"], v["id"], v["summary"], v["message"])
				detail := firstNonEmptyAnyStr(v["detail"], v["reason"], v["impact"], v["severity"])
				if title != "" && detail != "" {
					out = append(out, title+" ("+detail+")")
				} else if title != "" {
					out = append(out, title)
				}
			}
		}
	}
	return out
}

func buildSwarmOverviewHTML() string {
	return `<style>
  * { box-sizing: border-box; }
  body {
    margin: 0;
    padding: 12px 14px 18px;
    font-family: var(--font-family, system-ui, -apple-system, sans-serif);
    color: var(--foreground, #e4e4e7);
    background: var(--background, transparent);
    font-size: 12.5px;
    line-height: 1.45;
  }
  .hero {
    padding: 11px 13px;
    border-radius: 8px;
    background: var(--card, rgba(255,255,255,0.03));
    border: 1px solid var(--border, rgba(255,255,255,0.1));
    margin-bottom: 12px;
  }
  .hero-top {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 10px;
  }
  .phase-pill {
    flex-shrink: 0;
    font-size: 11px;
    font-weight: 600;
    padding: 2px 8px;
    border-radius: 999px;
    background: var(--secondary, rgba(59,130,246,0.15));
    color: var(--primary, #60a5fa);
  }
  .headline {
    font-size: 13.5px;
    font-weight: 600;
    margin: 0;
  }
  .kpi-row {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(96px, 1fr));
    gap: 6px;
    margin-top: 9px;
  }
  .kpi {
    padding: 6px 8px;
    border-radius: 6px;
    background: var(--secondary, rgba(255,255,255,0.04));
    border: 1px solid var(--border, rgba(255,255,255,0.07));
  }
  .kpi-val {
    font-size: 14px;
    font-weight: 700;
    color: var(--foreground, #e4e4e7);
  }
  .kpi-lbl {
    font-size: 10.5px;
    color: var(--muted-foreground, #a1a1aa);
    text-transform: uppercase;
    letter-spacing: 0.03em;
  }
  .section-title {
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--muted-foreground, #a1a1aa);
    margin: 13px 0 6px;
  }
  .card-list {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .card {
    position: relative;
    padding: 9px 11px;
    border-radius: 7px;
    background: var(--card, rgba(255,255,255,0.03));
    border: 1px solid var(--border, rgba(255,255,255,0.09));
  }
  .card-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    font-weight: 600;
    padding-right: 22px;
  }
  .card-note {
    margin-top: 4px;
    color: var(--muted-foreground, #a1a1aa);
    font-size: 12px;
  }
  .card-foot {
    margin-top: 7px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 6px;
    flex-wrap: wrap;
  }
  .refs {
    display: flex;
    align-items: center;
    gap: 4px;
    flex-wrap: wrap;
  }
  .ref-pill {
    font-family: var(--editor-font-family, monospace);
    font-size: 11px;
    padding: 1px 6px;
    border-radius: 4px;
    background: var(--secondary, rgba(255,255,255,0.07));
    color: var(--muted-foreground, #a1a1aa);
    border: 1px solid var(--border, rgba(255,255,255,0.08));
  }
  button.ref-pill {
    cursor: pointer;
  }
  button.ref-pill:hover {
    color: var(--foreground, #fff);
    border-color: var(--primary, #60a5fa);
  }
  .badge {
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.03em;
    padding: 1px 6px;
    border-radius: 4px;
    background: var(--secondary, rgba(255,255,255,0.08));
  }
  .badge.active {
    color: #34d399;
    background: rgba(52, 211, 153, 0.14);
  }
  .badge.decision, .badge.failure {
    color: #f87171;
    background: rgba(248, 113, 113, 0.14);
  }
  .btn-group {
    display: flex;
    align-items: center;
    gap: 5px;
    flex-wrap: wrap;
  }
  .btn {
    cursor: pointer;
    font-size: 11.5px;
    font-weight: 500;
    padding: 3px 9px;
    border-radius: 5px;
    border: 1px solid var(--border, rgba(255,255,255,0.16));
    background: var(--secondary, rgba(255,255,255,0.06));
    color: var(--foreground, inherit);
  }
  .btn:hover {
    border-color: var(--primary, #60a5fa);
  }
</style>
<div id="root"></div>
<script>
  function esc(s) {
    return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  var currentActions = [];
  function renderActionBtn(a) {
    currentActions.push(a);
    var idx = currentActions.length - 1;
    return '<button type="button" class="btn" data-act="' + idx + '">' + esc(a.label || 'Open') + '</button>';
  }
  function renderRefs(refs) {
    if (!Array.isArray(refs) || !refs.length) return '<span></span>';
    var out = '<span class="refs">';
    refs.forEach(function(r) {
      if (r.seq) {
        currentActions.push({ type: 'board_post', target: Number(r.seq), label: '#' + r.seq });
        var idx = currentActions.length - 1;
        out += '<button type="button" class="ref-pill" data-act="' + idx + '">#' + esc(r.seq) + '</button>';
      } else if (r.member) {
        out += '<span class="ref-pill">@' + esc(r.member) + '</span>';
      }
    });
    out += '</span>';
    return out;
  }
  document.addEventListener('click', function(e) {
    var btn = e.target.closest('[data-act]');
    if (!btn) return;
    var a = currentActions[Number(btn.getAttribute('data-act'))];
    if (!a || !window.narrator || typeof window.narrator.navigate !== 'function') return;
    var target = a.type === 'board_post' ? Number(a.target) : a.target;
    window.narrator.navigate({ type: String(a.type || ''), target: target });
  });
  window.narrator.onData(function(d, meta) {
    var v = (d && d.view) ? d.view : (d || {});
    var m = meta || window.narrator.meta || {};
    currentActions = [];
    var phaseText = '';
    if (v.phase && v.phase.label) {
      phaseText = v.phase.label + (v.phase.total ? ' · ' + v.phase.index + '/' + v.phase.total : '');
    }
    var attList = Array.isArray(v.attention) ? v.attention : [];
    var wsList = Array.isArray(v.workstreams) ? v.workstreams : [];
    var riskList = Array.isArray(v.risks) ? v.risks : [];
    var msList = Array.isArray(v.milestones) ? v.milestones : [];
    var memberCount = Array.isArray(m.members) ? m.members.length : wsList.length;

    var html = '<div class="hero"><div class="hero-top"><p class="headline">' + esc(v.headline || 'Kube-Agents Fleet Overview') + '</p>';
    if (phaseText) {
      html += '<span class="phase-pill">' + esc(phaseText) + '</span>';
    }
    html += '</div>';
    html += '<div class="kpi-row">' +
      '<div class="kpi"><div class="kpi-val">' + esc(memberCount) + '</div><div class="kpi-lbl">Active Agents</div></div>' +
      '<div class="kpi"><div class="kpi-val">' + esc(wsList.length) + '</div><div class="kpi-lbl">Workstreams</div></div>' +
      '<div class="kpi"><div class="kpi-val">' + esc(attList.length) + '</div><div class="kpi-lbl">Attention</div></div>' +
      '<div class="kpi"><div class="kpi-val">' + esc(msList.length) + '</div><div class="kpi-lbl">Milestones</div></div>' +
      '</div></div>';

    if (attList.length) {
      html += '<div class="section-title">Needs Your Attention</div><div class="card-list">';
      attList.forEach(function(a) {
        var btns = (a.actions || []).map(renderActionBtn).join('');
        var idAttr = a.id ? ' data-narrator-id="' + esc(a.id) + '"' : '';
        html += '<div class="card"' + idAttr + '><div class="card-head"><span>' + esc(a.title) + '</span><span class="badge ' + esc(a.kind) + '">' + esc(a.kind) + '</span></div>' +
          (a.why ? '<div class="card-note">' + esc(a.why) + '</div>' : '') +
          '<div class="card-foot">' + renderRefs(a.refs) + (btns ? '<span class="btn-group">' + btns + '</span>' : '') + '</div>' +
          '</div>';
      });
      html += '</div>';
    }

    if (wsList.length) {
      html += '<div class="section-title">Active Fleet Workstreams</div><div class="card-list">';
      wsList.forEach(function(w) {
        var btns = (w.actions || []).map(renderActionBtn).join('');
        var owners = Array.isArray(w.owners) && w.owners.length ? '@' + w.owners.join(', @') : '';
        var idAttr = w.id ? ' data-narrator-id="' + esc(w.id) + '"' : '';
        html += '<div class="card"' + idAttr + '><div class="card-head"><span>' + esc(w.title) + '</span><span class="badge ' + esc(w.state) + '">' + esc(w.state) + '</span></div>' +
          '<div class="card-note">' + (owners ? '<b>' + esc(owners) + '</b> · ' : '') + esc(w.note || '') + '</div>' +
          '<div class="card-foot">' + renderRefs(w.refs) + (btns ? '<span class="btn-group">' + btns + '</span>' : '') + '</div>' +
          '</div>';
      });
      html += '</div>';
    }

    if (riskList.length) {
      html += '<div class="section-title">Cluster Risks &amp; Blockers</div><div class="card-list">';
      riskList.forEach(function(r) {
        var btns = (r.actions || []).map(renderActionBtn).join('');
        var idAttr = r.id ? ' data-narrator-id="' + esc(r.id) + '"' : '';
        html += '<div class="card"' + idAttr + '><div class="card-head"><span>' + esc(r.text) + '</span></div>' +
          '<div class="card-foot">' + renderRefs(r.refs) + (btns ? '<span class="btn-group">' + btns + '</span>' : '') + '</div></div>';
      });
      html += '</div>';
    }

    if (msList.length) {
      html += '<div class="section-title">Recent Blackboard Topics &amp; Tasks</div><div class="card-list">';
      msList.forEach(function(ms) {
        var btns = (ms.actions || []).map(renderActionBtn).join('');
        var idAttr = ms.id ? ' data-narrator-id="' + esc(ms.id) + '"' : '';
        html += '<div class="card"' + idAttr + '><div class="card-head"><span>' + esc(ms.text) + '</span></div>' +
          '<div class="card-foot">' + renderRefs(ms.refs) + (btns ? '<span class="btn-group">' + btns + '</span>' : '') + '</div></div>';
      });
      html += '</div>';
    }

    document.getElementById('root').innerHTML = html;
  });
</script>
`
}
