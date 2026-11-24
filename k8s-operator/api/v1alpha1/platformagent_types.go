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
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// PlatformAgentModeNext is the one value spec.mode takes. The field is
// deprecated: "today" named a stack no release renders any more, so the
// admission webhook rewrites it, and an absent field, to this value, and the
// operator patches a stored "today" to it. The CRD markers on Mode below
// spell the same value by hand, since a marker cannot name a constant.
const PlatformAgentModeNext = "next"

// PlatformAgentSpec defines the desired state of PlatformAgent
type PlatformAgentSpec struct {
	AgentSpec `json:",inline"`

	// Harness configures the core execution environment and framework-level settings.
	// +required
	Harness *HarnessSpec `json:"harness,omitempty"`

	// Integration configures platform-specific external connections.
	// +optional
	Integration *PlatformAgentIntegrationSpec `json:"integration,omitempty"`

	// Mode is deprecated and ignored: every install runs the A2A stack (the
	// NATS bus and the A2A gateway), which "next" once selected.
	// The field stays so a stored object that still carries "today" remains
	// valid; the admission webhook rewrites "today" to "next", the schema
	// defaults an absent field to "next", and the operator patches a stored
	// "today" to "next" on its first pass. A later release removes the field.
	// +kubebuilder:validation:Enum=today;next
	// +kubebuilder:default=next
	// +optional
	Mode *string `json:"mode,omitempty"`

	// Scope declares which GCP projects, beyond the one the agent runs in, the
	// Cluster Agent reconcile enumerates for GKE clusters, and which projects and
	// clusters it leaves unmanaged. Absent, the reconcile lists the management project
	// alone, keeps the last declaration's exclusions and retires nothing; an empty
	// projects list in a present block drops the projects an earlier block declared.
	// The management project is always in scope and cannot be excluded. The design is docs/designs/multi-project-scope.md;
	// this is its phases 1 to 3: explicit projects, folders and organisations, and the
	// sharedVpcHosts and metricsScopes selectors, which resolve to explicit projects.
	// +optional
	Scope *ScopeSpec `json:"scope,omitempty"`

	// Hold pauses the agents' access to clusters' Kubernetes APIs without taking
	// them out of scope: an operator's lever for an incident, set with kubectl
	// patch and lifted by removing it. The credential broker and kube-proxy
	// refuse a held cluster's API to every caller, on the routes
	// spec.scope.exclude.clusters is refused on, and the refusal names the hold
	// and its reason so the agent reports it and stops rather than working
	// around it. A Cluster Agent task that targets a held cluster is refused for
	// every command, metadata reads included; the Platform Agent's own reads of
	// the GKE control plane for it (the project's cluster list, a describe) stay
	// allowed. Unlike an exclusion, a held cluster keeps its Cluster Agent
	// profile and its place in fleet answers and audits, and nothing restarts:
	// the operator renders the hold into the <name>-hold ConfigMap, which no
	// pod-template hash covers, the kubelet syncs the mounted file (usually
	// within a minute or two), and both components re-read it on the next
	// request. Like an exclusion it is checked when a request starts: a watch, a
	// followed log, or an exec, attach, port-forward or proxy stream opened before the
	// hold reached them keeps running until it ends. The installer does not
	// carry it: an upgrade keeps the live value.
	// +optional
	Hold *HoldSpec `json:"hold,omitempty"`

	// Storage sizes the agent's /opt/data volume and the shell sandbox's data
	// volume.
	// +optional
	Storage *PlatformAgentStorageSpec `json:"storage,omitempty"`

	// Tiers says which tier each person's profile tasks run at.
	// +optional
	Tiers *PlatformAgentTiersSpec `json:"tiers,omitempty"`

	// Schedules adjusts the built-in AgentSchedules the operator renders.
	// +optional
	Schedules *PlatformAgentSchedulesSpec `json:"schedules,omitempty"`
}

// PlatformAgentSchedulesSpec adjusts the operator's built-in AgentSchedules.
// A built-in's spec is the operator's to write, and the admission policy
// freezes it except spec.suspend, so an override is the one supported way
// to change a built-in without a release.
type PlatformAgentSchedulesSpec struct {
	// Overrides sets spec.suspend on built-in schedules, keyed on the
	// schedule's rendered name: a canary (<name>-canary) and its live row
	// are separate entries, so a suspend never carries from one phase to the
	// other. An entry naming no built-in is reported on the PlatformAgent's
	// BuiltinSchedules condition and otherwise ignored. A run-now-only
	// built-in, such as onboarding, takes only suspend: true, which stops
	// the operator from starting its run; suspend: false is refused and
	// reported, since it would put the schedule on its nominal cron.
	// +listType=map
	// +listMapKey=name
	// +kubebuilder:validation:MaxItems=64
	// +optional
	Overrides []AgentScheduleOverride `json:"overrides,omitempty"`
}

