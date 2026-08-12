// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package main

import (
	"testing"
	"time"
)

func TestClassifyPullFailure(t *testing.T) {
	tests := []struct {
		name    string
		message string
		want    pullClass
	}{
		// The first two are captured verbatim from GKE v1.36.2-gke.2064000
		// (containerd) rather than transcribed, because the markers are matched
		// against wording no API guarantees. Whatever else changes here, these
		// two must keep classifying in opposite directions: they are the same
		// Event.Reason on the same cluster minutes apart.
		{
			name:    "live: artifact registry repository does not exist",
			message: `Failed to pull image "us-docker.pkg.dev/gke-demos-345619/does-not-exist/nope:v1": failed to pull and unpack image "us-docker.pkg.dev/gke-demos-345619/does-not-exist/nope:v1": failed to resolve reference "us-docker.pkg.dev/gke-demos-345619/does-not-exist/nope:v1": failed to authorize: failed to fetch oauth token: unexpected status from GET request to https://us-docker.pkg.dev/v2/token?scope=repository%3Agke-demos-345619%2Fdoes-not-exist%2Fnope%3Apull&service=us-docker.pkg.dev: 404 Not Found`,
			want:    pullClassTerminal,
		},
		{
			name:    "live: registry unreachable",
			message: `Failed to pull image "10.255.255.1:5000/app/nope:v1": rpc error: code = DeadlineExceeded desc = failed to pull and unpack image "10.255.255.1:5000/app/nope:v1": failed to resolve reference "10.255.255.1:5000/app/nope:v1": failed to do request: Head "https://10.255.255.1:5000/v2/app/nope/manifests/v1": dial tcp 10.255.255.1:5000: i/o timeout`,
			want:    pullClassRetryable,
		},
		{
			// Three of the four events kubelet emits per pull failure look like
			// this one: a bare status with no cause in it. Without the memo this
			// is what fires, ten seconds after the cause was suppressed.
			name:    "live: bare ErrImagePull status carries no cause",
			message: "Error: ErrImagePull",
			want:    pullClassUnknown,
		},
		{
			name:    "live: bare ImagePullBackOff status carries no cause",
			message: "Error: ImagePullBackOff",
			want:    pullClassUnknown,
		},
		{
			name:    "artifact registry 429",
			message: `Failed to pull image "us-docker.pkg.dev/proj/repo/app:v1": rpc error: code = Unknown desc = failed to pull and unpack image "us-docker.pkg.dev/proj/repo/app:v1": failed to copy: httpReadSeeker: failed open: unexpected status code 429 Too Many Requests`,
			want:    pullClassRetryable,
		},
		{
			name:    "docker hub rate limit",
			message: `Failed to pull image "nginx:latest": toomanyrequests: You have reached your pull rate limit.`,
			want:    pullClassRetryable,
		},
		{
			name:    "registry 503",
			message: `Failed to pull image "example.com/app:v1": received unexpected HTTP status: 503 Service Unavailable`,
			want:    pullClassRetryable,
		},
		{
			name:    "dial timeout",
			message: `Failed to pull image "example.com/app:v1": dial tcp 10.0.0.1:443: i/o timeout`,
			want:    pullClassRetryable,
		},
		{
			name:    "tls handshake timeout",
			message: `Failed to pull image "example.com/app:v1": net/http: TLS handshake timeout`,
			want:    pullClassRetryable,
		},
		{
			name:    "bad tag",
			message: `Failed to pull image "us-docker.pkg.dev/proj/repo/app:v99": manifest unknown`,
			want:    pullClassTerminal,
		},
		{
			name:    "manifest for tag not found",
			message: `Failed to pull image "example.com/app:nope": manifest for example.com/app:nope not found`,
			want:    pullClassTerminal,
		},
		{
			name:    "access denied",
			message: `Failed to pull image "example.com/private:v1": denied: requested access to the resource is denied`,
			want:    pullClassTerminal,
		},
		{
			name:    "unauthorized",
			message: `Failed to pull image "example.com/private:v1": unauthorized: authentication required`,
			want:    pullClassTerminal,
		},
		{
			name:    "disk full",
			message: `Failed to pull image "example.com/app:v1": write /var/lib/containerd/blob: no space left on device`,
			want:    pullClassTerminal,
		},
		{
			// Terminal wins ties: a rate limit hit while retrying a tag that
			// does not exist is still a tag that does not exist.
			name:    "429 while pulling a nonexistent manifest is terminal",
			message: `Failed to pull image "example.com/app:v1": 429 Too Many Requests; manifest unknown`,
			want:    pullClassTerminal,
		},
		{
			name:    "causeless back-off carries nothing",
			message: `Back-off pulling image "example.com/app:v1"`,
			want:    pullClassUnknown,
		},
		{
			name:    "unrecognised registry wording is unknown, not held",
			message: `Failed to pull image "example.com/app:v1": the registry is having a bad day`,
			want:    pullClassUnknown,
		},
		{
			// The reason stripQuoted exists. Without it the repository path
			// reads as "denied" and a transient timeout is misclassified as a
			// permanent authorization failure.
			name:    "marker words in the repository path are not error text",
			message: `Failed to pull image "registry.example.com/denied-team/app:v1": dial tcp 10.0.0.1:443: i/o timeout`,
			want:    pullClassRetryable,
		},
		{
			// Bare HTTP-status digits are not markers, so a tag full of them is
			// inert even before stripQuoted runs.
			name:    "numeric tag is not an HTTP status",
			message: `Failed to pull image "example.com/app:1.503": manifest unknown`,
			want:    pullClassTerminal,
		},
		{
			name:    "unclassifiable message with a numeric tag stays unknown",
			message: `Back-off pulling image "example.com/app:v429"`,
			want:    pullClassUnknown,
		},
		{
			// Artifact Registry's quota error puts a 12-digit project number in
			// single quotes, which stripQuoted does not remove. A bare "429" or
			// "503" marker would eventually fire on one of those digits.
			name:    "project number digits do not classify on their own",
			message: `Failed to pull image "example.com/app:v1": rpc error for consumer 'project_number:235545413903': registry said something new`,
			want:    pullClassUnknown,
		},
		{
			name:    "empty message",
			message: "",
			want:    pullClassUnknown,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := classifyPullFailure(tc.message); got != tc.want {
				t.Errorf("classifyPullFailure(%q) = %v; want %v", tc.message, got, tc.want)
			}
		})
	}
}

