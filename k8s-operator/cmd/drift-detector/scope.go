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
	"errors"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"sync"
	"time"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/clusterprofiles"
)

const (
	// profileScopeRescanInterval bounds how stale the scope may be: an
	// unreachable record arriving this long after the last read of the
	// profiles directory reads it again before being judged. One directory
	// listing and one small file per profile, so a minute is cheap at any
	// fleet size the join itself can carry -- and it is the interval that
	// makes the hold safe on a long-lived pod, since cluster-agent-reconcile
	// writes profiles while the detector runs and discovery (the
	// reachability pass) runs once at startup.
	profileScopeRescanInterval = time.Minute
)

// scopeIndex answers whether the install's scope names a cluster, which is a
// different question from whether the join can read it. Every cluster the
// Platform Agent's reconcile onboards gets a Cluster Agent profile, so "a
// readable profile names it" is the detector's view of the scope; a cluster
// the join could not reach despite a profile (a 403 at discovery, a profile
// written after startup) is inside it, and a cluster in the project with no
// profile at all -- excluded, or never onboarded -- is outside.
type scopeIndex interface {
	// Profiled reports whether a readable Cluster Agent profile names
	// identity, by its exact triple. known is false when there is no answer:
	// the profiles directory could not be read, so the scope is unknown
	// rather than empty, and the caller holds nothing.
	Profiled(identity clusterIdentity) (profiled, known bool)
}

// profileScope is the scopeIndex over a profiles directory. It re-reads the
// directory lazily, when asked and at most once per profileScopeRescanInterval,
// and never addresses a cluster: clusterprofiles.ReadIdentities reads config
// files only.
//
// A profile that named a cluster on an earlier read and does not on this one
// -- its config removed, unreadable, unparsable, truncated, or stripped of its
// identity block, reported by ReadIdentities or not -- keeps that identity for
// as long as a directory is at its path, and the keep is logged by name once
// per streak. That is what closes the scaffold window: cluster_agent_profile.py
// copies the template config in and stamps the identity after it, in place (the
// one later rewrite is atomic and keeps the block), so a read landing between
// the two sees a cluster profile that names nothing; a first scaffold
// was unprofiled an instant earlier and loses nothing, but a re-scaffold of an
// existing profile would otherwise hold an in-scope cluster for up to an
// interval. The identity is dropped only with the directory, which is how the
// reconcile offboards a cluster. A profile the read drops that has never named
// a cluster here is logged by name once per streak too, as held, so the hold
// line is not the only statement in the log; the reserved platform and default
// profiles are the read's to keep quiet about, whatever state they are in.
//
// Safe for concurrent use. The joiner that asks it is single-threaded today,
// but the lock costs nothing and the rescan writes state, so the type does not
// inherit a constraint the joiner documents for a different reason.
type profileScope struct {
	dir string
	now func() time.Time

	mu        sync.Mutex
	scannedAt time.Time
	// byProfile is each cluster profile's identity by directory name, the
	// last one read for it; profiled is the same set keyed for the answer.
	byProfile map[string]clusterIdentity
	profiled  map[clusterIdentity]struct{}
	readable  bool
	// unreadableLogged keeps a directory that stays unreadable from logging
	// once per record; it resets on the next successful read so a directory
	// that breaks again is reported again.
	unreadableLogged bool
	// skipped is the profiles the last read did not read complete, by
	// directory name -- kept or held -- so each is logged once per streak
	// rather than once per rescan: the hold line alone would send the
	// operator to write a profile that exists.
	skipped map[string]struct{}
}

// newProfileScope returns the scope over dir, or nil when dir is empty: a
// detector started without --profiles-dir was never told the install's scope,
// and a nil scopeIndex is how the joiner learns that.
func newProfileScope(dir string) scopeIndex {
	if dir == "" {
		return nil
	}
	return &profileScope{dir: dir, now: time.Now}
}

// startupLine says at startup which mode the hold is in, for the reason the
// join and inject lines exist: the difference is invisible in every later line
// until a record is held. It reads the directory once, so a fresh install --
// a readable directory with no cluster profile yet, which holds every record
// off the joined clusters until the first reconcile tick -- says so before
// the first record rather than only in the hold lines that follow.
func (s *profileScope) startupLine() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.rescan()
	if !s.readable {
		return fmt.Sprintf("install scope unknown: %s cannot be read, so every unreachable record is forwarded and nothing is held until it can be", s.dir)
	}
	if len(s.profiled) == 0 && len(s.skipped) > 0 {
		return fmt.Sprintf("install scope read from %s: %d cluster profile(s) found and none readable (the lines above say why), so until one reads, every unreachable record is held out of the inject -- every record naming a live object, if the join reads no cluster", s.dir, len(s.skipped))
	}
	if len(s.profiled) == 0 {
		return fmt.Sprintf("install scope read from %s: no Cluster Agent profile yet, so until the Cluster Agent reconcile writes one, every unreachable record is held out of the inject -- every record naming a live object, if the join reads no cluster", s.dir)
	}
	return fmt.Sprintf("install scope read from %s: %d cluster profile(s); an unreachable record from a cluster none of them names is held out of the inject (re-read at most once a minute)", s.dir, len(s.profiled))
}

