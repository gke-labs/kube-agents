package lib

import "fmt"

// The profile dispatcher's hand-off to warm workers and the per-task
// liveness lease (#3655; #3660, "Admission and holding"). Workers have no
// fetch grant on the shared durables: the dispatcher holds each submission
// and hands it to one worker on that worker's own subject, and the worker
// answers on a subject only it may publish, so the dispatcher can take the
// subject's pod token as the answer's identity and never a name in the
// payload. A worker renews the liveness lease for each task it runs by
// publishing on its own lease subject (DispatchLeaseSubject), and the
// dispatcher, the bucket's only writer, persists it under a key that starts
// with that (profile, tier, pod). Workers hold no grant on the bucket at all:
// a KV bucket allows rollup headers, so a put grant on any key would let a
// worker purge every lease in it. After a restart the dispatcher lists a
// (profile, tier)'s keys to rebuild which workers are busy.

// ProfileLeaseBucket is the KV bucket the leases live in.
const ProfileLeaseBucket = "PROFILE_LEASES"

// DispatchSubject is where the dispatcher hands a task to one worker pod of
// a (profile, tier) pool.
func DispatchSubject(profile, tier, pod string) string {
	return fmt.Sprintf("a2a.dispatch.%s.%s.%s", profile, tier, pod)
}

// DispatchAcceptSubject is where that worker answers.
func DispatchAcceptSubject(profile, tier, pod string) string {
	return DispatchSubject(profile, tier, pod) + ".accept"
}

// DispatchLeaseSubject is where that worker renews the leases of the tasks
// it runs, task id in the payload.
func DispatchLeaseSubject(profile, tier, pod string) string {
	return DispatchSubject(profile, tier, pod) + ".lease"
}

// ProfileLeaseKey is a task's liveness lease as the dispatcher persists it:
// <profile>.<tier>.<pod>.<taskId>.
func ProfileLeaseKey(profile, tier, pod, taskID string) string {
	return fmt.Sprintf("%s.%s.%s.%s", profile, tier, pod, taskID)
}

// ProfileLeaseSubject is the KV subject a lease key is written on.
func ProfileLeaseSubject(key string) string {
	return "$KV." + ProfileLeaseBucket + "." + key
}
