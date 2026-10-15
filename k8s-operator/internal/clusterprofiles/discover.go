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
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"golang.org/x/oauth2"
	container "google.golang.org/api/container/v1"
	"k8s.io/client-go/rest"
)

const (
	// NoProfile is the profile name reported to OnSkip when the failure is
	// reading the profiles directory itself rather than one profile in it. A
	// caller labelling a metric by profile needs some value for it, and this is
	// the one to compare against rather than writing "-" at the call site.
	//
	// It is distinct from every real profile name because of where those names
	// come from, not because the filesystem forbids it: a directory may be
	// called "-", but the Platform Agent derives profile names from the
	// project/location/cluster triple, so it never generates one. A caller that
	// discriminates on this value is trusting that generator.
	NoProfile = "-"

	// hiddenPrefix marks a directory entry this scan ignores, so an editor's
	// or a tool's dot-directory beside the profiles is not read as one.
	hiddenPrefix = "."

	// clusterProfilePrefix is what the Platform Agent's profile_name puts in
	// front of every Cluster Agent profile directory, and nothing else carries
	// it (deploy/shared/sandbox_mirror.py discriminates on the same prefix).
	// ReadIdentities uses it for the two drops it cannot otherwise place: a
	// config that is absent, or reads cleanly and carries no cluster_identity
	// block, is a cluster profile mid-scaffold under the prefix and a
	// non-cluster profile or a stray directory without it.
	clusterProfilePrefix = "cluster-"
)

// reservedProfiles are the Hermes profiles that live under the same directory
// and are never a cluster's -- cluster_agent_profile.py's RESERVED_PROFILES --
// so ReadIdentities says nothing about them whatever state they are in.
var reservedProfiles = map[string]bool{"default": true, "platform": true}

// ErrNoClusterIdentity and ErrNoProfileConfig are the reasons ReadIdentities
// reports for a cluster- prefixed profile whose config reads cleanly and
// carries no cluster_identity block, or is absent -- and the reasons a caller
// supplies for the two drops it does not report: the same two shapes under a
// name without the prefix.
var (
	ErrNoClusterIdentity = errors.New(profileConfigFile + " carries no cluster_identity")
	ErrNoProfileConfig   = errors.New(profileConfigFile + " is absent")
)

// ProfileIdentity is one cluster profile's directory name with the identity
// its config carries, as ReadIdentities lists them.
type ProfileIdentity struct {
	Profile  string
	Identity Identity
}

// Cluster is one cluster discovered from a profile, addressable and
// authenticated. Config carries the control-plane address and this process's
// credential; building a client from it is the caller's job.
type Cluster struct {
	// Identity comes from the profile's cluster_identity block, which the
	// Platform Agent writes as machine-readable metadata. Deliberately not
	// derived from the profile directory name: that name is sanitized and
	// hash-truncated past 63 chars, so it is lossy.
	Identity Identity
	// Profile is the directory name. Unique by construction — the Python side
	// derives it from the whole triple — so a caller can use it as a
	// per-cluster filename, where the bare cluster name would collide.
	Profile string
	Config  *rest.Config
	// Autopilot is the describe call's Autopilot.Enabled, carried out of
	// Discover because the GKE API was already asked. It separates the two
	// clusters that look identical from inside — both report zero nodes and
	// leave GKE's own system pods Pending — but mean opposite things: on
	// Autopilot that is the product scaling an unused cluster to nothing, and
	// on Standard it is a fault. A caller with no describe behind it (the
	// direct --in-cluster cluster) never builds one of these and reads false,
	// which is the fail-open side of that question.
	Autopilot bool
}

// Discoverer scans a profiles directory. The zero value reaches the real GKE
// API with this process's own Google credentials and reports nothing about the
// profiles it skips; the fields below replace those pieces.
type Discoverer struct {
	// Describe reads one cluster's control-plane addressing. nil asks the GKE
	// API. A field rather than a package-level variable so a test can answer
	// it without a Google credential or a network call, and so two callers in
	// one process cannot stub it out from under each other.
	Describe func(ctx context.Context, id Identity) (*container.Cluster, error)

	// TokenSource mints the credential every discovered cluster is reached
	// with. nil uses this process's Application Default Credentials at
	// GKEAuthScope. A field for the same reason as Describe.
	TokenSource func(ctx context.Context) (oauth2.TokenSource, error)

	// Want decides whether a profile's cluster is one this caller wants at all,
	// from its identity alone and before anything is spent on reaching it. nil
	// wants every cluster.
	//
	// A caller can drop clusters from the slice Discover returns instead, and
	// gets a different outcome: by then the token has been minted and the GKE
	// API has been asked where each cluster is. That costs a describe call per
	// unwanted cluster, and — where the caller's credentials do not carry
	// container.clusters.get in the unwanted cluster's project — turns the drop
	// into a skip reported as a GKE permission failure, which sends an operator
	// to grant access to a cluster the caller was going to discard. Deciding
	// here rather than there is what makes an unwanted cluster free and silent.
	//
	// Not reported through OnSkip: a cluster the caller never wanted is not a
	// cluster it failed to reach, and counting the two together would inflate
	// the number an operator reads as "clusters I am losing coverage of".
	Want func(id Identity) bool

	// OnSkip is told about every cluster that will not be reached, with the
	// profile it came from — or NoProfile, when the profiles directory itself
	// could not be read. A skip is not an error return (see Discover), so this
	// is the only channel through which "this cluster is not being watched"
	// becomes visible. nil discards them.
	OnSkip func(profile string, err error)
}

