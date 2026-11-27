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
	"fmt"
	"strings"

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
)

const (
	// helmReleaseNameAnnotation and helmReleaseNamespaceAnnotation are what
	// Helm stamps on every object a release owns.
	helmReleaseNameAnnotation      = "meta.helm.sh/release-name"
	helmReleaseNamespaceAnnotation = "meta.helm.sh/release-namespace"

	// profilesNamespaceSuffix names the install's second namespace after its
	// first, as the chart and the operator do (profileNamespaceSuffix in
	// k8s-operator/internal/controller/agentprofile_namespace.go).
	profilesNamespaceSuffix = "-profiles"

	// rbacGroup and admissionGroup hold the cluster-scoped objects the chart
	// renders that the join's ServiceAccount cannot read, so their release
	// annotations are out of reach and they are matched by name instead.
	rbacGroup      = "rbac.authorization.k8s.io"
	admissionGroup = "admissionregistration.k8s.io"

	// releaseNameSeparator follows the release name in the name of every such
	// object (tests/test_chart_cluster_scoped_names.py holds the chart to it).
	releaseNameSeparator = "-"

	crdGroup    = "apiextensions.k8s.io"
	crdResource = "customresourcedefinitions"

	// productCRDSuffix ends the name of every CRD kube-agents ships. A suffix
	// and not a list: the detector that reads an upgrade is the one the
	// upgrade replaces, and it has to recognise a CRD the new release adds.
	productCRDSuffix = ".kubeagents.x-k8s.io"

	// helmReleaseRecordPrefix and helmReleaseRecordRevisionInfix frame the
	// name of the Secret Helm creates for each revision of a release:
	// sh.helm.release.v1.<release>.v<revision>.
	helmReleaseRecordPrefix        = "sh.helm.release.v1."
	helmReleaseRecordRevisionInfix = ".v"
	revisionDigits                 = "0123456789"
	secretsResource                = "secrets"
	createVerb                     = "create"
	updateVerb                     = "update"
	patchVerb                      = "patch"

	// deleteCollectionVerb is the audit verb for a call that deletes a
	// collection of objects. An upgrade makes none, so it is carded as a
	// delete is.
	deleteCollectionVerb = "deletecollection"

	// ownReleaseSeparator splits --own-release into namespace and name.
	ownReleaseSeparator = "/"

	// ownReleaseField, releaseRevisionField and releaseRevisionEndField are
	// appended to the DRIFT line of a record the own-release rule marks, as
	// backlogMarker is for a late one.
	ownReleaseField         = "own_release"
	releaseRevisionField    = "release_revision"
	releaseRevisionEndField = "release_revision_end"
)

// ownReleaseKind says which part of the own-release rule a record matched.
// The daemon treats the two apart: a CRD write is digested on its own, and a
// release write only while the same principal has a Helm upgrade under way
// (releaseRevision marks its start and its end).
type ownReleaseKind string

const (
	ownReleaseNone   ownReleaseKind = ""
	ownReleaseCRD    ownReleaseKind = "crd"
	ownReleaseObject ownReleaseKind = "release"
)

// helmRelease names one Helm release.
type helmRelease struct {
	Namespace string
	Name      string
}

func (r helmRelease) String() string {
	return r.Namespace + ownReleaseSeparator + r.Name
}

// parseOwnRelease reads --own-release. Empty is the zero value, which turns
// the rule off.
func parseOwnRelease(value string) (helmRelease, error) {
	if value == "" {
		return helmRelease{}, nil
	}
	namespace, name, found := strings.Cut(value, ownReleaseSeparator)
	if !found || namespace == "" || name == "" || strings.Contains(name, ownReleaseSeparator) {
		return helmRelease{}, fmt.Errorf("--own-release=%q is not <namespace>/<name>", value)
	}
	return helmRelease{Namespace: namespace, Name: name}, nil
}

// releaseOf reads the Helm release a live object says it belongs to.
func releaseOf(obj *unstructured.Unstructured) helmRelease {
	annotations := obj.GetAnnotations()
	return helmRelease{
		Namespace: annotations[helmReleaseNamespaceAnnotation],
		Name:      annotations[helmReleaseNameAnnotation],
	}
}