// AgentScheduleOverride sets one built-in schedule's spec.suspend.
type AgentScheduleOverride struct {
	// Name is the built-in schedule's rendered name.
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=63
	Name string `json:"name"`
	// Suspend is the value the operator writes to the schedule's
	// spec.suspend.
	Suspend bool `json:"suspend"`
}

// PlatformAgentTiersSpec maps people to tiers (#3659).
type PlatformAgentTiersSpec struct {
	// Membership lists the people whose profile tasks run at a tier other
	// than read. Anyone not listed runs at read, and the console runs at
	// read whatever this says, since everyone on it shares one principal. A
	// thread whose authors are at different tiers can't start a profile task,
	// and a steer from an author at another tier than the running task's is
	// refused. A member whose tier no Tier defines is refused profile tasks
	// until one does; they don't fall back to read. The tier must be
	// interactive: the admission webhook refuses an unattended-mode Tier.
	// +listType=map
	// +listMapKey=backend
	// +listMapKey=user
	// At most 200 entries, so the list fits the one environment variable the
	// gateway reads it from (TestTheLargestMembershipFitsOneEnvString).
	// +kubebuilder:validation:MaxItems=200
	// +optional
	Membership []TierMember `json:"membership,omitempty"`
}

// TierMember maps one person on one chat backend to a tier.
type TierMember struct {
	// Backend is the surface the person writes from: one whose requester is
	// one verified person. The a2a door's eval class isn't one: its callers
	// name themselves under a shared token; its Google and Entra sign-in
	// classes are.
	// +kubebuilder:validation:Enum=gchat;slack;discord;a2a-google;a2a-entra
	// +required
	Backend string `json:"backend"`

	// User is the person's id on that backend: their email on Google Chat
	// (compared without case) and on a2a-google (as their Google token
	// carries it), their Entra object id on a2a-entra (as their Entra token
	// carries it), their user id on Slack or Discord.
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=128
	// +kubebuilder:validation:Pattern=`^[^,=\s]+$`
	// +required
	User string `json:"user"`

	// Tier is the name of an interactive-mode Tier.
	// +kubebuilder:validation:MaxLength=63
	// +kubebuilder:validation:Pattern=`^[a-z0-9]([-a-z0-9]*[a-z0-9])?$`
	// +required
	Tier string `json:"tier"`
}

// HoldSpec names the clusters whose Kubernetes API the agents must not reach
// until an operator lifts the hold.
type HoldSpec struct {
	// All holds every cluster, the one the harness runs in included.
	// +optional
	All bool `json:"all,omitempty"`

	// Clusters holds single clusters by the full triple, matched as
	// spec.scope.exclude.clusters entries are.
	// +kubebuilder:validation:MaxItems=100
	// +listType=map
	// +listMapKey=projectId
	// +listMapKey=location
	// +listMapKey=clusterName
	// +optional
	Clusters []ScopeClusterRef `json:"clusters,omitempty"`

	// Reason is shown in every refusal, so the agent can tell its user why
	// the cluster is paused (an incident link, a change window).
	// +kubebuilder:validation:MaxLength=256
	// +optional
	Reason string `json:"reason,omitempty"`
}

