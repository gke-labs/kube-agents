package gateway

import (
	"context"
	"fmt"
	"slices"
	"strings"
	"time"

	"github.com/slack-go/slack"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The autonomy dial on the Home tab: what the agent may change on its own,
// drawn from the board snapshot's dial section (lib.BoardDial), which
// session-kv fills only while the install's KUBEAGENTS_DIAL is on. A missing
// section, or one not enabled, draws nothing.
//
// The level is three radio buttons (Explain, Propose fixes, Auto-merge proven
// fixes). Under it, each kind of fix shows its clean merges in one repository
// at a time, picked per viewer, and that repository's state: off, "Pending:
// rules PR #N" while the rules-file pull request is open, or on. Only a
// viewer the section lists in Admins gets the radio buttons and the Request
// or Stop button; everyone else sees the same lines read-only. That is display
// only: session-kv checks the presser on every action.
//
// A tap is no turn. It is admitted by the Home tab's rule (homeAdmitted), goes
// over the board route as a lib.BoardAction (dial.level, dial.request or
// dial.stop) the way a board-health row's Retry does (slack_board.go), and
// session-kv's one-line answer is drawn under the panel for the presser. The
// tab is then read again and republished to everyone looking at it. A repo
// pick changes only the presser's own view and sends nothing.
const (
	// slackDialActionPrefix begins every dial control's action id.
	slackDialActionPrefix = "kage_dial."
	slackDialActionLevel  = slackDialActionPrefix + "level"
	slackDialActionRepo   = slackDialActionPrefix + "repo"
	slackDialActionReq    = slackDialActionPrefix + "request"
	slackDialActionStop   = slackDialActionPrefix + "stop"
	// slackDialSeenPrefix keys a dial tap in the adapter's seen ring.
	slackDialSeenPrefix = "dial:"
	// slackDialFieldSeparator joins a row's kind, environment and repository
	// key in a control's value; none of the three holds it.
	slackDialFieldSeparator = "|"
	// slackDialTimeout bounds a tap's round trip, the board action's.
	slackDialTimeout = slackBoardTimeout
	// slackDialFollowUps are when the tab is read again after a Request or
	// Stop: session-kv answers at once and runs the switch script on its own,
	// so the row turns pending, or gets a note, seconds to a minute later.
	slackDialFollowUpFirst = 20 * time.Second
	slackDialFollowUpLast  = 75 * time.Second
	// slackDialKindsMax and slackDialReposMax keep the panel inside the Home
	// tab's 100-block limit and a select's 100 options.
	slackDialKindsMax = 6
	slackDialReposMax = 50
	// slackDialOptionMax keeps a select option's text, and firstLine's "…",
	// inside Slack's 75 characters; slackDialValueMax is Slack's cap on an
	// option's value, past which views.publish refuses the whole tab.
	slackDialOptionMax = 74
	slackDialValueMax  = 150

	slackDialTitle      = "Autonomy dial"
	slackDialIntro      = "What the agent may change on its own."
	slackDialLevelNow   = "*Level:* %s"
	slackDialSetBy      = "Set to %s by <@%s> %s"
	slackDialNoRepos    = "no fixes of this kind yet"
	slackDialRepoPrompt = "Repository"
	slackDialProgress   = "%d of %d clean merges"
	slackDialDaysShort  = "%d of %d clean merges, on %d of %d separate days"
	slackDialStateOff   = "off"
	slackDialStateOn    = "auto-merges"
	slackDialPending    = "Pending: <%s|rules PR #%d>"
	slackDialPendingNo  = "Pending: rules PR"
	slackDialRequest    = "Request auto-merge"
	slackDialStop       = "Stop"
	slackDialFailed     = "Couldn't reach the agent just now. Try again."
	slackDialNotAdmin   = "Only an install admin can change the dial."
	slackDialDone       = "Done."
)

// slackDialLevels are the radio buttons, in order: the level and its label
// and description.
var slackDialLevels = []struct{ level, title, description string }{
	{lib.DialLevelExplain, "Explain", "Finds and explains; changes nothing"},
	{lib.DialLevelPropose, "Propose fixes", "Opens a pull request; your team merges"},
	{lib.DialLevelAutomerge, "Auto-merge proven fixes", "Your pipeline merges proven fixes"},
}

// slackDialLevelTitle is a level's label, or the level as written when it is
// not one of the three.
func slackDialLevelTitle(level string) string {
	for _, l := range slackDialLevels {
		if l.level == level {
			return l.title
		}
	}
	return slackEscaper.Replace(level)
}

// slackDialRowValue is a row's kind, environment and repository key in one
// control value.
func slackDialRowValue(kind, environment, repoKey string) string {
	return strings.Join([]string{kind, environment, repoKey}, slackDialFieldSeparator)
}

// slackDialRow splits a control value back into its row; ok is false for one
// the panel did not draw.
func slackDialRow(value string) (kind, environment, repoKey string, ok bool) {
	parts := strings.Split(value, slackDialFieldSeparator)
	if len(parts) != 3 || parts[0] == "" || parts[2] == "" {
		return "", "", "", false
	}
	return parts[0], parts[1], parts[2], true
}

// slackDialRepoKey is a viewer's pick for one kind in one environment.
func slackDialRepoKey(kind, environment string) string {
	return kind + slackDialFieldSeparator + environment
}

// slackDialProgressLine is a repository's clean merges as one line: capped at
// the target, and naming the days when the merges are enough but the days
// are not.
func slackDialProgressLine(r lib.BoardDialRepo) string {
	clean := min(max(r.Clean, 0), r.Needed)
	if r.Needed > 0 && r.Clean >= r.Needed && !r.Ready && r.DaysNeeded > 0 {
		return fmt.Sprintf(slackDialDaysShort, clean, r.Needed, min(r.Days, r.DaysNeeded), r.DaysNeeded)
	}
	return fmt.Sprintf(slackDialProgress, clean, r.Needed)
}

// slackDialStateLine is a repository's state for its kind.
func slackDialStateLine(r lib.BoardDialRepo) string {
	switch r.State {
	case lib.DialStateOn:
		return slackDialStateOn
	case lib.DialStatePending:
		if r.PendingPR != nil && r.PendingPR.Number > 0 && strings.HasPrefix(r.PendingPR.URL, slackHomeHTTPS) {
			return fmt.Sprintf(slackDialPending, r.PendingPR.URL, r.PendingPR.Number)
		}
		return slackDialPendingNo
	default:
		return slackDialStateOff
	}
}

// homeDialBlocks draws the dial for member, or nothing when the snapshot has
// no enabled dial.
func (s *SlackAdapter) homeDialBlocks(member string, d *lib.BoardDial) []slack.Block {
	if d == nil || !d.Enabled {
		return nil
	}
	admin := slices.Contains(d.Admins, member)
	mrk := func(text string) *slack.TextBlockObject {
		return slack.NewTextBlockObject(slack.MarkdownType, text, false, false)
	}
	plain := func(text string) *slack.TextBlockObject {
		return slack.NewTextBlockObject(slack.PlainTextType, text, false, false)
	}
	blocks := []slack.Block{
		slack.NewDividerBlock(),
		slack.NewHeaderBlock(plain(slackDialTitle)),
	}
	if admin {
		var options []*slack.OptionBlockObject
		var initial *slack.OptionBlockObject
		for _, l := range slackDialLevels {
			o := slack.NewOptionBlockObject(l.level, mrk("*"+l.title+"*"), plain(l.description))
			options = append(options, o)
			if l.level == d.Level {
				initial = o
			}
		}
		radio := slack.NewRadioButtonsBlockElement(slackDialActionLevel, options...)
		radio.InitialOption = initial
		blocks = append(blocks, slack.NewSectionBlock(mrk(slackDialIntro), nil, slack.NewAccessory(radio)))
	} else {
		blocks = append(blocks, slack.NewSectionBlock(mrk(fmt.Sprintf(slackDialLevelNow, slackDialLevelTitle(d.Level))), nil, nil))
	}
	if d.SetBy != "" && d.SetAt > 0 {
		blocks = append(blocks, slack.NewContextBlock("", mrk(fmt.Sprintf(slackDialSetBy,
			slackDialLevelTitle(d.Level), slackEscaper.Replace(d.SetBy), slackDate(time.Unix(d.SetAt, 0), slackDateDayTime)))))
	}
	for i, k := range d.Kinds {
		if i == slackDialKindsMax {
			break
		}
		blocks = append(blocks, s.homeDialKind(member, admin, k)...)
	}
	if status := s.dialStatus(member); status != "" {
		blocks = append(blocks, slack.NewContextBlock("", mrk(status)))
	}
	return blocks
}

// homeDialKind is one kind's rows: its title, the repository picker when it
// has more than one, the picked repository's progress and state, and for an
// admin its Request or Stop.
func (s *SlackAdapter) homeDialKind(member string, admin bool, k lib.BoardDialKind) []slack.Block {
	mrk := func(text string) *slack.TextBlockObject {
		return slack.NewTextBlockObject(slack.MarkdownType, text, false, false)
	}
	plain := func(text string) *slack.TextBlockObject {
		return slack.NewTextBlockObject(slack.PlainTextType, text, false, false)
	}
	title := "*" + slackEscaper.Replace(k.Title) + "*"
	if k.Environment != "" {
		title += " (" + slackEscaper.Replace(k.Environment) + ")"
	}
	// A repository key too long for a Slack value is left out of the whole
	// row, picker and button alike, rather than blanking the tab.
	var repos []lib.BoardDialRepo
	for _, r := range k.Repos {
		if len(repos) < slackDialReposMax && len(slackDialRowValue(k.Kind, k.Environment, r.RepoKey)) <= slackDialValueMax {
			repos = append(repos, r)
		}
	}
	if len(repos) == 0 {
		return []slack.Block{slack.NewSectionBlock(mrk(title+slackHomeDot+slackDialNoRepos), nil, nil)}
	}
	picked := repos[0]
	if key := s.dialPick(member, k.Kind, k.Environment); key != "" {
		for _, r := range repos {
			if r.RepoKey == key {
				picked = r
				break
			}
		}
	}
	var accessory *slack.Accessory
	if len(repos) > 1 {
		var options []*slack.OptionBlockObject
		var initial *slack.OptionBlockObject
		for _, r := range repos {
			value := slackDialRowValue(k.Kind, k.Environment, r.RepoKey)
			o := slack.NewOptionBlockObject(value, plain(firstLine(slackDialRepoName(r), slackDialOptionMax)), nil)
			options = append(options, o)
			if r.RepoKey == picked.RepoKey {
				initial = o
			}
		}
		sel := slack.NewOptionsSelectBlockElement(slack.OptTypeStatic, plain(slackDialRepoPrompt), slackDialActionRepo, options...)
		sel.InitialOption = initial
		accessory = slack.NewAccessory(sel)
	}
	blocks := []slack.Block{slack.NewSectionBlock(mrk(title), nil, accessory)}
	blocks = append(blocks, slack.NewContextBlock("", mrk(strings.Join([]string{
		slackEscaper.Replace(slackDialRepoName(picked)), slackDialProgressLine(picked), slackDialStateLine(picked),
	}, slackHomeDot))))
	if note := strings.TrimSpace(picked.Note); note != "" {
		blocks = append(blocks, slack.NewContextBlock("", mrk(slackEscaper.Replace(firstLine(note, slackHomeLineMax)))))
	}
	if admin {
		id, label := slackDialActionReq, slackDialRequest
		if picked.State == lib.DialStateOn || picked.State == lib.DialStatePending {
			id, label = slackDialActionStop, slackDialStop
		}
		btn := slack.NewButtonBlockElement(id, slackDialRowValue(k.Kind, k.Environment, picked.RepoKey), plain(label))
		if id == slackDialActionStop {
			btn.Style = slack.StyleDanger
		}
		blocks = append(blocks, slack.NewActionBlock("", btn))
	}
	return blocks
}

// slackDialRepoName is how a repository is named on the panel: its name as
// session-kv sent it, else its key.
func slackDialRepoName(r lib.BoardDialRepo) string {
	if r.Repo != "" {
		return r.Repo
	}
	return r.RepoKey
}

// dialPick is member's picked repository for one kind, or "".
func (s *SlackAdapter) dialPick(member, kind, environment string) string {
	s.home.mu.Lock()
	defer s.home.mu.Unlock()
	return s.home.dialPicks[member][slackDialRepoKey(kind, environment)]
}

func (s *SlackAdapter) setDialPick(member, kind, environment, repoKey string) {
	s.home.mu.Lock()
	defer s.home.mu.Unlock()
	// A pick is a sign the member is looking: count them as a viewer, so the
	// prune in the redraw keeps the pick even when the tab was opened more
	// than slackHomeViewerTTL ago or before a gateway restart.
	if s.home.viewers == nil {
		s.home.viewers = map[string]time.Time{}
	}
	if _, ok := s.home.viewers[member]; ok || len(s.home.viewers) < slackHomeViewersCap {
		s.home.viewers[member] = time.Now()
	}
	if s.home.dialPicks == nil {
		s.home.dialPicks = map[string]map[string]string{}
	}
	if s.home.dialPicks[member] == nil {
		if len(s.home.dialPicks) >= slackHomeViewersCap {
			return
		}
		s.home.dialPicks[member] = map[string]string{}
	}
	s.home.dialPicks[member][slackDialRepoKey(kind, environment)] = repoKey
}

// dialStatus is the answer to member's last dial tap, while it is recent.
func (s *SlackAdapter) dialStatus(member string) string {
	s.home.mu.Lock()
	defer s.home.mu.Unlock()
	st, ok := s.home.dialStatuses[member]
	if !ok || time.Since(st.at) > slackHomeViewerTTL {
		delete(s.home.dialStatuses, member)
		return ""
	}
	return st.text
}

func (s *SlackAdapter) setDialStatus(member, text string) {
	s.home.mu.Lock()
	defer s.home.mu.Unlock()
	if s.home.dialStatuses == nil {
		s.home.dialStatuses = map[string]slackHomePickStatus{}
	}
	s.home.dialStatuses[member] = slackHomePickStatus{text: text, at: time.Now()}
}

// dialClick takes one dial tap off the event pump. A tap that is not a Home
// tab action, has no presser or timestamp, or was seen already is dropped.
func (s *SlackAdapter) dialClick(p slackClickPayload, actionID, value, selected string) {
	member, actionTS := p.User.ID, p.ActionTS
	if p.Channel.ID != "" || p.Container.Type != slackContainerView || member == "" || actionTS == "" {
		s.log.Info("slack: dropping a dial tap that is not a Home tab action", "member", member, "channel", p.Channel.ID, "container", p.Container.Type)
		return
	}
	if s.alreadySeen(slackDialSeenPrefix + member + "/" + actionTS) {
		return
	}
	var action lib.BoardAction
	switch actionID {
	case slackDialActionRepo:
		kind, environment, repoKey, ok := slackDialRow(selected)
		if !ok {
			s.log.Info("slack: dropping a dial repo pick with no row", "member", member)
			return
		}
		s.setDialPick(member, kind, environment, repoKey)
		s.goSession(func() { s.publishHome(member, false) })
		return
	case slackDialActionLevel:
		if !slices.ContainsFunc(slackDialLevels, func(l struct{ level, title, description string }) bool { return l.level == selected }) {
			s.log.Info("slack: dropping a dial level tap with no level", "member", member, "level", selected)
			return
		}
		action = lib.BoardAction{Action: lib.DialActionLevel, Level: selected}
	case slackDialActionReq, slackDialActionStop:
		kind, environment, repoKey, ok := slackDialRow(value)
		if !ok {
			s.log.Info("slack: dropping a dial tap with no row", "member", member, "actionId", actionID)
			return
		}
		verb := lib.DialActionRequest
		if actionID == slackDialActionStop {
			verb = lib.DialActionStop
		}
		action = lib.BoardAction{Action: verb, Kind: kind, Environment: environment, RepoKey: repoKey}
	default:
		s.log.Info("slack: dropping a dial tap with an unknown action id", "member", member, "actionId", actionID)
		return
	}
	s.mu.Lock()
	actor := s.boardActor
	s.mu.Unlock()
	if actor == nil {
		s.log.Warn("slack: dropping a dial tap: no board route is wired", "member", member)
		return
	}
	action.User = member
	s.goSession(func() { s.runDialTap(member, action, actor) })
}

// runDialTap admits the presser, sends the action and redraws the tab: for
// the presser with session-kv's answer, then for everyone looking at it.
func (s *SlackAdapter) runDialTap(member string, action lib.BoardAction, actor BoardActor) {
	if admitted, decided := s.homeAdmitted(member); !admitted || !decided {
		s.log.Info("slack: ignoring a dial tap from a member the gateway would not answer", "member", member, "decided", decided)
		if decided {
			s.publishHome(member, false)
		}
		return
	}
	if board, err := s.homeBoard(); err == nil && board != nil && board.Dial != nil && !slices.Contains(board.Dial.Admins, member) {
		// The controls were drawn for an admin who no longer is one, or the
		// payload was not ours. session-kv would refuse it too.
		s.setDialStatus(member, slackDialNotAdmin)
		s.publishHome(member, false)
		return
	}
	action.By = s.slackUserName(member)
	ctx, cancel := context.WithTimeout(context.Background(), slackDialTimeout)
	defer cancel()
	res, err := actor(ctx, action)
	line := slackDialAnswer(res, err)
	s.log.Info("slack: dial tap", "action", action.Action, "member", member, "level", action.Level,
		"kind", action.Kind, "environment", action.Environment, "repo", action.RepoKey, "ok", res != nil && res.OK, "err", err)
	s.setDialStatus(member, line)
	s.home.mu.Lock()
	s.home.cached, s.home.readErr = nil, nil
	s.home.mu.Unlock()
	s.publishHome(member, false)
	s.homeChanged(true)
	if action.Action != lib.DialActionLevel {
		for _, after := range []time.Duration{slackDialFollowUpFirst, slackDialFollowUpLast} {
			time.AfterFunc(after, func() { s.homeChanged(true) })
		}
	}
}

// slackDialAnswer is the presser's line: session-kv's message, escaped, or
// an ask to try again when nothing came back.
func slackDialAnswer(res *lib.BoardActionResult, err error) string {
	if err != nil || res == nil || res.Error != "" {
		return slackDialFailed
	}
	if msg := strings.TrimSpace(res.Message); msg != "" {
		return slackEscaper.Replace(firstLine(msg, slackHomeLineMax))
	}
	if res.OK {
		return slackDialDone
	}
	return slackDialFailed
}
