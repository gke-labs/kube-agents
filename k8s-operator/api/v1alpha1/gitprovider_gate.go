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

// gitprovider_gate.go — registered providers that this release refuses.
//
// A provider here has its table entry, its CRD enum value and its agent-side
// package, and every rule of its entry is tested. It is refused because a
// part of the install outside the provider is not ready for it: the fleet
// audit keeps its ledger in issues and finds its items by label, and a
// Bitbucket forge has neither until the ledger can live in the report store
// (docs/designs/version-control-support.md §8). A Bitbucket install declared
// before that would go FATAL at the first audit run.
//
// The release that wires the ledger deletes this file, the one call in
// LookupGitProvider, the call of releasedProviderNames in lookupGitProvider
// (back to providerNames), the test TestThisReleaseRefusesBothBitbucketProviders and
// the helper releaseBitbucket in bitbucket_test.go, and the sentence
// "Bitbucket is not available in this release." in
// docs/site/src/content/docs/operator/platformagent-crd.md. Nothing else reads
// it.

import (
	"fmt"
	"strings"
)

// bitbucketGateReason is what an administrator who declares a Bitbucket forge
// is told.
const bitbucketGateReason = "the fleet audit cannot yet keep its ledger on a forge with no issue tracker and no labels. " +
	"A later release turns this provider on"

// unreleasedGitProviders maps each refused provider to the reason.
var unreleasedGitProviders = map[string]string{
	GitProviderBitbucket:           bitbucketGateReason,
	GitProviderBitbucketDataCenter: bitbucketGateReason,
}

// releasedProviderNames is the registered providers less the refused ones,
// for the message that lists what an administrator may write.
func releasedProviderNames(table map[string]*GitProvider) []string {
	var names []string
	for _, name := range providerNames(table) {
		if _, refused := unreleasedGitProviders[name]; !refused {
			names = append(names, name)
		}
	}
	return names
}

// unreleasedGitProvider is why a registered provider is refused in this
// release, or nil.
func unreleasedGitProvider(name string) error {
	trimmed := strings.ToLower(strings.TrimSpace(name))
	if reason, refused := unreleasedGitProviders[trimmed]; refused {
		return fmt.Errorf("git provider %q is not available in this release: %s", trimmed, reason)
	}
	return nil
}
