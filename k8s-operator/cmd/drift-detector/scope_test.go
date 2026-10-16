// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//	http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// writeScopeProfile is writeClusterProfile (profiles_test.go) taking the
// identity the scope is then asked about.
func writeScopeProfile(t *testing.T, dir, profile string, identity clusterIdentity) {
	t.Helper()
	writeClusterProfile(t, dir, profile, identity.Project, identity.Cluster, identity.Location)
}

func testProfileScope(dir string, now *time.Time) *profileScope {
	return &profileScope{dir: dir, now: func() time.Time { return *now }}
}

func TestProfileScopeAnswersFromTheProfilesDirectory(t *testing.T) {
	dir := t.TempDir()
	prodA := clusterIdentity{Project: "p1", Location: "us-central1", Cluster: "prod-a"}
	prodB := clusterIdentity{Project: "p1", Location: "us-central1", Cluster: "prod-b"}
	writeScopeProfile(t, dir, "prod-a", prodA)
	now := time.Now()
	s := testProfileScope(dir, &now)

	if profiled, known := s.Profiled(prodA); !profiled || !known {
		t.Errorf("Profiled(prod-a) = (%v, %v), want (true, true): a profile names it", profiled, known)
	}
	if profiled, known := s.Profiled(prodB); profiled || !known {
		t.Errorf("Profiled(prod-b) = (%v, %v), want (false, true): the directory was read and no readable profile names it", profiled, known)
	}
}

// A profile written after startup counts as soon as the scope is stale: the
// reconcile onboards clusters while the detector runs, and a record from one
// must not be held because discovery ran before the profile existed.
func TestProfileScopeRereadsTheDirectoryAfterTheInterval(t *testing.T) {
	dir := t.TempDir()
	prodB := clusterIdentity{Project: "p1", Location: "us-central1", Cluster: "prod-b"}
	now := time.Now()
	s := testProfileScope(dir, &now)

	if profiled, known := s.Profiled(prodB); profiled || !known {
		t.Fatalf("Profiled(prod-b) = (%v, %v) on an empty directory, want (false, true): a readable empty scope is known, and is the fresh-install state that holds every record", profiled, known)
	}
	writeScopeProfile(t, dir, "prod-b", prodB)
	if profiled, _ := s.Profiled(prodB); profiled {
		t.Errorf("Profiled(prod-b) = true inside the rescan interval, want the cached answer (the interval is the whole cost bound)")
	}
	now = now.Add(profileScopeRescanInterval)
	if profiled, known := s.Profiled(prodB); !profiled || !known {
		t.Errorf("Profiled(prod-b) = (%v, %v) after the interval, want (true, true): the directory was re-read", profiled, known)
	}
}

// An unreadable directory is an unknown scope, not an empty one, and the
// answer says so rather than reporting every cluster as outside it.
func TestProfileScopeReportsAnUnreadableDirectoryAsUnknown(t *testing.T) {
	logs := captureLog(t)
	base := t.TempDir()
	dir := filepath.Join(base, "absent")
	now := time.Now()
	s := testProfileScope(dir, &now)
	prodA := clusterIdentity{Project: "p1", Location: "us-central1", Cluster: "prod-a"}
	if profiled, known := s.Profiled(prodA); profiled || known {
		t.Errorf("Profiled = (%v, %v) on a missing directory, want (false, false)", profiled, known)
	}
	now = now.Add(profileScopeRescanInterval)
	s.Profiled(prodA)
	const line = "cannot read " + "%s" + " to tell which clusters are inside the install's scope"
	want := strings.Replace(line, "%s", dir, 1)
	if got := strings.Count(logs.String(), want); got != 1 {
		t.Errorf("logged the unreadable directory %d time(s) over two reads, want once per streak:\n%s", got, logs.String())
	}

	// It appears, reads clean, then is gone again: a new streak, logged again.
	writeScopeProfile(t, dir, "prod-a", prodA)
	now = now.Add(profileScopeRescanInterval)
	if profiled, known := s.Profiled(prodA); !profiled || !known {
		t.Errorf("Profiled = (%v, %v) once the directory exists, want (true, true)", profiled, known)
	}
	if err := os.RemoveAll(dir); err != nil {
		t.Fatal(err)
	}
	now = now.Add(profileScopeRescanInterval)
	if profiled, known := s.Profiled(prodA); profiled || known {
		t.Errorf("Profiled = (%v, %v) after the directory vanished, want (false, false): scope unknown again", profiled, known)
	}
	if got := strings.Count(logs.String(), want); got != 2 {
		t.Errorf("logged the unreadable directory %d time(s) across two streaks, want 2:\n%s", got, logs.String())
	}
}