func (d Discoverer) want(id Identity) bool {
	return d.Want == nil || d.Want(id)
}

func (d Discoverer) describe(ctx context.Context, id Identity) (*container.Cluster, error) {
	if d.Describe != nil {
		return d.Describe(ctx, id)
	}
	return describeClusterViaAPI(ctx, id)
}

func (d Discoverer) tokenSource(ctx context.Context) (oauth2.TokenSource, error) {
	if d.TokenSource != nil {
		return d.TokenSource(ctx)
	}
	return defaultTokenSource(ctx)
}

func (d Discoverer) skip(profile string, err error) {
	if d.OnSkip != nil {
		d.OnSkip(profile, err)
	}
}

// ReadIdentities lists the cluster identity of every Cluster Agent profile in
// dir, reading only the profiles' config files: no credential is minted and the
// GKE API is not asked anything, so it costs a directory read and is safe to
// call again while the process runs. It is the scope question -- does a profile
// name this cluster? -- as distinct from Discover's reachability question, and
// the two answers differ for every profile Discover skips.
//
// Reported to onSkip (nil to ignore), with the directory name and why, is
// every entry dropped for a reason that could be a cluster profile naming
// nothing on this read: a config that cannot be read or parsed, a
// cluster_identity that is present but incomplete, and a cluster- prefixed
// entry whose config is absent or carries no block at all (the scaffold writes
// the identity after the template copy, so a read landing between sees exactly
// this) or which is a symlink leading nowhere. Silent are hidden entries, plain
// files, the reserved profiles (default and platform, never a cluster's), and
// an entry without the prefix that has no config, a config with no block, or
// is a symlink leading nowhere -- the shapes the read cannot tell from a
// non-cluster profile or an entry that was never a profile (the old
// plugin-mount layout left one directory per plugin on the volume), which a
// caller that saw it name a cluster keeps on its own record. A listing that failed on
// any of these would make one broken profile hide the whole fleet, but a
// profile dropped here is a cluster the caller will treat as unnamed, and the
// caller has to be able to say so. Only a directory that cannot be read at all
// is an error, because then the scope is unknown rather than empty.
func ReadIdentities(dir string, onSkip func(profile string, err error)) ([]ProfileIdentity, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, fmt.Errorf("read profiles dir %s: %w", dir, err)
	}
	skip := func(profile string, err error) {
		if onSkip != nil {
			onSkip(profile, err)
		}
	}
	var ids []ProfileIdentity
	for _, e := range entries {
		name := e.Name()
		if strings.HasPrefix(name, hiddenPrefix) || reservedProfiles[name] {
			continue
		}
		clusterProfile := strings.HasPrefix(name, clusterProfilePrefix)
		if !e.IsDir() {
			// os.ReadDir types an entry from its own bits, so a profile
			// reached through a link reports IsDir false; follow it. A link
			// that leads nowhere is reported under the prefix only, like the
			// other shapes that could as well be no profile at all.
			if e.Type()&os.ModeSymlink == 0 {
				continue
			}
			info, err := os.Stat(filepath.Join(dir, name))
			if err != nil {
				if clusterProfile {
					skip(name, fmt.Errorf("symlink cannot be followed: %w", err))
				}
				continue
			}
			if !info.IsDir() {
				if clusterProfile {
					skip(name, errors.New("symlink does not lead to a directory"))
				}
				continue
			}
		}
		cfg, err := readProfileConfig(filepath.Join(dir, name, profileConfigFile))
		if err != nil {
			skip(name, err)
			continue
		}
		if cfg == nil {
			if clusterProfile {
				skip(name, ErrNoProfileConfig)
			}
			continue
		}
		id := cfg.ClusterIdentity
		switch {
		case id.complete():
			ids = append(ids, ProfileIdentity{Profile: name, Identity: id})
		case id != (Identity{}):
			skip(name, fmt.Errorf("cluster_identity is incomplete: %q", id.String()))
		case clusterProfile:
			skip(name, ErrNoClusterIdentity)
		}
	}
	return ids, nil
}

