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
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"os"
)

const (
	// The count flags, which are also the keys of a profile file, so one
	// name serves the file, the command line and the summary.
	flagNodes      = "nodes"
	flagPods       = "pods"
	flagNamespaces = "namespaces"
	flagServices   = "services"
	flagEvents     = "events"
)

// countField is one of the profile's counts: its name, which serves as the
// flag, the profile key and the summary label, and where it lives in a
// profile.
type countField struct {
	name string
	// field returns the count's address in p, so the one table serves both
	// reading a profile and writing one.
	field func(p *profile) *int
}

// countFields lists the counts in the order the summary reports them. It is
// the only enumeration of them: parseFlags, withOverrides, counts and
// validate all iterate it, so none can drift from the others.
var countFields = []countField{
	{flagNodes, func(p *profile) *int { return &p.Nodes }},
	{flagPods, func(p *profile) *int { return &p.Pods }},
	{flagNamespaces, func(p *profile) *int { return &p.Namespaces }},
	{flagServices, func(p *profile) *int { return &p.Services }},
	{flagEvents, func(p *profile) *int { return &p.Events }},
}

// profile is the shape of the cluster a run seeds: the object counts the
// generator and the phases take. tests/scale/kwok/profiles/*.json carry one
// each; the count flags override single fields.
type profile struct {
	Nodes      int `json:"nodes"`
	Pods       int `json:"pods"`
	Namespaces int `json:"namespaces"`
	Services   int `json:"services"`
	Events     int `json:"events"`
}

// loadProfile reads a profile file. An unknown key is an error rather than
// a silent zero, so a misspelt count fails with its name.
func loadProfile(path string) (profile, error) {
	raw, err := os.ReadFile(path) // #nosec G304 -- the profile the command line names; reading it is the point
	if err != nil {
		return profile{}, fmt.Errorf("profile: %w", err)
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	var p profile
	if err := dec.Decode(&p); err != nil {
		return profile{}, fmt.Errorf("profile %s: %w", path, err)
	}
	return p, nil
}

// withOverrides returns the profile with the counts in set, keyed by flag
// name, in place of the file's.
func (p profile) withOverrides(set map[string]int) profile {
	for _, c := range countFields {
		if value, ok := set[c.name]; ok {
			*c.field(&p) = value
		}
	}
	return p
}

// counts pairs each count with its flag name.
func (p profile) counts() map[string]int {
	counts := make(map[string]int, len(countFields))
	for _, c := range countFields {
		counts[c.name] = *c.field(&p)
	}
	return counts
}

// validate rejects a count of zero or less, and more nodes than pods: pods
// land on node i mod nodes, so a node past the pod count would stay empty.
func (p profile) validate() error {
	counts := p.counts()
	for _, c := range countFields {
		if counts[c.name] <= 0 {
			return fmt.Errorf("%s must be greater than zero, got %d", c.name, counts[c.name])
		}
	}
	if p.Pods < p.Nodes {
		return fmt.Errorf("pods (%d) must be at least nodes (%d) so every node carries a pod", p.Pods, p.Nodes)
	}
	return nil
}

// resolveProfile is the profile a run uses: the file at path, if any, with
// the explicit count flags in set laid over it, validated. Without a file
// every count flag must be set.
func resolveProfile(path string, set map[string]int) (profile, error) {
	var p profile
	if path != "" {
		loaded, err := loadProfile(path)
		if err != nil {
			return profile{}, err
		}
		p = loaded
	} else if len(set) < len(countFields) {
		return profile{}, errors.New("either --profile or all of --nodes, --pods, --namespaces, --services and --events are required")
	}
	p = p.withOverrides(set)
	if err := p.validate(); err != nil {
		return profile{}, err
	}
	return p, nil
}
