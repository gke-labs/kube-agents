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

//go:build race

package fakeapiserver

// The fleet-scale cluster under the race detector, which a hand-run
// `go test -race` builds: a tenth of the Pods and Events and a third of the
// Nodes, so the soak's instrumented decode of ten clusters takes seconds.
// The same tests run against both datasets; sizes.go holds the full one CI
// runs.
const (
	FleetNodes  = 500
	FleetPods   = 2_000
	FleetEvents = 5_000
	// UnderRace says which of the two datasets this build carries.
	UnderRace = true
)

// Fleet is the fleet-scale cluster's sizes for this build.
var Fleet = Sizes{Nodes: FleetNodes, Pods: FleetPods, Events: FleetEvents}