// PlatformAgentStorageSpec sizes the agent's persistent data volumes.
type PlatformAgentStorageSpec struct {
	// Size is the request of the <name>-data claim and of the shell sandbox's
	// data claim. The operator requests the same base size for both and grows
	// the sandbox's claim first, so the sandbox mirror fits;
	// expectedClusterProfiles can raise the agent's claim alone. A StorageClass or a
	// quota that refuses the growth of one claim can leave the two claims at
	// different sizes. Absent, the operator default applies (50Gi). Raising it
	// expands both existing claims online where the StorageClass sets
	// allowVolumeExpansion; where it does not, the claims keep their size and
	// the DataVolumeNotExpanded condition says so. The admission webhook refuses
	// a lower value and a value of zero or less, because a claim cannot shrink.
	// Removing the field never shrinks a claim.
	// +optional
	Size *resource.Quantity `json:"size,omitempty"`

	// ExpectedClusterProfiles is how many Cluster Agent profiles the install is
	// expected to hold, one per cluster in scope. It sizes the agent's own
	// <name>-data claim to at least a base for the Platform Agent's stores plus
	// a per-profile budget for each, 8Gi plus 400Mi per profile rounded up to
	// whole GiB, measured at the default 90-day session retention. The claim
	// takes the larger of that and Size, or the operator
	// default when Size is absent, so this only ever raises it. Raising it
	// expands the existing claim online where the StorageClass allows. The
	// shell sandbox's claim holds no session stores and keeps Size or the
	// default. The formula passes the 50Gi default above 107 profiles.
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=20000
	// +optional
	ExpectedClusterProfiles *int32 `json:"expectedClusterProfiles,omitempty"`
}

// ScopeSpec is the opt-in set of projects, folders and organisations, plus the Shared
// VPC host and Metrics Scope selectors that resolve to projects, the Cluster Agent
// reconcile manages.
type ScopeSpec struct {
	// Projects lists GCP project IDs whose GKE clusters get Cluster Agent
	// profiles, in addition to the management project. The agent's service
	// account needs the read roles in each: the install's Terraform binds them
	// from the same scope input that renders this block, and a CR edited by
	// hand needs the same grants made by hand; a project it cannot list is
	// reported with the reason (denied, api-disabled, unreachable) and its
	// existing profiles are kept.
	// +kubebuilder:validation:MaxItems=100
	// +kubebuilder:validation:items:Pattern=`^[a-z][a-z0-9-]{4,28}[a-z0-9]$`
	// +listType=set
	// +optional
	Projects []string `json:"projects,omitempty"`

	// Folders lists GCP folder IDs (numeric) whose every project, at any depth,
	// contributes its GKE clusters, resolved through Cloud Asset Inventory on each
	// run. The agent's service account needs roles/cloudasset.viewer and the read
	// roles on the folder: the install's Terraform binds them from the same scope
	// input that renders this block, and a CR edited by hand needs the same grants
	// made by hand; a folder it cannot read freezes its previous members and is
	// reported with the reason.
	// +kubebuilder:validation:MaxItems=100
	// +kubebuilder:validation:items:Pattern=`^[0-9]{1,20}$`
	// +listType=set
	// +optional
	Folders []string `json:"folders,omitempty"`

	// Organizations lists GCP organisation IDs (numeric), resolved the way Folders
	// are.
	// +kubebuilder:validation:MaxItems=100
	// +kubebuilder:validation:items:Pattern=`^[0-9]{1,20}$`
	// +listType=set
	// +optional
	Organizations []string `json:"organizations,omitempty"`

	// SharedVpcHosts lists Shared VPC host project IDs; every service project
	// attached to the host contributes its GKE clusters, resolved on each run from
	// the Compute API (gcloud compute shared-vpc list-associated-resources). The
	// host project itself is not included: name it in projects if its clusters are
	// wanted. A project that is not a Shared VPC host resolves to no members: the
	// API answers HTTP 400 for it, which the reconcile reads as an empty membership
	// rather than a failed lookup, so a misdeclared host cannot hold the scope prune.
	// Nothing is inherited through a Shared VPC, so the agent's service account
	// needs the read roles in each resolved project, and compute.projects.get in
	// the host project (roles/compute.viewer carries it): the install's Terraform
	// resolves the host at plan time and binds both (the design's §10 step 3),
	// so a service project attached after the last apply reads denied until the
	// next one; a CR edited by hand needs the grants made by hand. A lookup that
	// fails freezes
	// the selector's previous members and holds the scope prune, and is reported
	// in the snapshot's containers array under sharedVpcHosts/<host>.
	// +kubebuilder:validation:MaxItems=100
	// +kubebuilder:validation:items:Pattern=`^[a-z][a-z0-9-]{4,28}[a-z0-9]$`
	// +listType=set
	// +optional
	SharedVpcHosts []string `json:"sharedVpcHosts,omitempty"`

	// MetricsScopes lists Cloud Monitoring Metrics Scope scoping-project IDs; every
	// monitored project of the scope contributes its GKE clusters, resolved on each
	// run from the Monitoring API (gcloud beta monitoring metrics-scopes describe).
	// The API names monitored projects by number, so each is mapped to its ID with
	// gcloud projects describe, and a monitored project the agent cannot read is
	// reported by number under the naming call's outcome (denied for a 403).
	// Nothing is inherited through a Metrics Scope, so the agent's service account
	// needs the read roles in each resolved project: the install's Terraform
	// resolves the scope at plan time and binds them (the design's §10 step 3),
	// so a project linked after the last apply reads denied until the next one;
	// a CR edited by hand needs the grants made by hand. The lookup itself needs
	// to read the scope in the scoping project:
	// roles/monitoring.metricsScopesViewer (resourcemanager.projects.get and
	// resourcemanager.projects.list) is the narrowest role that grants it, and the
	// read roles the scope binds carry both between them. The Monitoring API has to
	// be enabled in the scoping project. A lookup that fails freezes the selector's
	// previous members and holds the scope prune, and is reported in the
	// snapshot's containers array under metricsScopes/<scope>.
	// +kubebuilder:validation:MaxItems=100
	// +kubebuilder:validation:items:Pattern=`^[a-z][a-z0-9-]{4,28}[a-z0-9]$`
	// +listType=set
	// +optional
	MetricsScopes []string `json:"metricsScopes,omitempty"`

	// MaxProjects caps the resolved set the reconcile lists on each run, the
	// management project included: explicit projects, selector members and
	// container members fill it in a fixed order, and a project past it reads
	// over-cap (profiles kept, nothing created). The reconcile sizes its listing
	// workers and budget from it, and the bootstrap gate its ceiling, so an estate
	// of a few hundred projects declares the number it has rather than reading half
	// of itself over-cap. The MaxItems=100 cap on each declared list is unchanged: a
	// hand-written list past a hundred entries is the shape folders exist for. The
	// install's Terraform refuses a plan whose explicit projects and selector
	// members exceed it (while a selector is declared or the cap is below its
	// default), and renders the same value here from its scope input.
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=5000
	// +kubebuilder:default=100
	// +optional
	MaxProjects *int32 `json:"maxProjects,omitempty"`

	// Exclude subtracts projects and clusters after every selector has
	// contributed.
	// +optional
	Exclude *ScopeExcludeSpec `json:"exclude,omitempty"`
}

