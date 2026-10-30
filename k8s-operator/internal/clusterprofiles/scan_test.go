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

package clusterprofiles

import (
	"bytes"
	"context"
	"errors"
	"log"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
)

// captureLog sends the standard logger to a buffer for the test's duration.
func captureLog(t *testing.T) *bytes.Buffer {
	t.Helper()
	var buf bytes.Buffer
	prevOut, prevFlags := log.Writer(), log.Flags()
	log.SetOutput(&buf)
	log.SetFlags(0)
	t.Cleanup(func() {
		log.SetOutput(prevOut)
		log.SetFlags(prevFlags)
	})
	return &buf
}

var testScanOptions = ScanOptions{Component: "test-component", Consequence: "tested"}

func TestDiscoverAndBuildBuildsOnePerClusterAndCountsSkips(t *testing.T) {
	dir := t.TempDir()
	writeClusterProfile(t, dir, "cluster-good", "p", "good", "us-central1")
	writeClusterProfile(t, dir, "cluster-ghost", "p", "ghost", "us-central1")
	writeClusterProfile(t, dir, "cluster-nobuild", "p", "nobuild", "us-central1")
	writeNonClusterProfile(t, dir, "default")
	s := newStub()
	s.failures["p/us-central1/ghost"] = errors.New("not found")
	logs := captureLog(t)

	var reported []string
	opts := testScanOptions
	opts.OnSkip = func(profile string, _ error) { reported = append(reported, profile) }
	scan, err := DiscoverAndBuild(context.Background(), s.discoverer(), dir, opts, func(c Cluster) (string, error) {
		if c.Identity.Cluster == "nobuild" {
			return "", errors.New("client would not build")
		}
		return c.Profile, nil
	})
	if err != nil {
		t.Fatalf("DiscoverAndBuild: %v", err)
	}
	if len(scan.Built) != 1 || scan.Built[0] != "cluster-good" {
		t.Errorf("Built = %v, want [cluster-good]", scan.Built)
	}
	// One Discover skip and one build failure; the non-cluster profile is
	// not a cluster lost, so it is neither.
	if scan.Skipped != 2 {
		t.Errorf("Skipped = %d, want 2", scan.Skipped)
	}
	if scan.DirUnreadable {
		t.Error("DirUnreadable = true after a directory that read fine")
	}
	sort.Strings(reported)
	if strings.Join(reported, ",") != "cluster-ghost,cluster-nobuild" {
		t.Errorf("OnSkip saw %v, want cluster-ghost and cluster-nobuild", reported)
	}
	for _, want := range []string{
		"test-component: skipping profile cluster-ghost, its cluster will NOT be tested:",
		"test-component: skipping profile cluster-nobuild, its cluster will NOT be tested: client would not build",
	} {
		if !strings.Contains(logs.String(), want) {
			t.Errorf("log lacks %q:\n%s", want, logs.String())
		}
	}
}

func TestDiscoverAndBuildReportsAnUnreadableDirAsAFlagNotACount(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "profiles")
	if err := os.MkdirAll(dir, 0o000); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	t.Cleanup(func() { _ = os.Chmod(dir, 0o700) })
	if os.Geteuid() == 0 {
		t.Skip("running as root, an unreadable directory is still readable")
	}
	logs := captureLog(t)

	var reported []string
	opts := testScanOptions
	opts.OnSkip = func(profile string, _ error) { reported = append(reported, profile) }
	scan, err := DiscoverAndBuild(context.Background(), newStub().discoverer(), dir, opts, func(c Cluster) (string, error) {
		return c.Profile, nil
	})
	if err != nil {
		t.Fatalf("an unreadable dir must degrade, not fail: %v", err)
	}
	if !scan.DirUnreadable {
		t.Error("DirUnreadable = false; an unreadable directory is indistinguishable from an empty one without it")
	}
	// An unknown number of clusters is behind it, so it is not one skip.
	if scan.Skipped != 0 {
		t.Errorf("Skipped = %d, want 0", scan.Skipped)
	}
	// Still passed on, for a caller that labels a metric by profile.
	if len(reported) != 1 || reported[0] != NoProfile {
		t.Errorf("OnSkip saw %v, want [%q]", reported, NoProfile)
	}
	if strings.Contains(logs.String(), "skipping profile") {
		t.Errorf("an unreadable directory was logged as a skipped profile:\n%s", logs.String())
	}
}

func TestDiscoverAndBuildReturnsAMissingDirAsTheError(t *testing.T) {
	_, err := DiscoverAndBuild(context.Background(), newStub().discoverer(), filepath.Join(t.TempDir(), "absent"), testScanOptions, func(c Cluster) (string, error) {
		return c.Profile, nil
	})
	if err == nil {
		t.Fatal("a missing profiles dir must be fatal: discovery runs once and a restart fixes it")
	}
}

func TestDiscoverAndBuildKeepsTheCallersWantAndReplacesItsOnSkip(t *testing.T) {
	dir := t.TempDir()
	writeClusterProfile(t, dir, "cluster-kept", "p", "kept", "us-central1")
	writeClusterProfile(t, dir, "cluster-declined", "p", "declined", "us-central1")
	captureLog(t)

	d := newStub().discoverer()
	callerOnSkip := false
	d.OnSkip = func(string, error) { callerOnSkip = true }
	d.Want = func(id Identity) bool { return id.Cluster != "declined" }
	scan, err := DiscoverAndBuild(context.Background(), d, dir, testScanOptions, func(c Cluster) (string, error) {
		if c.Identity.Cluster == "kept" {
			return "", errors.New("skip this one")
		}
		return c.Profile, nil
	})
	if err != nil {
		t.Fatalf("DiscoverAndBuild: %v", err)
	}
	// Declined by Want: neither built nor counted as lost.
	if len(scan.Built) != 0 || scan.Skipped != 1 {
		t.Errorf("Built = %v, Skipped = %d; want none built and only the build failure skipped", scan.Built, scan.Skipped)
	}
	if callerOnSkip {
		t.Error("the Discoverer's own OnSkip was called; DiscoverAndBuild replaces it, and opts.OnSkip is the channel")
	}
}