// Discover scans a Hermes profiles directory (normally /opt/data/profiles) and
// returns one Cluster per Cluster Agent profile found.
//
// This runs once, at the caller's startup, so the result is a snapshot rather
// than something kept in step with the directory. A cluster onboarded later is
// not reached until the process restarts, and one torn down later leaves the
// caller holding a config for a control plane that is gone. Re-scanning on an
// interval and reconciling against the result is deliberate follow-up work, not
// done here.
//
// A subdirectory is treated as a cluster profile only if its config.yaml
// carries a complete cluster_identity block. That is how non-cluster profiles
// ("default", "platform") are skipped: testing for the data we need is more
// durable than hardcoding a list of names that the Python side may extend.
// ReadIdentities does name those two, and only to stay silent about them when
// they are broken: for the scope the question is not "which profiles address a
// cluster" but "which drops are worth a line", and a reserved profile is never
// a cluster's whatever state it is in.
//
// The identity is also the whole of what a config is built from, and that is a
// deliberate change of source. Discovery used to require a kubeconfig.yaml
// beside config.yaml and read the API server address out of it, which stopped
// working the moment the shell moved into its own pod: `gcloud container
// clusters get-credentials` now runs there, so the file it writes lands on the
// sandbox's volume and this process never sees it. Every profile created after
// that change would have been silently missed. Asking the GKE API for the
// address removes the dependency rather than reinstating it — and it has to be
// removed rather than reinstated, because anything read back out of the sandbox
// is writable by the model, and a caller attaches a cloud-platform token to
// whatever host it is told to talk to.
//
// A profile that looks like a cluster profile but fails to load is skipped, not
// fatal. Dropping one cluster is bad; the alternative is worse, because these
// errors would propagate out of the caller's startup before it has built
// anything, so a single unparseable config.yaml would stop it reaching any
// cluster at all — including the one whose own client would have been fine.
// Every skip goes to OnSkip, so "this cluster is not being reached" stays
// visible and alertable without being fatal.
//
// Failing to read the directory splits two ways, because a restart fixes one
// kind of failure and not the other.
//
// A directory that does not exist yet is an error return. It is written by
// another process that may simply not have run, so exiting is what makes the
// caller self-healing: whatever supervises it restarts it, and the next attempt
// succeeds once the directory appears. Degrading instead would be permanent —
// discovery runs once, so a process that starts without profiles keeps going
// without them until something else restarts it, which is a far worse outcome
// than a few seconds of restarts at boot.
//
// Any other read error — permissions, I/O — is not something a restart will
// fix, so those go to OnSkip and return no clusters rather than crashlooping
// forever.
//
// Finding none is not an error either. A freshly installed harness has no
// Cluster Agent profiles until the first cluster-agent-reconcile tick creates
// them, so an empty result is a normal startup state rather than a
// misconfiguration. The caller decides whether that leaves it with nothing to
// do.
func (d Discoverer) Discover(ctx context.Context, dir string) ([]Cluster, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, fmt.Errorf("profiles dir %s does not exist yet (it is created by the platform agent; exiting so the next start can pick it up): %w", dir, err)
		}
		d.skip(NoProfile, fmt.Errorf("cannot read profiles dir %s, no profile clusters will be reached: %w", dir, err))
		return nil, nil
	}
	var clusters []Cluster
	// Minted on first use, then shared: every profile authenticates as the same
	// pod identity, and a process with no cluster profiles at all -- which is
	// every single-cluster install -- should not pay for a credential it never
	// uses.
	var tokenSource oauth2.TokenSource
	// Keyed on the full project/location/cluster triple, not the bare name:
	// two clusters called "prod" in different locations are two clusters, and
	// both get their own profile. See Identity.String.
	seen := make(map[string]string) // identity -> profile it came from
	for _, e := range entries {
		if !e.IsDir() || strings.HasPrefix(e.Name(), hiddenPrefix) {
			continue
		}
		home := filepath.Join(dir, e.Name())

		identity, err := ReadIdentity(filepath.Join(home, profileConfigFile))
		if err != nil {
			d.skip(e.Name(), err)
			continue
		}
		if identity == nil {
			continue // not a cluster profile
		}
		// Before the token and the describe below, which is the whole point of
		// the field. Also before the duplicate check: two profiles claiming a
		// cluster nobody wants is not a collision worth reporting.
		if !d.want(*identity) {
			continue
		}
		if prev, dup := seen[identity.String()]; dup {
			d.skip(e.Name(), fmt.Errorf("cluster %s is already claimed by profile %s", identity, prev))
			continue
		}

		if tokenSource == nil {
			tokenSource, err = d.tokenSource(ctx)
			if err != nil {
				d.skip(e.Name(), fmt.Errorf("this pod has no Google credentials, so its control plane cannot be reached: %w", err))
				continue
			}
		}
		described, err := d.describe(ctx, *identity)
		if err != nil {
			d.skip(e.Name(), fmt.Errorf("asking the GKE API where %s is: %w", identity, err))
			continue
		}
		cfg, err := ClientConfigForIdentity(described)
		if err != nil {
			d.skip(e.Name(), err)
			continue
		}
		UseGoogleTokenSource(cfg, tokenSource)

		seen[identity.String()] = e.Name()
		clusters = append(clusters, Cluster{
			Identity:  *identity,
			Profile:   e.Name(),
			Config:    cfg,
			Autopilot: described.Autopilot != nil && described.Autopilot.Enabled,
		})
	}
	return clusters, nil
}