// ScopeExcludeSpec names what the scope must never manage or read.
type ScopeExcludeSpec struct {
	// Projects are project IDs or shell-style globs (`*-sandbox`) matched against
	// each resolved project ID. A match on the management project is ignored and
	// recorded in the scope snapshot, never applied.
	// +kubebuilder:validation:MaxItems=100
	// +kubebuilder:validation:items:Pattern=`^[a-z0-9*?\[\]!-]{1,63}$`
	// +listType=set
	// +optional
	Projects []string `json:"projects,omitempty"`

	// Clusters names single clusters by the full triple, because cluster names
	// are unique only within a project and location. This replaces the
	// RECONCILE_EXCLUDE environment variable, which matched bare names across
	// every project.
	// An excluded cluster gets no Cluster Agent, and fleet answers, the
	// onboarding sweep and the fleet-audit collectors that list clusters leave
	// it out. The credential broker and kube-proxy refuse every route to its
	// Kubernetes API, for every caller, the agents' own commands included: a
	// kubeconfig, get-credentials, kube-proxy, the watchers' relay and the gke
	// MCP server's in-cluster tools. Spell entries by project ID: one spelled
	// by project number refuses its location and name in any project. A GCP
	// metadata read that names it is refused too (a cluster describe, a
	// node-pool read, the gke MCP server's get_cluster, get_node_pool and
	// list_node_pools), and a logging read leaves out its entries. Project-wide
	// reads stay allowed (the project's cluster list, for one), and these
	// still cover it through them only:
	// the daily upgrade-readiness watch's version table (its in-cluster
	// readiness rules skip the cluster), the networking audit's subnet usage,
	// the GCE audit's instance listing, and the gke MCP server's list_clusters.
	// The broker enforces it, not IAM: the agent's roles still reach the
	// cluster, so the service account itself is not fenced off.
	// +kubebuilder:validation:MaxItems=100
	// +listType=map
	// +listMapKey=projectId
	// +listMapKey=location
	// +listMapKey=clusterName
	// +optional
	Clusters []ScopeClusterRef `json:"clusters,omitempty"`
}

