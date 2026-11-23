package hermesbridge

import "github.com/gke-labs/kube-agents/a2a/lib/answerscrub"

// What of an answer reaches TASKS is answerscrub.Scrub's output: the
// task's seen values (answerscrub.Seen) exact-matched out, then the shape
// rules (answerscrub.ScrubInline).
//
// The bridge sees a tool's result only through the activity door: hermes's
// post_tool_call delivery carries the tool's whole result (extra.result).
// Under the cli executor that is the bridge's own child; under the api
// executor the pod-wide hook, so the coordinator session and the worker
// sessions of the cards the task filed or tracks, whether or not the turn
// is waiting on them yet (cardWorkerRun). The set lives on the task's
// activityState and is cleared when the task finalizes.

// scrubAnswer is answerscrub.Scrub with the run's own seen values.
func scrubAnswer(run *taskRun, text string) string {
	var seen *answerscrub.Seen
	if a := run.act.Load(); a != nil {
		seen = &a.secrets
	}
	return answerscrub.Scrub(text, seen)
}