// scopeStartupLine is startupLine for whatever newProfileScope returned,
// including the nil of no --profiles-dir.
func scopeStartupLine(scope scopeIndex) string {
	if s, ok := scope.(*profileScope); ok {
		return s.startupLine()
	}
	return "install scope unknown: no --profiles-dir, so every unreachable record is forwarded and nothing is held"
}

// Profiled implements scopeIndex.
func (s *profileScope) Profiled(identity clusterIdentity) (bool, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.scannedAt.IsZero() || s.now().Sub(s.scannedAt) >= profileScopeRescanInterval {
		s.rescan()
	}
	if !s.readable {
		return false, false
	}
	// The exact triple, and nothing looser: a fleet legitimately runs "prod"
	// in two locations, and one of them may be the excluded cluster this hold
	// exists for, so a profile naming the same project and cluster under
	// another location names nothing a record carries. Such a profile -- a
	// zone written for a regional cluster -- is one discovery reports as
	// skipped with the GKE API's answer, and the startup skip line says what
	// becomes of its cluster's records.
	_, ok := s.profiled[identity]
	return ok, true
}

// rescan replaces the profiled set from the directory. Caller holds mu.
func (s *profileScope) rescan() {
	s.scannedAt = s.now()
	dropped := map[string]error{}
	ids, err := clusterprofiles.ReadIdentities(s.dir, func(profile string, err error) {
		dropped[profile] = err
	})
	if err != nil {
		s.readable = false
		if !s.unreadableLogged {
			log.Printf("%s: cannot read %s to tell which clusters are inside the install's scope, so no record is held out of the inject until it can be read: %v", commandName, s.dir, err)
			s.unreadableLogged = true
		}
		return
	}

	byProfile := make(map[string]clusterIdentity, len(ids))
	for _, p := range ids {
		byProfile[p.Profile] = identityFromProfile(p.Identity)
	}
	skipped := map[string]struct{}{}
	// The keep is keyed on the directory, not on the read having reported the
	// drop: a profile this scope saw complete is a cluster profile whatever
	// its name, and a config removed or stripped of its whole block under a
	// name without the cluster- prefix are the drops ReadIdentities does not
	// report.
	for profile, kept := range s.byProfile {
		if _, complete := byProfile[profile]; complete {
			continue
		}
		// A directory, through a link or not; a path that is now a file is a
		// profile that is gone, whatever sits at its name.
		if info, statErr := os.Stat(filepath.Join(s.dir, profile)); statErr != nil || !info.IsDir() {
			continue
		}
		byProfile[profile] = kept
		skipped[profile] = struct{}{}
		if _, logged := s.skipped[profile]; !logged {
			log.Printf("%s: profile %s names no cluster on this read (%v); keeping %s inside the install's scope while the profile directory exists", commandName, profile, s.dropReason(profile, dropped[profile]), kept)
		}
	}
	for profile, reason := range dropped {
		if _, kept := skipped[profile]; kept {
			continue
		}
		skipped[profile] = struct{}{}
		if _, logged := s.skipped[profile]; !logged {
			log.Printf("%s: profile %s names no readable cluster for the install's scope (%v); an unreachable record from its cluster is held out of the inject as outside the scope until it does", commandName, profile, reason)
		}
	}

	set := make(map[clusterIdentity]struct{}, len(byProfile))
	for _, id := range byProfile {
		set[id] = struct{}{}
	}
	s.readable = true
	s.unreadableLogged = false
	s.skipped = skipped
	s.byProfile = byProfile
	s.profiled = set
}

// dropReason is the reason ReadIdentities gave for a profile it dropped, or
// for the drops it does not report under a name without the cluster- prefix
// -- a config that is absent, or reads cleanly with no cluster_identity block
// -- which of the two, told apart by a stat.
func (s *profileScope) dropReason(profile string, reported error) error {
	if reported != nil {
		return reported
	}
	if _, err := os.Stat(filepath.Join(s.dir, profile, clusterprofiles.ProfileConfigFile)); errors.Is(err, os.ErrNotExist) {
		return clusterprofiles.ErrNoProfileConfig
	}
	return clusterprofiles.ErrNoClusterIdentity
}