// The startup line says which mode the hold is in before the first record,
// for each of the three states a deployed detector can start in.
func TestScopeStartupLine(t *testing.T) {
	captureLog(t)
	empty := t.TempDir()
	named := t.TempDir()
	writeScopeProfile(t, named, "cluster-p1-prod-a-us-central1", clusterIdentity{Project: "p1", Location: "us-central1", Cluster: "prod-a"})
	broken := t.TempDir()
	if err := os.MkdirAll(filepath.Join(broken, "cluster-p1-prod-b-us-central1"), 0o700); err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		name  string
		scope scopeIndex
		want  []string
	}{
		{"no --profiles-dir", newProfileScope(""), []string{"install scope unknown", "no --profiles-dir", "nothing is held"}},
		{"unreadable directory", newProfileScope(filepath.Join(empty, "absent")), []string{"install scope unknown", "cannot be read", "nothing is held"}},
		{"no cluster profile yet", newProfileScope(empty), []string{"install scope read from " + empty, "no Cluster Agent profile yet", "is held out of the inject"}},
		{"profiles present, none readable", newProfileScope(broken), []string{"install scope read from " + broken, "1 cluster profile(s) found and none readable", "is held out of the inject"}},
		{"profiles present", newProfileScope(named), []string{"install scope read from " + named, "1 cluster profile(s)", "is held out of the inject"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got := scopeStartupLine(tc.scope)
			for _, want := range tc.want {
				if !strings.Contains(got, want) {
					t.Errorf("scopeStartupLine() = %q, want it to contain %q", got, want)
				}
			}
		})
	}
}

// A cluster profile that does not parse and has never named a cluster leaves
// its cluster held as outside the scope -- and the scope says so once per
// streak, by profile name, in the log, because the hold line alone would send
// the operator to write a profile that exists.
func TestProfileScopeReportsAProfileItCouldNotRead(t *testing.T) {
	logs := captureLog(t)
	dir := t.TempDir()
	broken := clusterIdentity{Project: "p1", Location: "us-central1", Cluster: "cluster-p1-prod-c-us-central1"}
	if err := os.MkdirAll(filepath.Join(dir, "cluster-p1-prod-c-us-central1"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "cluster-p1-prod-c-us-central1", "config.yaml"), []byte("model: ["), 0o600); err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	s := testProfileScope(dir, &now)

	if profiled, known := s.Profiled(broken); profiled || !known {
		t.Errorf("Profiled(prod-c) = (%v, %v) for an unparsable profile, want (false, true): it names nothing, and the directory was read", profiled, known)
	}
	now = now.Add(profileScopeRescanInterval)
	s.Profiled(broken) // a second read in the same streak
	const heldLine = "profile cluster-p1-prod-c-us-central1 names no readable cluster for the install's scope"
	if got := strings.Count(logs.String(), heldLine); got != 1 {
		t.Errorf("logged %q %d time(s) over two broken reads, want once per streak:\n%s", heldLine, got, logs.String())
	}
	if strings.Contains(logs.String(), "keeping") {
		t.Errorf("log says an identity is kept for a profile that never named one:\n%s", logs.String())
	}

	writeScopeProfile(t, dir, "cluster-p1-prod-c-us-central1", broken)
	now = now.Add(profileScopeRescanInterval)
	if profiled, _ := s.Profiled(broken); !profiled {
		t.Error("Profiled(prod-c) = false after the profile was rewritten, want true")
	}
	if err := os.WriteFile(filepath.Join(dir, "cluster-p1-prod-c-us-central1", "config.yaml"), []byte("model: ["), 0o600); err != nil {
		t.Fatal(err)
	}
	now = now.Add(profileScopeRescanInterval)
	s.Profiled(broken)
	// A new streak, and a different one: the profile has named a cluster since.
	if got := strings.Count(logs.String(), "profile cluster-p1-prod-c-us-central1 names no cluster on this read"); got != 1 {
		t.Errorf("want the profile logged again, as kept, when it breaks after a clean read; log:\n%s", logs.String())
	}
}

