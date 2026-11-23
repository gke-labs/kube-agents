package gateway

import "github.com/gke-labs/kube-agents/a2a/lib"

// taskIsProfile reports whether taskID, read under addressee, is a task
// addressed to an AgentProfile, whose supervisor is the dispatcher rather than
// this gateway (gke-labs/kube-agents#5301). It decides how a read treats a
// five-token supervisor terminal once the install has a cutoff
// (lib.AcceptsSupervisorEvent), so it is answered per task, never from the
// addressee's name, which a session and a profile can share:
//
//   - a task on the record is a profile task exactly when its TaskRef has a
//     Tier (startTaskWith writes it from the route's tier, set only for a
//     profile);
//   - the conversation's card-stop control is published on its addressee's
//     .in, never submitted to a dispatcher, so it is not one;
//   - a task the record does not hold falls back to the profile directory:
//     an addressee with a live card is a profile.
//
// A directory that can't answer (not loaded, or the profile's card not yet
// watched in) reads the task as a session task: this fails open, where the
// MCP server fails closed (mcp's refIsProfile). The record names every
// profile task this gateway started by its tier, so the fallback meets only a
// task the record doesn't hold, and the gateway acts on what it reads (busy
// checks, heals, stop trees, probes): a task wrongly read as unfinished here
// is acted on, not just shown, so the fallback keeps the gateway terminal, as
// every read did before the cutoff. The MCP server only reads, and knows
// nothing of the kind of most tasks it reads, so it errs toward showing a
// task unfinished, which a later read corrects.
func (g *Gateway) taskIsProfile(rec *SessionRecord, taskID, addressee string) bool {
	if rec != nil {
		if rec.CardStop != nil && rec.CardStop.TaskID == taskID {
			return false
		}
		if ref, ok := rec.TaskRefFor(taskID); ok {
			return ref.Tier != ""
		}
	}
	_, ok := g.profiles.lookup(addressee)
	return ok
}

// profileRead is the read option for taskID on rec (taskIsProfile).
func (g *Gateway) profileRead(rec *SessionRecord, taskID, addressee string) lib.ReadOption {
	return lib.ProfileTaskIf(g.taskIsProfile(rec, taskID, addressee))
}
