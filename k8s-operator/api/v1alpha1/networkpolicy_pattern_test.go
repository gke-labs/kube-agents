/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package v1alpha1

import (
	"fmt"
	"net"
	"os"
	"regexp"
	"strings"
	"testing"

	"sigs.k8s.io/yaml"
)

// crdPath is the generated CRD the API server actually enforces. The test reads it
// rather than the +kubebuilder marker beside the field: a marker that never made it
// into the CRD constrains nothing, and this file is also hand-edited when
// controller-gen cannot run, so a typo in the YAML is worth catching here rather
// than in CI's byte-equality gate.
const crdPath = "../../config/crd/bases/kubeagents.x-k8s.io_platformagents.yaml"

// ipv4PatternMarker appears in every spec.networkPolicy pattern that admits an IPv4
// literal, and in no other pattern in the CRD.
const ipv4PatternMarker = "25[0-5]"

// collectPatterns walks the decoded CRD and returns every `pattern` value that
// contains marker, keyed by the pattern itself so a duplicate spelling collapses.
func collectPatterns(node any, marker string, out map[string]bool) {
	switch v := node.(type) {
	case map[string]any:
		for key, child := range v {
			if key == "pattern" {
				if s, ok := child.(string); ok && strings.Contains(s, marker) {
					out[s] = true
				}
				continue
			}
			collectPatterns(child, marker, out)
		}
	case []any:
		for _, child := range v {
			collectPatterns(child, marker, out)
		}
	}
}

// TestNetworkPolicyIPv4Patterns holds the octet bound in spec.networkPolicy's four
// IPv4-bearing patterns. The failure it exists to catch is silent at every layer: an
// address the CRD admits and net.ParseIP then refuses is dropped by the resolver, so
// a dnsClusterIPs pin reverts to discovery and the only trace is a log line. Go has
// refused an IPv4 field with a leading zero since 1.17, so admission has to as well.
func TestNetworkPolicyIPv4Patterns(t *testing.T) {
	raw, err := os.ReadFile(crdPath)
	if err != nil {
		t.Fatalf("failed to read %s: %v", crdPath, err)
	}
	var doc any
	if err := yaml.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("failed to parse %s: %v", crdPath, err)
	}

	patterns := map[string]bool{}
	collectPatterns(doc, ipv4PatternMarker, patterns)

	// dnsClusterIPs, metadataDaemon.endpoint, additionalEgress[].to[].cidr, and its
	// except items. A count that moves means a pattern was added or lost, and the
	// cases below no longer cover what they claim to.
	const wantPatterns = 4
	if len(patterns) != wantPatterns {
		t.Fatalf("found %d IPv4-bearing patterns in %s, want %d: %v", len(patterns), crdPath, wantPatterns, keys(patterns))
	}

	// Leading zeros, per octet position. net.ParseIP rejects every one.
	rejected := []string{"010.96.0.10", "10.096.0.10", "10.96.00.10", "10.96.0.010", "0010.96.0.10"}

	for pattern := range patterns {
		re, err := regexp.Compile(pattern)
		if err != nil {
			t.Errorf("pattern does not compile: %v\n%s", err, pattern)
			continue
		}

		// All four admit a bare host address, and that symmetry is deliberate: a
		// user writing 10.0.1.5 in `except` next to a `cidr` that takes 10.0.1.5
		// should not get an apply-time rejection from one field and not its
		// neighbour, quoting a 200-character regex to explain the difference. The
		// two prefix-bearing patterns therefore make the prefix optional; the two
		// bare-IP-only ones have no prefix alternative at all. Re-tightening either
		// prefix-bearing pattern fails here.
		if !re.MatchString("10.96.0.10") {
			t.Errorf("pattern rejects the bare host 10.96.0.10; every spec.networkPolicy IPv4 pattern accepts one: %s", pattern)
		}

		// Each pattern accepts a bare IP, a bare IP with a prefix, or only the
		// latter. Find the suffix this one wants before asserting anything, so the
		// test does not silently pass by feeding every pattern a string it rejects
		// for the wrong reason. The check above makes "" the answer for all four
		// today; the helper stays so a pattern added later that does require a
		// prefix still gets the octet coverage below rather than one error line.
		suffix, ok := acceptedSuffix(re)
		if !ok {
			t.Errorf("pattern rejects 10.96.0.10 both bare and with a /24: %s", pattern)
			continue
		}

		for _, bad := range rejected {
			if re.MatchString(bad + suffix) {
				t.Errorf("pattern admits the leading-zero form %q, which net.ParseIP refuses: %s", bad+suffix, pattern)
			}
			if net.ParseIP(bad) != nil {
				t.Errorf("net.ParseIP unexpectedly accepts %q; this test's premise no longer holds", bad)
			}
		}
		for _, bad := range []string{"256.1.1.1", "1.256.1.1", "300.1.1.1", "1.2.3", "1.2.3.4.5"} {
			if re.MatchString(bad + suffix) {
				t.Errorf("pattern admits the out-of-range address %q: %s", bad+suffix, pattern)
			}
		}

		// Every canonical octet value still admitted, so the tightening did not
		// narrow the pattern past leading zeros.
		for octet := 0; octet < 256; octet++ {
			addr := fmt.Sprintf("10.0.0.%d", octet)
			if !re.MatchString(addr + suffix) {
				t.Errorf("pattern rejects the valid address %q: %s", addr+suffix, pattern)
			}
			addr = fmt.Sprintf("%d.0.0.1", octet)
			if !re.MatchString(addr + suffix) {
				t.Errorf("pattern rejects the valid address %q: %s", addr+suffix, pattern)
			}
		}
	}
}

// acceptedSuffix reports the prefix suffix a pattern needs to admit 10.96.0.10:
// "" where a bare IP is allowed, "/24" where one is required.
func acceptedSuffix(re *regexp.Regexp) (string, bool) {
	for _, suffix := range []string{"", "/24"} {
		if re.MatchString("10.96.0.10" + suffix) {
			return suffix, true
		}
	}
	return "", false
}

func keys(m map[string]bool) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}