// A cluster profile whose config names nothing on this read -- the scaffold
// stamps the identity last, in place, so a read can land before it -- keeps the
// identity it last carried while its directory exists, so a re-scaffold of an
// onboarded cluster does not hold that cluster for an interval. The keep is
// keyed on the directory and not on the profile's name or on ReadIdentities
// reporting the drop: a profile without the cluster- prefix whose whole block
// is gone is the drop the read cannot report, and it is kept the same way. A
// cluster profile that has never named a cluster is reported, not passed over
// as a non-cluster profile, so the hold has a line pointing at the file.
func TestProfileScopeKeepsAProfilesLastIdentityWhileItsDirectoryExists(t *testing.T) {
	logs := captureLog(t)
	dir := t.TempDir()
	prodD := clusterIdentity{Project: "p1", Location: "us-central1", Cluster: "prod-d"}
	prodF := clusterIdentity{Project: "p1", Location: "us-central1", Cluster: "prod-f"}
	writeScopeProfile(t, dir, "cluster-p1-prod-d-us-central1", prodD)
	writeScopeProfile(t, dir, "prod-f", prodF)
	now := time.Now()
	s := testProfileScope(dir, &now)
	for _, id := range []clusterIdentity{prodD, prodF} {
		if profiled, _ := s.Profiled(id); !profiled {
			t.Fatalf("Profiled(%s) = false with its profile written", id.Cluster)
		}
	}

	// Mid-rewrite: the template config, no identity block -- under both names.
	for _, profile := range []string{"cluster-p1-prod-d-us-central1", "prod-f"} {
		if err := os.WriteFile(filepath.Join(dir, profile, "config.yaml"), []byte("model:\n  provider: custom\n"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	now = now.Add(profileScopeRescanInterval)
	for _, id := range []clusterIdentity{prodD, prodF} {
		if profiled, known := s.Profiled(id); !profiled || !known {
			t.Errorf("Profiled(%s) = (%v, %v) while its config names nothing, want (true, true): the last identity is kept", id.Cluster, profiled, known)
		}
	}
	for _, want := range []string{
		"profile cluster-p1-prod-d-us-central1 names no cluster on this read (config.yaml carries no cluster_identity); keeping p1/us-central1/prod-d inside the install's scope",
		"profile prod-f names no cluster on this read (config.yaml carries no cluster_identity); keeping p1/us-central1/prod-f inside the install's scope",
	} {
		if !strings.Contains(logs.String(), want) {
			t.Errorf("log lacks %q:\n%s", want, logs.String())
		}
	}
	if strings.Contains(logs.String(), "held out of the inject") {
		t.Errorf("log says a kept cluster is held:\n%s", logs.String())
	}

	// The config removed but the directory left: kept, with that as the reason.
	if err := os.Remove(filepath.Join(dir, "prod-f", "config.yaml")); err != nil {
		t.Fatal(err)
	}
	// A new streak needs a clean read in between, so rewrite then remove.
	writeScopeProfile(t, dir, "prod-f", prodF)
	now = now.Add(profileScopeRescanInterval)
	s.Profiled(prodF)
	if err := os.Remove(filepath.Join(dir, "prod-f", "config.yaml")); err != nil {
		t.Fatal(err)
	}
	now = now.Add(profileScopeRescanInterval)
	if profiled, _ := s.Profiled(prodF); !profiled {
		t.Error("Profiled(prod-f) = false with its config removed and its directory present, want true")
	}
	if want := "profile prod-f names no cluster on this read (config.yaml is absent); keeping p1/us-central1/prod-f"; !strings.Contains(logs.String(), want) {
		t.Errorf("log lacks %q:\n%s", want, logs.String())
	}

	// Replaced by a file: no directory, so nothing to keep.
	if err := os.RemoveAll(filepath.Join(dir, "prod-f")); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "prod-f"), []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	now = now.Add(profileScopeRescanInterval)
	if profiled, known := s.Profiled(prodF); profiled || !known {
		t.Errorf("Profiled(prod-f) = (%v, %v) with a file at the profile's path, want (false, true): the keep is for a directory", profiled, known)
	}

	// Gone: the reconcile offboarded it, and the identity goes with the directory.
	if err := os.RemoveAll(filepath.Join(dir, "cluster-p1-prod-d-us-central1")); err != nil {
		t.Fatal(err)
	}
	now = now.Add(profileScopeRescanInterval)
	if profiled, known := s.Profiled(prodD); profiled || !known {
		t.Errorf("Profiled(prod-d) = (%v, %v) after the directory was removed, want (false, true)", profiled, known)
	}

	// Never named a cluster: a cluster- directory the scope has not seen complete.
	if err := os.MkdirAll(filepath.Join(dir, "cluster-p1-prod-e-us-central1"), 0o700); err != nil {
		t.Fatal(err)
	}
	now = now.Add(profileScopeRescanInterval)
	prodE := clusterIdentity{Project: "p1", Location: "us-central1", Cluster: "prod-e"}
	if profiled, known := s.Profiled(prodE); profiled || !known {
		t.Errorf("Profiled(prod-e) = (%v, %v) for a cluster profile with no config yet, want (false, true)", profiled, known)
	}
	const heldLine = "profile cluster-p1-prod-e-us-central1 names no readable cluster for the install's scope (config.yaml is absent); an unreachable record from its cluster is held"
	if !strings.Contains(logs.String(), heldLine) {
		t.Errorf("log lacks %q:\n%s", heldLine, logs.String())
	}
}

// The platform profile lives under the same directory and names no cluster, so
// a bad edit to it is nobody's held cluster and gets no line; the same break in
// a profile this scope saw name a cluster is kept, with the parse error as the
// reason.
func TestProfileScopeIsSilentAboutABrokenNonClusterProfile(t *testing.T) {
	logs := captureLog(t)
	dir := t.TempDir()
	prodG := clusterIdentity{Project: "p1", Location: "us-central1", Cluster: "prod-g"}
	writeScopeProfile(t, dir, "prod-g", prodG)
	if err := os.MkdirAll(filepath.Join(dir, "platform"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "platform", "config.yaml"), []byte("model: ["), 0o600); err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	s := testProfileScope(dir, &now)
	s.Profiled(prodG)
	if strings.Contains(logs.String(), "profile platform ") {
		t.Errorf("log mentions the platform profile, which names no cluster and holds nothing:\n%s", logs.String())
	}

	if err := os.WriteFile(filepath.Join(dir, "prod-g", "config.yaml"), []byte("model: ["), 0o600); err != nil {
		t.Fatal(err)
	}
	now = now.Add(profileScopeRescanInterval)
	if profiled, _ := s.Profiled(prodG); !profiled {
		t.Error("Profiled(prod-g) = false after its config broke, want true: the last identity is kept")
	}
	if !strings.Contains(logs.String(), "profile prod-g names no cluster on this read (parse ") {
		t.Errorf("log lacks the kept line with the parse error as its reason:\n%s", logs.String())
	}
	if strings.Contains(logs.String(), "profile platform ") {
		t.Errorf("log mentions the platform profile:\n%s", logs.String())
	}
}

func TestNewProfileScopeIsNilWithoutADirectory(t *testing.T) {
	if s := newProfileScope(""); s != nil {
		t.Errorf("newProfileScope(\"\") = %#v, want a nil scopeIndex: no --profiles-dir means the scope was never declared", s)
	}
	if s := newProfileScope(t.TempDir()); s == nil {
		t.Error("newProfileScope(dir) = nil, want a scope")
	}
}