// ownRelease is the Helm release this install was made by and the cluster it
// runs in. The zero value matches nothing.
type ownRelease struct {
	release helmRelease
	cluster clusterIdentity
}

// wrote reports whether a record is a write an install or upgrade of
// kube-agents makes to its own objects. Such a record goes to the digest
// instead of a card: it runs under the installer's account, so the classifier
// sees a person, and a card each spent the day's drift budget on the product's
// own upgrade.
//
// Most tests read the record alone, because the join's ServiceAccount cannot
// read about half of what an upgrade writes (RBAC and admission policies).
// Whatever no test matches keeps its card: a delete, a subresource,
// another cluster, an object elsewhere that does not name the release. A
// kubectl write is carded because the installer applies the chart through
// Helm's Go client, with one exception: it applies the product's CRDs with
// kubectl, so those match with any client. The user agent, a name and the
// annotations are all the writer's to choose, so a match alone does not send
// a release write to the digest: the daemon digests it only while the same
// principal has a Helm upgrade under way, and cards it otherwise.
func (o ownRelease) wrote(event DriftEvent) ownReleaseKind {
	record := event.Record
	ref := record.Resource
	if o.release == (helmRelease{}) || recordIdentity(record) != o.cluster {
		return ownReleaseNone
	}
	if record.Verb == deleteVerb || record.Verb == deleteCollectionVerb || ref.Subresource != "" {
		return ownReleaseNone
	}
	if isProductCRD(ref) {
		return ownReleaseCRD
	}
	if strings.HasPrefix(record.UserAgent, kubectlUserAgentPrefix) {
		return ownReleaseNone
	}
	if o.ownsNamespace(ref.Namespace) || o.namesClusterObject(ref) {
		return ownReleaseObject
	}
	if event.Outcome == joinEnriched && event.HelmRelease == o.release {
		return ownReleaseObject
	}
	return ownReleaseNone
}

// releaseRevision reads the start or the end of a Helm upgrade off a record.
// Helm creates the release's next record Secret first in every upgrade and
// rollback, and updates that Secret when the upgrade ends, deployed or
// failed. started is the revision a create names, ended the one an update
// names; both are "" for any other record, and for a kubectl write. Read off
// the name: the join may not read Secrets.
func (o ownRelease) releaseRevision(record AuditRecord) (started, ended string) {
	ref := record.Resource
	if o.release == (helmRelease{}) || recordIdentity(record) != o.cluster {
		return "", ""
	}
	if strings.HasPrefix(record.UserAgent, kubectlUserAgentPrefix) {
		return "", ""
	}
	if ref.Group != "" || ref.Resource != secretsResource || ref.Namespace != o.release.Namespace || ref.Subresource != "" {
		return "", ""
	}
	revision, found := strings.CutPrefix(ref.Name, helmReleaseRecordPrefix+o.release.Name+helmReleaseRecordRevisionInfix)
	if !found || revision == "" || strings.Trim(revision, revisionDigits) != "" {
		return "", ""
	}
	switch record.Verb {
	case createVerb:
		return revision, ""
	case updateVerb, patchVerb:
		return "", revision
	}
	return "", ""
}

// isProductCRD reports whether ref is one of kube-agents' own CRDs.
func isProductCRD(ref ResourceRef) bool {
	return ref.Group == crdGroup && ref.Resource == crdResource && strings.HasSuffix(ref.Name, productCRDSuffix)
}

// ownsNamespace reports whether namespace is one of the install's two.
func (o ownRelease) ownsNamespace(namespace string) bool {
	return namespace != "" && (namespace == o.release.Namespace || namespace == o.release.Namespace+profilesNamespaceSuffix)
}

// namesClusterObject reports whether ref is a cluster-scoped RBAC or admission
// object named after the release.
func (o ownRelease) namesClusterObject(ref ResourceRef) bool {
	if ref.Namespace != "" || (ref.Group != rbacGroup && ref.Group != admissionGroup) {
		return false
	}
	return strings.HasPrefix(ref.Name, o.release.Name+releaseNameSeparator)
}
