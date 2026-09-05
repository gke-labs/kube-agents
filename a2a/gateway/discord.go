package gateway

import (
	"context"
	"fmt"
	"log/slog"
	"strings"
	"sync"

	"github.com/bwmarrin/discordgo"
)

const (
	// threadAutoArchiveMinutes is Discord's 24h auto-archive tier; an
	// archived thread reopens on the next message, so sessions survive it.
	threadAutoArchiveMinutes = 1440
	// threadMembersPage is Discord's ThreadMembers page-size ceiling; the
	// roster past one page is reported rosterComplete=false, not paged.
	threadMembersPage = 100
	// threadNameCap keeps minted thread names under Discord's 100-char
	// limit with headroom for the ellipsis.
	threadNameCap = 80
)

// DiscordAdapter is the test backend: the only one of the three with no
// approval gate — we own the server — and outbound-websocket-only, so no
// inbound endpoint on the dev cluster. Its identity mapping table is a
// feature, not a compromise: it keeps a toy backend structurally incapable
// of asserting a real principal (gateway design, test-backend section).
type DiscordAdapter struct {
	s   *discordgo.Session
	log *slog.Logger

	mu       sync.Mutex
	botRoles map[string][]string // guildID -> role ids the bot holds
}

// botRoleIDs returns the bot's own role ids in guildID, cached for the life
// of the process (a pod roll refreshes it). Discord's mention picker offers
// the bot's managed role under the same name as the bot, and the two render
// identically in chat - so a mention of a role the bot holds addresses the
// bot. A failed lookup is not cached; the next message retries.
func (d *DiscordAdapter) botRoleIDs(s *discordgo.Session, guildID string) []string {
	d.mu.Lock()
	defer d.mu.Unlock()
	if ids, ok := d.botRoles[guildID]; ok {
		return ids
	}
	member, err := s.State.Member(guildID, s.State.User.ID)
	if err != nil || member == nil {
		member, err = s.GuildMember(guildID, s.State.User.ID)
		if err != nil {
			d.log.Warn("bot member lookup failed; role mentions will not resolve", "guild", guildID, "err", err)
			return nil
		}
	}
	if d.botRoles == nil {
		d.botRoles = map[string][]string{}
	}
	d.botRoles[guildID] = member.Roles
	return member.Roles
}

// NewDiscordAdapter dials the Discord gateway websocket.
func NewDiscordAdapter(token string, log *slog.Logger) (*DiscordAdapter, error) {
	s, err := discordgo.New("Bot " + token)
	if err != nil {
		return nil, fmt.Errorf("discord session: %w", err)
	}
	// MessageContent is a privileged intent; it must be enabled on the bot in
	// the developer portal (W0's server setup).
	s.Identify.Intents = discordgo.IntentsGuilds |
		discordgo.IntentsGuildMessages |
		discordgo.IntentsDirectMessages |
		discordgo.IntentMessageContent
	return &DiscordAdapter{s: s, log: log}, nil
}

// conversationID is the backend-qualified session key. A channel or space is
// not a session; a conversation in it is — so the id is the thread or DM
// channel, guild-qualified where one exists.
func conversationID(guildID, channelID string) string {
	if guildID == "" {
		return "discord:dm/" + channelID
	}
	return fmt.Sprintf("discord:%s/%s", guildID, channelID)
}

// channelFromConversation inverts conversationID for the adapter's own use.
func channelFromConversation(conversation string) string {
	rest, ok := strings.CutPrefix(conversation, "discord:")
	if !ok {
		return ""
	}
	if dm, ok := strings.CutPrefix(rest, "dm/"); ok {
		return dm
	}
	_, channel, ok := strings.Cut(rest, "/")
	if !ok {
		return ""
	}
	return channel
}

// Run registers the message handler and holds the websocket open until ctx
// is done.
func (d *DiscordAdapter) Run(ctx context.Context, handler func(InboundMessage)) error {
	remove := d.s.AddHandler(func(s *discordgo.Session, m *discordgo.MessageCreate) {
		msg, ok := d.inbound(s, m)
		if ok {
			handler(msg)
		}
	})
	defer remove()
	if err := d.s.Open(); err != nil {
		return fmt.Errorf("discord open: %w", err)
	}
	// Open can succeed without a READY packet, leaving State.User nil.
	user := "(identity pending)"
	if d.s.State.User != nil {
		user = d.s.State.User.Username
	}
	d.log.Info("discord connected", "user", user)
	<-ctx.Done()
	return d.s.Close()
}