const (
	throttleMsg = `Failed to pull image "example.com/app:v1": 429 Too Many Requests`
	badTagMsg   = `Failed to pull image "example.com/app:v1": manifest unknown`
	backOffMsg  = `Back-off pulling image "example.com/app:v1"`
)

// TestPullClassMemoCarriesCauseForward is the behaviour the memo exists for:
// the event naming the cause and the event backing off are different events.
func TestPullClassMemoCarriesCauseForward(t *testing.T) {
	m := newPullClassMemo(0, 0)

	if got := m.Resolve("pod-1", throttleMsg); got != pullClassRetryable {
		t.Fatalf("cause event = %v; want retryable", got)
	}
	if got := m.Resolve("pod-1", backOffMsg); got != pullClassRetryable {
		t.Errorf("causeless back-off = %v; want the remembered retryable", got)
	}
}

func TestPullClassMemoIsPerUID(t *testing.T) {
	m := newPullClassMemo(0, 0)
	m.Resolve("pod-1", throttleMsg)

	if got := m.Resolve("pod-2", backOffMsg); got != pullClassUnknown {
		t.Errorf("unrelated pod = %v; want unknown", got)
	}
}

// TestPullClassMemoTerminalSupersedes: within one pod UID the image is fixed, so
// a terminal verdict arriving after a retryable one is the retry finally
// surfacing the real problem. It must not be downgraded back.
func TestPullClassMemoTerminalSupersedes(t *testing.T) {
	m := newPullClassMemo(0, 0)
	m.Resolve("pod-1", throttleMsg)

	if got := m.Resolve("pod-1", badTagMsg); got != pullClassTerminal {
		t.Fatalf("terminal after retryable = %v; want terminal", got)
	}
	if got := m.Resolve("pod-1", throttleMsg); got != pullClassTerminal {
		t.Errorf("retryable after terminal = %v; want terminal to stick", got)
	}
	if got := m.Resolve("pod-1", backOffMsg); got != pullClassTerminal {
		t.Errorf("causeless back-off = %v; want terminal", got)
	}
}

// TestPullClassMemoExpires guards the stale-inheritance case: a pod that was
// throttled, recovered, and hours later fails for an unrelated reason must not
// inherit the old class.
func TestPullClassMemoExpires(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	m := newPullClassMemo(10*time.Minute, 0)
	m.now = func() time.Time { return now }

	m.Resolve("pod-1", throttleMsg)

	now = now.Add(11 * time.Minute)
	if got := m.Resolve("pod-1", backOffMsg); got != pullClassUnknown {
		t.Errorf("back-off after ttl = %v; want unknown", got)
	}
	if got := m.Len(); got != 0 {
		t.Errorf("expired entry left %d entries; want 0", got)
	}
}

// TestPullClassMemoIsBounded: a cluster churning through pods must not grow the
// map without limit. Every UID here is distinct and none has expired, so the cap
// is enforced by evicting the oldest.
func TestPullClassMemoIsBounded(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	m := newPullClassMemo(time.Hour, 4)
	m.now = func() time.Time { return now }

	for i := 0; i < 20; i++ {
		m.Resolve(string(rune('a'+i)), throttleMsg)
		now = now.Add(time.Second)
	}
	if got := m.Len(); got > 4 {
		t.Errorf("memo holds %d entries; want <= 4", got)
	}
}

// TestPullClassMemoNilSafe: a dispatcher built without a memo degrades to
// per-message classification rather than panicking.
func TestPullClassMemoNilSafe(t *testing.T) {
	var m *pullClassMemo

	if got := m.Resolve("pod-1", throttleMsg); got != pullClassRetryable {
		t.Errorf("nil memo = %v; want the message's own class", got)
	}
	if got := m.Resolve("pod-1", backOffMsg); got != pullClassUnknown {
		t.Errorf("nil memo carried a cause forward: %v; want unknown", got)
	}
}

// TestPullClassMemoEmptyUID: an event with no involvedObject.uid cannot be
// correlated with anything, so it must classify standalone and record nothing.
func TestPullClassMemoEmptyUID(t *testing.T) {
	m := newPullClassMemo(0, 0)

	if got := m.Resolve("", throttleMsg); got != pullClassRetryable {
		t.Errorf("empty uid = %v; want the message's own class", got)
	}
	if got := m.Len(); got != 0 {
		t.Errorf("empty uid recorded %d entries; want 0", got)
	}
}