// ScopeClusterRef identifies one GKE cluster.
type ScopeClusterRef struct {
	// ProjectID is the project the cluster lives in.
	// +kubebuilder:validation:Pattern=`^[a-z0-9][a-z0-9-]*$`
	// +kubebuilder:validation:MaxLength=63
	ProjectID string `json:"projectId"`

	// Location is the cluster's region or zone.
	// +kubebuilder:validation:Pattern=`^[a-z0-9][a-z0-9-]*$`
	// +kubebuilder:validation:MaxLength=63
	Location string `json:"location"`

	// ClusterName is the GKE cluster's name.
	// +kubebuilder:validation:Pattern=`^[a-z0-9][a-z0-9-]*$`
	// +kubebuilder:validation:MaxLength=63
	ClusterName string `json:"clusterName"`
}

// PlatformAgentIntegrationSpec extends common IntegrationSpec with platform-specific connections.
//
// The rules below are the schema half of the forge declaration's checks: the
// two spellings are exclusive, and a repository names a declared forge. The
// webhook refuses the same CRs, but the chart ships it off and its default
// failurePolicy is Ignore, and a CR the schema admitted would reach the
// reconciler, which seeds only the entries it accepts and reports the rest as
// Degraded long after the apply that introduced them succeeded.
// +kubebuilder:validation:XValidation:rule="!(has(self.github) && (has(self.forges) || has(self.repositories)))",message="set integration.forges and integration.repositories, or integration.github, not both; integration.github is a deprecated alias for one forge with provider github"
// +kubebuilder:validation:XValidation:rule="!has(self.repositories) || self.repositories.all(r, has(self.forges) && self.forges.exists(f, f.name == r.forge))",message="every integration.repositories entry must name a forge declared in integration.forges"
type PlatformAgentIntegrationSpec struct {
	IntegrationSpec `json:",inline"`

	// GoogleChat configures the Google Chat integration.
	// +optional
	GoogleChat *GoogleChatSpec `json:"googleChat,omitempty"`

	// Slack configures the Slack integration.
	// +optional
	Slack *SlackSpec `json:"slack,omitempty"`

	// Teams configures the Microsoft Teams integration.
	// +optional
	Teams *TeamsSpec `json:"teams,omitempty"`
}

// GoogleChatSpec contains the configuration for the Google Chat integration,
// enabling communication and event routing via Google Chat.
// +kubebuilder:validation:XValidation:rule="!has(self.enabled) || self.enabled == false || (has(self.projectId) && has(self.topicName) && has(self.subscriptionName))",message="projectId, topicName, and subscriptionName are required when Google Chat integration is enabled"
type GoogleChatSpec struct {
	// Enabled toggles the Google Chat integration.
	// +kubebuilder:default=false
	// +optional
	Enabled *bool `json:"enabled,omitempty"`

	// ProjectID is the target GCP Project ID for Pub/Sub.
	// +optional
	ProjectID string `json:"projectId,omitempty"`

	// TopicName is the GCP Chat Topic Name.
	// +optional
	TopicName string `json:"topicName,omitempty"`

	// SubscriptionName is the GCP Chat Subscription Name.
	// +optional
	SubscriptionName string `json:"subscriptionName,omitempty"`

	// AllowedUsers is a list of allowed users. If not present, all users will be allowed.
	// +listType=set
	// +optional
	AllowedUsers []string `json:"allowedUsers,omitempty"`

	// HomeChannel is the home channel Chat address.
	// +optional
	HomeChannel string `json:"homeChannel,omitempty"`

	// HomeChannelPolicy ("auto", also absent, or "manual") says whether the
	// A2A gateway may learn the Google Chat home space when HomeChannel is
	// empty. Under "auto" the space of the first top-level mention of the
	// agent in a named space, never a direct message or a group chat, becomes
	// the home channel: the gateway records it in the <agent>-home-channel
	// ConfigMap, the operator projects it into the <agent>-chat-home
	// ConfigMap it mounts as a directory in the agent pod and the gateway
	// pod, both read the file within about a minute with no pod restarted,
	// and HomeChannelUnset clears. Under "manual" HomeChannel alone decides.
	// A set HomeChannel wins under either. No schema default, as on Slack:
	// absent is read as "auto" by the operator.
	// +kubebuilder:validation:Enum=auto;manual
	// +optional
	HomeChannelPolicy string `json:"homeChannelPolicy,omitempty"`

	// Mode controls output verbosity in Google Chat ("default" or "debug").
	// "default": Quiet mode (silences memory reviews, approval cards, and tool progress).
	// "debug": Full verbosity (surfaces tool progress, memory reviews, interim messages, and approval cards).
	// +kubebuilder:validation:Enum=default;debug
	// +kubebuilder:default:="default"
	// +optional
	Mode string `json:"mode,omitempty"`

	// AnswerFeedback draws thumbs-up and thumbs-down buttons under each
	// final answer on Google Chat, and keeps each rating
	// with its question and answer (a thumbs-down with its note) on the install for the feedback bucket's max age (30 days as provisioned).
	// False hides the buttons and keeps nothing.
	// +kubebuilder:default=true
	// +optional
	AnswerFeedback *bool `json:"answerFeedback,omitempty"`
}

