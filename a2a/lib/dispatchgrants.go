package lib

import (
	"fmt"
	"hash/fnv"
	"strconv"
	"strings"
)

// The dispatcher's per-profile grants, split across connections (#5381).
//
// One connection holding every profile's grants can't be minted past about
// 1,000 profiles: the callout's reply carrying the user JWT is one NATS
// message, capped at 1 MiB. So the dispatcher opens a control connection for
// its fixed grants, plus DispatcherBuckets connections. Each one names its
// bucket in the CONNECT password (DispatcherBucketCredential), beside the
// pod's bus token. The callout derives that connection's grants from the
// profiles that hash to the bucket (DispatcherBucket), using
// DispatcherGrants. The token decides who connects. The bucket only picks
// which slice of that principal's grants this connection holds, so it is not
// a credential.

// DispatcherBuckets is K, the number of bucket connections. The dispatcher
// and the callout must agree on it, and the bucket credential spells it
// ("7/32"), so a pair built with different values refuses the connection
// instead of granting the wrong slice. Changing it moves almost every
// profile to another bucket.
const DispatcherBuckets = 32

// DispatcherBucket is the bucket a profile's grants ride on: fnv32a(name)
// mod DispatcherBuckets.
func DispatcherBucket(profile string) int {
	h := fnv.New32a()
	_, _ = h.Write([]byte(profile))
	return int(h.Sum32() % DispatcherBuckets)
}

// DispatcherBucketCredential is what a bucket connection sends as its CONNECT
// password: "<bucket>/<DispatcherBuckets>".
func DispatcherBucketCredential(bucket int) string {
	return strconv.Itoa(bucket) + "/" + strconv.Itoa(DispatcherBuckets)
}

// ParseDispatcherBucketCredential reads a bucket connection's password. It
// accepts only the canonical spelling DispatcherBucketCredential writes, with
// this build's K and a bucket in [0, K). Anything else is an error, so a
// stray password is refused rather than read as some bucket.
func ParseDispatcherBucketCredential(s string) (int, error) {
	b, k, ok := strings.Cut(s, "/")
	if !ok {
		return 0, fmt.Errorf("a dispatcher bucket is <bucket>/<buckets>")
	}
	if k != strconv.Itoa(DispatcherBuckets) {
		return 0, fmt.Errorf("the dispatcher asks for %q buckets; this callout splits its grants into %d", k, DispatcherBuckets)
	}
	n, err := strconv.Atoi(b)
	if err != nil || n < 0 || n >= DispatcherBuckets || strconv.Itoa(n) != b {
		return 0, fmt.Errorf("dispatcher bucket %q is not one of 0..%d", b, DispatcherBuckets-1)
	}
	return n, nil
}

// DispatcherGrantProfile is one profile the dispatcher holds grants for: an
// admitted profile with the tiers its durables serve, or a draining one,
// which has no durables left.
type DispatcherGrantProfile struct {
	Name     string
	Tiers    []string
	Draining bool
}

// DispatcherSubjects is a set of NATS permission subjects.
type DispatcherSubjects struct {
	Publish   []string
	Subscribe []string
}

// dispatcherSubmitKinds are the kinds the operator makes a durable for, per
// tier, in this order.
var dispatcherSubmitKinds = []string{SubmitKindTop, SubmitKindChild}

// DispatcherGrants lists the per-profile subjects the dispatcher needs for
// profiles, in the order given. For each admitted profile, each tier:
//   - every durable (top and child): INFO, MSG.NEXT and ACK on PROFILE_SUBMITS.
//
// For every profile, draining or not:
//   - publish on a2a.tasks.<profile>.*.supervisor (failing a task it can't run);
//   - publish on the stop stream's direct get for a2a.stop.<profile>.*
//     (catching up after a restart);
//   - subscribe on a2a.stop.<profile>.*.
//
// Every subject spells the profile's name: no wildcard sits where a profile
// name goes. NATS can't express a prefix, and a whole-token wildcard over the
// profile position in a2a.tasks would reach the sessions' supervisor subjects
// too. These are the subjects the operator's dispatcherIdentity spelled
// before #5381, and lib is now their one spelling.
func DispatcherGrants(profiles []DispatcherGrantProfile) DispatcherSubjects {
	var out DispatcherSubjects
	for _, p := range profiles {
		if !p.Draining {
			for _, tier := range p.Tiers {
				for _, kind := range dispatcherSubmitKinds {
					d := ProfileDurableName(p.Name, tier, kind)
					out.Publish = append(out.Publish,
						"$JS.API.CONSUMER.INFO."+ProfileSubmitStream+"."+d,
						"$JS.API.CONSUMER.MSG.NEXT."+ProfileSubmitStream+"."+d,
						"$JS.ACK."+ProfileSubmitStream+"."+d+".>",
					)
				}
			}
		}
	}
	for _, p := range profiles {
		out.Publish = append(out.Publish,
			"a2a.tasks."+p.Name+".*."+TaskClassSupervisor,
			"$JS.API.DIRECT.GET."+DispatchStopStream+"."+DispatchStopPrefix+p.Name+".*",
		)
		out.Subscribe = append(out.Subscribe, DispatchStopPrefix+p.Name+".*")
	}
	return out
}

// ProfilesInBucket is the profiles whose grants ride on bucket, in the order
// given.
func ProfilesInBucket(profiles []DispatcherGrantProfile, bucket int) []DispatcherGrantProfile {
	var out []DispatcherGrantProfile
	for _, p := range profiles {
		if DispatcherBucket(p.Name) == bucket {
			out = append(out, p)
		}
	}
	return out
}