// inbound normalizes one MessageCreate. The sender id is whatever Discord's
// authenticated websocket delivered — the backend's own identity mechanism;
// mapping it to a principal (or dropping it) is the session manager's job.
// Scope: DMs and threads carry every message. A non-thread guild channel is
// NOT a session — "a channel or space is not a session; a conversation in
// it is" — so a bot mention there starts a thread from that message and the
// session binds to the thread. Two people mentioning the bot in one channel
// get two conversations, never one shared task they steer by accident.
func (d *DiscordAdapter) inbound(s *discordgo.Session, m *discordgo.MessageCreate) (InboundMessage, bool) {
	if s.State.User == nil {
		return InboundMessage{}, false // no READY yet; we can't even self-filter
	}
	if m.Author == nil || m.Author.Bot || m.Author.ID == s.State.User.ID {
		return InboundMessage{}, false
	}
	kind := "group"
	isDM := m.GuildID == ""
	if isDM {
		kind = "dm"
	}
	text := strings.TrimSpace(m.Content)
	channelID := m.ChannelID
	if !isDM {
		ch, err := s.State.Channel(m.ChannelID)
		if err != nil || ch == nil {
			ch, err = s.Channel(m.ChannelID)
			if err != nil {
				d.log.Warn("channel lookup failed", "channel", m.ChannelID, "err", err)
				return InboundMessage{}, false
			}
		}
		if !ch.IsThread() {
			mentioned := false
			for _, u := range m.Mentions {
				if u.ID == s.State.User.ID {
					mentioned = true
					break
				}
			}
			// A role mention counts when the role is one the bot holds:
			// "@kage" via the picker frequently resolves to the managed
			// role, not the user, and both render identically.
			var mentionedRoles []string
			if len(m.MentionRoles) > 0 {
				for _, held := range d.botRoleIDs(s, m.GuildID) {
					for _, r := range m.MentionRoles {
						if r == held {
							mentionedRoles = append(mentionedRoles, r)
						}
					}
				}
			}
			if len(mentionedRoles) > 0 {
				mentioned = true
			}
			if !mentioned {
				return InboundMessage{}, false
			}
			text = stripMention(text, s.State.User.ID)
			for _, r := range mentionedRoles {
				text = strings.TrimSpace(strings.ReplaceAll(text, "<@&"+r+">", ""))
			}
			if text == "" {
				// A bare mention has nothing to run. Bail before the thread
				// spawns - an empty session thread that never gets a reply
				// is the "dropped message" shape this adapter exists to
				// avoid.
				return InboundMessage{}, false
			}
			thread, err := s.MessageThreadStartComplex(m.ChannelID, m.ID, &discordgo.ThreadStart{
				Name:                threadName(text),
				AutoArchiveDuration: threadAutoArchiveMinutes,
			})
			if err != nil {
				d.log.Error("thread start failed; channel messages cannot become sessions", "channel", m.ChannelID, "err", err)
				_, _ = s.ChannelMessageSend(m.ChannelID, "I work in threads — start one (or grant me Create Public Threads) and ask there.")
				return InboundMessage{}, false
			}
			channelID = thread.ID
		}
	}
	if text == "" {
		return InboundMessage{}, false
	}
	return InboundMessage{
		Conversation: conversationID(m.GuildID, channelID),
		Kind:         kind,
		AuthorID:     m.Author.ID,
		MessageID:    m.ID,
		Text:         text,
	}, true
}

// threadName derives a thread title from the ask; Discord caps names at 100.
func threadName(text string) string {
	name := strings.TrimSpace(text)
	if name == "" {
		name = "a2a session"
	}
	return truncateRunes(name, threadNameCap)
}

func stripMention(text, botID string) string {
	for _, form := range []string{"<@" + botID + ">", "<@!" + botID + ">"} {
		text = strings.ReplaceAll(text, form, "")
	}
	return strings.TrimSpace(text)
}

func (d *DiscordAdapter) Post(conversation, text string) (string, error) {
	channel := channelFromConversation(conversation)
	if channel == "" {
		return "", fmt.Errorf("malformed conversation id %q", conversation)
	}
	msg, err := d.s.ChannelMessageSend(channel, text)
	if err != nil {
		return "", err
	}
	return msg.ID, nil
}

func (d *DiscordAdapter) Edit(conversation, messageID, text string) error {
	channel := channelFromConversation(conversation)
	if channel == "" {
		return fmt.Errorf("malformed conversation id %q", conversation)
	}
	_, err := d.s.ChannelMessageEdit(channel, messageID, text)
	return err
}

// Roster reads membership from the backend's membership API: thread members
// for threads, recipients for DMs. A plain guild channel would need a
// permission walk over the member list — live-read territory for the LCD
// tool — so it reports incomplete instead of guessing.
func (d *DiscordAdapter) Roster(conversation string) ([]string, bool, error) {
	channel := channelFromConversation(conversation)
	if channel == "" {
		return nil, false, fmt.Errorf("malformed conversation id %q", conversation)
	}
	ch, err := d.s.State.Channel(channel)
	if err != nil || ch == nil {
		ch, err = d.s.Channel(channel)
		if err != nil {
			return nil, false, err
		}
	}
	switch {
	case ch.Type == discordgo.ChannelTypeDM || ch.Type == discordgo.ChannelTypeGroupDM:
		ids := make([]string, 0, len(ch.Recipients)+1)
		for _, u := range ch.Recipients {
			ids = append(ids, u.ID)
		}
		return ids, true, nil
	case ch.IsThread():
		members, err := d.s.ThreadMembers(channel, threadMembersPage, false, "")
		if err != nil {
			return nil, false, err
		}
		ids := make([]string, 0, len(members))
		for _, m := range members {
			ids = append(ids, m.UserID)
		}
		// One page covers the test rooms; a thread past it is incomplete.
		return ids, len(members) < threadMembersPage, nil
	default:
		return nil, false, nil
	}
}

// OpenDirect returns the DM conversation for a user — the DM-switch
// primitive. Shipped, unused: everything posts to the room it came from
// until the classifier exists.
func (d *DiscordAdapter) OpenDirect(userID string) (string, error) {
	ch, err := d.s.UserChannelCreate(userID)
	if err != nil {
		return "", err
	}
	return conversationID("", ch.ID), nil
}