// SlackSpec contains the configuration for the Slack integration.
// +kubebuilder:validation:XValidation:rule="!has(self.enabled) || self.enabled == false || (has(self.botTokenSecretRef) && has(self.appTokenSecretRef))",message="botTokenSecretRef and appTokenSecretRef are required when Slack integration is enabled"
type SlackSpec struct {
	// Enabled toggles the Slack integration.
	// +kubebuilder:default=false
	// +optional
	Enabled *bool `json:"enabled,omitempty"`

	// BotTokenSecretRef securely references a Secret containing the SLACK_BOT_TOKEN.
	// +optional
	BotTokenSecretRef *corev1.SecretKeySelector `json:"botTokenSecretRef,omitempty"`

	// AppTokenSecretRef securely references a Secret containing the SLACK_APP_TOKEN.
	// +optional
	AppTokenSecretRef *corev1.SecretKeySelector `json:"appTokenSecretRef,omitempty"`

	// AllowedUsers is a list of allowed member IDs. If not present, all users will be allowed.
	// A plain ID (U...) is a member of the install's own workspace. Under mode next, an entry
	// T...:U... (a workspace ID, a colon, a member ID) admits that member of another workspace
	// in a Slack Connect channel; nothing else admits one, an absent list included. With the
	// list absent, every member of the install's own workspace is allowed, guest accounts
	// included. Under mode next the same list decides who may move the home channel from the
	// app's Home tab, except that a guest account of the workspace never moves it, listed or
	// not.
	// +listType=set
	// +optional
	AllowedUsers []string `json:"allowedUsers,omitempty"`

	// HomeChannel is the default channel ID for scheduled messages.
	// +optional
	HomeChannel string `json:"homeChannel,omitempty"`

	// HomeChannelName is the human-readable name for the home channel.
	// +optional
	HomeChannelName string `json:"homeChannelName,omitempty"`

	// AnswerFeedback draws Slack's thumbs-up and thumbs-down buttons under
	// each final answer, and keeps each rating with
	// its question and answer (a thumbs-down with its note) on the install for the feedback bucket's max age (30 days as provisioned).
	// False hides the buttons and keeps nothing.
	// +kubebuilder:default=true
	// +optional
	AnswerFeedback *bool `json:"answerFeedback,omitempty"`

	// HomeChannelPolicy ("auto", also absent, or "manual") says whether the
	// A2A gateway may learn the Slack home channel when HomeChannel is
	// empty. Under "auto" the channel of the first top-level mention of the
	// agent in a channel becomes the home channel: the gateway
	// records it in the <agent>-home-channel ConfigMap, the operator projects
	// it into the <agent>-chat-home ConfigMap it mounts as a directory in the
	// agent pod and the gateway pod, both read the file within about a minute
	// with no pod restarted, and HomeChannelUnset clears. An agent image
	// older than this operator reads env only and never sees a learned
	// channel: keep the agent image at the operator's release. Under
	// "manual" HomeChannel alone decides. A set HomeChannel wins under
	// either. No schema default: the API server would
	// apply one to every stored object it reads, so absent is read as "auto"
	// by the operator instead.
	// +kubebuilder:validation:Enum=auto;manual
	// +optional
	HomeChannelPolicy string `json:"homeChannelPolicy,omitempty"`
}

