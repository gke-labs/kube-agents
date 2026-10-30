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
	"context"
	"log"
)

// Scan is what one DiscoverAndBuild pass came back with.
type Scan[T any] struct {
	// Built is one value per discovered cluster whose build succeeded.
	Built []T

	// Skipped counts the profiles naming a cluster this run will not reach:
	// one Discover skipped, or one whose build failed. One per straggler, so
	// it can be compared against the size of the fleet.
	Skipped int

	// DirUnreadable says the profiles directory itself could not be read
	// (EACCES from a bad mount, ENOTDIR when the path names a file). Not
	// folded into Skipped, because the number of clusters behind an
	// unreadable directory is unknown and counting it as one would understate
	// it to whoever alerts on that number. Not an error either: Discover
	// degrades here rather than returning one, a restart being no fix for
	// permissions. Built is empty and Skipped is 0 for an empty directory
	// too, so without this a caller cannot tell a broken mount from profiles
	// that have not been written yet.
	DirUnreadable bool
}

// ScanOptions is what a caller of DiscoverAndBuild says about itself.
type ScanOptions struct {
	// Component prefixes every log line, as the binary's name.
	Component string

	// Consequence finishes "its cluster will NOT be ..." in a skip's log line:
	// what this caller does with a cluster that a skip loses ("watched",
	// "joined").
	Consequence string

	// OnSkip, when set, is called after the skip is logged and counted, for
	// every skip including NoProfile. For a caller with somewhere else to
	// report it, such as a metric labelled by profile.
	OnSkip func(profile string, err error)
}

// DiscoverAndBuild runs d.Discover over dir and builds one T per discovered
// cluster, with the failure policy every consumer of this package shares:
//
//   - a missing directory, or a credential proxy that cannot be reached or
//     answers with an error about itself, is the error returned, because
//     discovery runs once and a restart fixes it;
//   - an unreadable directory degrades, and is reported as Scan.DirUnreadable
//     rather than as a count;
//   - a single bad profile is skipped, so one unparseable config.yaml cannot
//     cost the whole fleet -- and a cluster whose build fails is skipped too,
//     for the same reason.
//
// Every skip is logged and counted. The count matters because a fan-in that
// silently reached six of seven clusters looks exactly like a fleet with six
// clusters in it.
//
// d is the caller's own copy, so a package-level test seam is not written to.
// Its OnSkip is replaced (report through opts.OnSkip instead); its Want and
// Describe are kept; a nil Broker is filled from BrokerFromEnv, and a Broker
// already set is a test's and is kept.
//
// build is what differs between consumers -- an informer-backed client in the
// event watcher, a dynamic client in the drift detector -- which is why
// Discover returns configs rather than clients. Its error is the skip's
// reason, logged as given.
func DiscoverAndBuild[T any](ctx context.Context, d Discoverer, dir string, opts ScanOptions, build func(Cluster) (T, error)) (Scan[T], error) {
	scan := Scan[T]{}
	skip := func(profile string, err error) {
		if profile == NoProfile {
			// The directory itself, not a profile in it. The error already
			// says which directory and what it means.
			scan.DirUnreadable = true
			log.Printf("%s: %v", opts.Component, err)
		} else {
			scan.Skipped++
			log.Printf("%s: skipping profile %s, its cluster will NOT be %s: %v", opts.Component, profile, opts.Consequence, err)
		}
		if opts.OnSkip != nil {
			opts.OnSkip(profile, err)
		}
	}

	d.OnSkip = skip
	if d.Broker == nil {
		broker, err := BrokerFromEnv()
		if err != nil {
			return scan, err
		}
		d.Broker = broker
	}
	discovered, err := d.Discover(ctx, dir)
	if err != nil {
		return scan, err
	}

	scan.Built = make([]T, 0, len(discovered))
	for _, c := range discovered {
		built, err := build(c)
		if err != nil {
			skip(c.Profile, err)
			continue
		}
		scan.Built = append(scan.Built, built)
	}
	return scan, nil
}
