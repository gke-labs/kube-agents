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

//go:build !race

package fakeapiserver

// The fleet-scale cluster: the thousand-node shape whose startup burst the
// event watcher is sized for, served per fake cluster when the race detector
// is off, which is how CI runs the operator's tests. sizes_race.go holds the
// smaller dataset the same tests use under a hand-run `go test -race`, where
// the instrumented decode of this many objects would take minutes.
const (
	FleetNodes  = 1_500
	FleetPods   = 20_000
	FleetEvents = 50_000
	// UnderRace says which of the two datasets this build carries.
	UnderRace = false
)

// Fleet is the fleet-scale cluster's sizes for this build.
var Fleet = Sizes{Nodes: FleetNodes, Pods: FleetPods, Events: FleetEvents}