// TeamsSpec contains the configuration for the Microsoft Teams integration.
// +kubebuilder:validation:XValidation:rule="!has(self.enabled) || self.enabled == false || (has(self.appIdSecretRef) && has(self.appPasswordSecretRef))",message="appIdSecretRef and appPasswordSecretRef are required when Teams integration is enabled"
// +kubebuilder:validation:XValidation:rule="!has(self.inbound) || !has(self.inbound.enabled) || self.inbound.enabled == false || (has(self.enabled) && self.enabled == true && has(self.tenantId) && self.tenantId.matches('^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'))",message="integration.teams.enabled and a tenantId in GUID form are required when integration.teams.inbound is enabled: the receiver serves one tenant of one enabled integration"
type TeamsSpec struct {
	// Enabled toggles the Microsoft Teams integration.
	// +kubebuilder:default=false
	// +optional
	Enabled *bool `json:"enabled,omitempty"`

	// AppIdSecretRef securely references a Secret containing the Microsoft App (Client) ID.
	// +optional
	AppIdSecretRef *corev1.SecretKeySelector `json:"appIdSecretRef,omitempty"`

	// AppPasswordSecretRef securely references a Secret containing the Microsoft App Client Secret.
	// +optional
	AppPasswordSecretRef *corev1.SecretKeySelector `json:"appPasswordSecretRef,omitempty"`

	// TenantId is the Microsoft Entra ID Tenant ID for single-tenant enterprise lock-down.
	// +optional
	TenantId string `json:"tenantId,omitempty"`

	// AllowedUsers is a list of allowed Entra ID Object IDs (from.aadObjectId) or Bot Framework
	// user IDs (from.id). An inbound Teams activity carries no User Principal Name or email.
	// When empty and allowAllUsers is false (default), interactions are rejected.
	// +listType=set
	// +optional
	AllowedUsers []string `json:"allowedUsers,omitempty"`

	// AllowAllUsers allows any authenticated tenant user to interact with the bot.
	// When false (default), users not listed in allowedUsers are rejected.
	// +kubebuilder:default=false
	// +optional
	AllowAllUsers *bool `json:"allowAllUsers,omitempty"`

	// HomeChannel is the Teams conversation the agent's proactive posts go
	// to: on the Hermes path the default conversation for scheduled
	// messages; with the gateway's Teams backend armed (the operator's
	// A2A_TEAMS_BACKEND switch), the home its chat.notify route posts alerts
	// and reports into. A conversation id: "19:..." (a channel ends in
	// @thread.tacv2, a group or meeting chat in @thread.v2) or "a:..." (a
	// personal chat), never a thread (no ";messageid="); the gateway refuses
	// any other shape, and only a channel home threads its posts.
	// +optional
	HomeChannel string `json:"homeChannel,omitempty"`

	// HomeChannelName is the human-readable name for the home channel.
	// +optional
	HomeChannelName string `json:"homeChannelName,omitempty"`

	// AdaptiveCards toggles rich Adaptive Card rendering for reports and updates.
	// +kubebuilder:default=true
	// +optional
	AdaptiveCards *bool `json:"adaptiveCards,omitempty"`

	// Inbound is the receiver for the messages Microsoft delivers to the
	// bot. Without it the bot can post to Teams, but a message typed in
	// Teams does not reach the agent: Bot Framework delivers each message
	// by an HTTPS request to the address registered for the bot, and no
	// component of the install listens for it. With it the operator runs a
	// small pod that verifies Microsoft's signed token on each request,
	// pins the tenant, and hands the message to the credential broker's
	// Teams queue; the admin exposes its Service with their own Ingress and
	// TLS certificate and registers that address with the bot. It needs
	// tenantId. The receiver holds no credential of the install's own.
	// +optional
	Inbound *TeamsInboundSpec `json:"inbound,omitempty"`
}

// TeamsInboundSpec configures the Teams inbound receiver.
type TeamsInboundSpec struct {
	// Enabled runs the receiver. Off by default: the install then accepts
	// no connection from the internet, as it did before the receiver
	// existed.
	// +kubebuilder:default=false
	// +optional
	Enabled *bool `json:"enabled,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status

// PlatformAgent is the Schema for the platformagents API
type PlatformAgent struct {
	metav1.TypeMeta `json:",inline"`

	// metadata is a standard object metadata
	// +optional
	metav1.ObjectMeta `json:"metadata,omitempty"`

	// spec defines the desired state of PlatformAgent
	// +required
	Spec PlatformAgentSpec `json:"spec"`

	// status defines the observed state of PlatformAgent
	// +optional
	Status AgentStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// PlatformAgentList contains a list of PlatformAgent
type PlatformAgentList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitzero"`
	Items           []PlatformAgent `json:"items"`
}

func init() {
	SchemeBuilder.Register(&PlatformAgent{}, &PlatformAgentList{})
}
