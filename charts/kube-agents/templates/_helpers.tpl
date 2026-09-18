{{/*
Chart name and version, as the helm.sh/chart label value.
*/}}
{{- define "kube-agents.chart" -}}
{{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels applied to every rendered object.

part-of is a constant, not a template value: it is the key the project-wide
footprint query selects on (-l app.kubernetes.io/part-of=kube-agents), so an
object that renders without it is invisible to every doc'd cleanup and audit
command. See the Resource labels reference page for the contract this shares
with the operator, the kustomizations, and the provisioner.
*/}}
{{- define "kube-agents.labels" -}}
helm.sh/chart: {{ include "kube-agents.chart" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/part-of: kube-agents
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}

{{/*
The registry prefix images built from this repo resolve under, or "" to leave
them on their public defaults. Takes the root context.
*/}}
{{- define "kube-agents.imageRegistry" -}}
{{- (.Values.global | default dict).imageRegistry | default "" | trimSuffix "/" -}}
{{- end }}

{{/*
The same for images this project does not build (LiteLLM, fluent-bit). Falls
back to imageRegistry, since a single-prefix mirror is the common case and a
chart that mirrored only its own images would render a half-mirrored install —
the operator handing its managed pods public references after `helm install`
reported success.

This deliberately does NOT match third_party_registry_prefix in
scripts/installer/common.sh, which requires THIRD_PARTY_REGISTRY_PREFIX
explicitly. The asymmetry is about history, not preference: REGISTRY_PREFIX
shipped before this inventory existed and has always meant "the registry
holding the images this project builds", so widening it would redirect working
installs to images their mirror was never given. global.imageRegistry is new
here and carries no such promise, so it can take the safer default.

Takes the root context.
*/}}
{{- define "kube-agents.thirdPartyImageRegistry" -}}
{{- $g := .Values.global | default dict -}}
{{- $g.thirdPartyImageRegistry | default $g.imageRegistry | default "" | trimSuffix "/" -}}
{{- end }}

{{/*
One global.imagePullSecrets entry, as a Secret name.

Both spellings are accepted: the bare name, so a single secret is reachable
with --set global.imagePullSecrets[0]=regcred, and the {name: x} map that
Kubernetes' own PodSpec and most charts' global.imagePullSecrets take. The map
is the shape people write first, and rendering one straight into a value gives
the Secret name "map[name:regcred]" -- which the API server accepts, the
kubelet cannot find, and nothing anywhere reports as wrong. Anything else stops
the render, because the alternative is the same silent failure by another
route.

Takes one entry, not the root context.
*/}}
{{- define "kube-agents.imagePullSecretName" -}}
{{- if kindIs "string" . -}}
{{ required "global.imagePullSecrets: an entry cannot be an empty Secret name" . }}
{{- else if kindIs "map" . -}}
{{ required (printf "global.imagePullSecrets: a map entry needs a non-empty `name`; this one has keys [%s]" (join " " (keys .))) .name }}
{{- else -}}
{{ fail (printf "global.imagePullSecrets entries must be a Secret name or {name: <secret>}, got a %s" (kindOf .)) }}
{{- end -}}
{{- end }}

{{/*
The pod-level imagePullSecrets block, or nothing at all when
global.imagePullSecrets is empty.

Returns the whole block including its key, so callers write
`{{- with (include "kube-agents.imagePullSecrets" .) }}{{ . | nindent N }}{{- end }}`
and an unset value adds no stray blank line. Same contract as
kube-agents.compactFields, and the same reason: every pod spec the chart
renders and the PlatformAgent CR have to agree on this, and a hand-written `if`
at each of them is one place for the next reader to forget.

Takes the root context.
*/}}
{{- define "kube-agents.imagePullSecrets" -}}
{{- with (.Values.global | default dict).imagePullSecrets -}}
imagePullSecrets:
{{- range . }}
  - name: {{ include "kube-agents.imagePullSecretName" . | quote }}
{{- end }}
{{- end }}
{{- end }}

{{/*
The same names, comma-joined for the operator's IMAGE_PULL_SECRETS env var, or
the empty string when there are none -- falsy, so callers can `with` it.

Takes the root context.
*/}}
{{- define "kube-agents.imagePullSecretNames" -}}
{{- $names := list -}}
{{- range (.Values.global | default dict).imagePullSecrets -}}
{{- $names = append $names (include "kube-agents.imagePullSecretName" .) -}}
{{- end -}}
{{- join "," $names -}}
{{- end }}

{{/*
Rewrite an image repository onto a registry prefix, keeping only the trailing
image name: quay.io/jetstack/cert-manager-webhook under "reg.example.com/m"
becomes reg.example.com/m/cert-manager-webhook. That flat layout is what
scripts/mirror_images.sh writes and what the operator assumes when it derives
the credential-proxy reference from the agent one. An empty registry returns
the repository untouched, so a default install renders byte-identically.

The trailing segment is a stand-in for the real rule. mirror_images.sh names
each destination after the images.json entry's .name, and a chart cannot read
images.json at render time, so this reproduces it by convention rather than by
lookup. An image whose inventory name differs from its trailing segment
(hindsight-postgresql is docker.io/pgvector/pgvector) cannot use this helper —
kube-agents.thirdPartyImage below takes the real name explicitly. Check 3c in
hack/check-image-inventory.sh fails the build when a rendered mirror name is
not an inventory name, which is what keeps the shortcut safe.

Takes a dict: {repository, registry}. Returns the repository only — the
PlatformAgent CR carries repository and tag in separate fields, so joining
them here would not suit every caller.
*/}}
{{- define "kube-agents.imageRepository" -}}
{{- $registry := .registry | default "" | trimSuffix "/" -}}
{{- if $registry -}}
{{- printf "%s/%s" $registry (.repository | splitList "/" | last) -}}
{{- else -}}
{{- .repository -}}
{{- end -}}
{{- end }}

{{/*
A complete third-party image reference, reproducing third_party_image() from
scripts/installer/common.sh: mirrored installs pull <prefix>/<name>:<tag>
with any @sha256 digest dropped — `make mirror-images` pushes by tag, and the
copy's digest differs from the upstream one, so keeping it would break every
mirrored pull — while unmirrored installs pull the inventory's full pin,
digest and all.

`name` is the images.json entry name, which is what mirror_images.sh names the
destination; it defaults to the repository's trailing segment, the common case
where the two agree. Passing it explicitly is what lets an image like
hindsight-postgresql (docker.io/pgvector/pgvector) render correctly under a
mirror.

Takes a dict: {repository, tag, name (optional), root (the root context)}.
*/}}
{{- define "kube-agents.thirdPartyImage" -}}
{{- $registry := include "kube-agents.thirdPartyImageRegistry" .root -}}
{{- if $registry -}}
{{- printf "%s/%s:%s" $registry (.name | default (.repository | splitList "/" | last)) (.tag | splitList "@" | first) -}}
{{- else -}}
{{- printf "%s:%s" .repository .tag -}}
{{- end -}}
{{- end }}

{{/*
Whether the Hindsight memory store renders. hindsight.enabled is a tri-state:
true and false are answers, and null (the default) follows the agent's memory
provider — the providers that need the Hindsight API get it, everything else
does not, so an install cannot select hindsight memory and silently receive
no store.
*/}}
{{- define "kube-agents.hindsightEnabled" -}}
{{- $explicit := .Values.hindsight.enabled -}}
{{- if kindIs "invalid" $explicit -}}
{{- $provider := ((.Values.platformAgent.harness.memory | default dict).provider) | default "" -}}
{{- if or (eq $provider "kube_agents_memory") (eq $provider "hindsight") -}}
true
{{- end -}}
{{- else if $explicit -}}
true
{{- end -}}
{{- end }}

{{/*
The OTLP/HTTP collector base URL for the chart's own consumers (the LiteLLM exporter).

Unset means the GKE Managed OpenTelemetry collector, which is what these consumers have
always used. The operator has a richer answer available — it can discover a collector at
reconcile time — but Helm renders once, before any of that, so it keeps the historical
default rather than guessing.
*/}}
{{- define "kube-agents.otlpEndpoint" -}}
{{- .Values.telemetry.otlpEndpoint | default "http://opentelemetry-collector.gke-managed-otel.svc.cluster.local:4318" -}}
{{- end }}

{{/*
The namespace to open OTLP egress to, for the LiteLLM NetworkPolicy.

A namespaceSelector cannot be derived at reconcile time the way the agent's endpoint can:
it has to be right when the policy is applied. So it comes from telemetry.collectorNamespace
when given, and otherwise from the endpoint host, which is a cluster-local Service name in
the case this feature exists for (<svc>.<ns>.svc.cluster.local, or the shortened <svc>.<ns>).

Anything else — an external vendor endpoint, a bare hostname — has no namespace to open,
and what the static policy does then follows the operator's dynamic copy. With
litellm.otel on, this renders "" and the caller emits no OTLP rule: the exporter goes out
over the port-443 rule, which kube-agents.litellmOTLPPortCheck has already made sure is
where the endpoint listens, and a made-up namespaceSelector would open 4317/4318 to a
namespace nothing exports to. With litellm.otel off (the default) there is no LiteLLM
exporter, and the rule keeps the shipping gke-managed-otel default rather than changing
a policy over an egress rule nothing uses.

Only the static litellm-policy render calls this. On the default install the operator
owns the policy and resolves the namespace at reconcile time from the CR.
*/}}
{{- define "kube-agents.otlpCollectorNamespace" -}}
{{- if .Values.telemetry.collectorNamespace -}}
{{- .Values.telemetry.collectorNamespace -}}
{{- else if not .Values.telemetry.otlpEndpoint -}}
gke-managed-otel
{{- else if include "kube-agents.otlpEndpointIsClusterLocal" . -}}
{{- index (splitList "." (include "kube-agents.otlpEndpointHost" .)) 1 -}}
{{- else if not .Values.litellm.otel -}}
gke-managed-otel
{{- end -}}
{{- end }}

{{/*
The host[:port] of telemetry.otlpEndpoint: scheme and path stripped, nothing else.
*/}}
{{- define "kube-agents.otlpEndpointHostPort" -}}
{{- $hostport := .Values.telemetry.otlpEndpoint | trimPrefix "https://" | trimPrefix "http://" -}}
{{- splitList "/" $hostport | first -}}
{{- end }}

{{/*
The host of telemetry.otlpEndpoint, parsed exactly the way the operator's
otlpCollectorNamespace (k8s-operator, platformagent_manifests.go) parses the same value
when it builds the dynamic policy: exact lowercase scheme prefixes, cut at the first "/",
then at the first ":". The two renders have to reach the same verdict about the same
endpoint, so this deliberately inherits the operator's blind spots rather than being
smarter than it — a bracketed IPv6 literal cuts at its first colon and reads as external
on both sides, a query string stays in the last label on both sides. Anything this leaves
unreadable is refused by kube-agents.litellmOTLPPortCheck instead of guessed at.
*/}}
{{- define "kube-agents.otlpEndpointHost" -}}
{{- include "kube-agents.otlpEndpointHostPort" . | splitList ":" | first -}}
{{- end }}

{{/*
"true" when telemetry.otlpEndpoint names an in-cluster Service, "" otherwise — the one
place that heuristic lives, so the namespace helper and the port check cannot drift.

Only two shapes are an in-cluster Service: exactly <svc>.<ns>, or <svc>.<ns>.svc[...].
Anything with a third label that is not "svc" is a public DNS name, and reading its
second label as a namespace would quietly open egress to a namespace named "vendor".
*/}}
{{- define "kube-agents.otlpEndpointIsClusterLocal" -}}
{{- $parts := splitList "." (include "kube-agents.otlpEndpointHost" .) -}}
{{- if or (eq (len $parts) 2) (and (ge (len $parts) 3) (eq (index $parts 2) "svc")) -}}
true
{{- end -}}
{{- end }}

{{/*
Fails the render when the LiteLLM OTLP exporter points at an external host that
litellm-policy cannot reach, whoever renders that policy.

Neither copy of the policy has a rule for an external host except port 443, and with no
collector namespace configured the operator emits no OTLP rule at all for an endpoint
that is not an in-cluster Service. So an external endpoint on any other port (an OTLP
vendor's 4317/4318 ingress, say) renders green and exports nothing, and the only signal
is an operator log line. This catches it at render time. Renders nothing; it is
included unconditionally and acts only when litellm.otel is on — the check is about
LiteLLM's exporter, which does not exist otherwise — and litellm.networkPolicy is on,
since with it off nothing blocks. An explicit telemetry.collectorNamespace is the user
asserting the collector is in-cluster whatever its host looks like (an IP literal, a
bare Service name), and both renders then open 4317/4318 to that namespace, so the
check stands aside for it. It also stands aside for an in-cluster host on a port other
than 4317/4318, on purpose: the URL carries the Service port and the policy sees the
targetPort, so a Service mapping 9999 to 4318 works and a fail there would be wrong.

Two host shapes are refused even on 443, because the 443 rule excepts private ranges
and they are decidable at render time: an IP literal inside a range that render's 443
rule excepts, and a single-label hostname, which resolves through the Pod's search
domain to a Service in its own namespace. Both are in-cluster collectors in disguise,
and telemetry.collectorNamespace is the remedy, as it was before this check existed.
The two rules except different ranges — the static copy the three RFC 1918 blocks and
nothing over IPv6, the operator's those plus CGNAT and link-local space, and over IPv6
unique-local, link-local and multicast — so the check refuses exactly what the rule it
is standing in for does not reach, and nothing else: a loopback literal is excepted by
neither and renders. A DNS name that happens to resolve to private space is not
decidable here, and the docs say so.
*/}}
{{- define "kube-agents.litellmOTLPPortCheck" -}}
{{- /*
  The switches that leave LiteLLM unselected, in every render, so that nothing blocks:
  litellm.networkPolicy=false stops both renders; on the operator-owned render the CR's
  spec.networkPolicy.enabled=false and the enable-litellm-network-policy: "false"
  annotation each delete the managed copy. On that render a collector namespace
  supplied through platformAgent.annotations counts the same as
  telemetry.collectorNamespace, because the operator opens 4317/4318 to it; the static
  render reads only the value, so there the annotation opens nothing and does not count.
*/ -}}
{{- $crAnnotations := .Values.platformAgent.annotations | default dict -}}
{{- $crNetworkPolicy := .Values.platformAgent.networkPolicy | default dict -}}
{{- $operatorOwned := and .Values.platformAgent.enabled .Values.operator.enabled -}}
{{- /* The operator reads both annotations trimmed, and the opt-out case-insensitively. */ -}}
{{- $optOutAnnotation := get $crAnnotations "kubeagents.x-k8s.io/enable-litellm-network-policy" | toString | trim | lower -}}
{{- $crOptOut := and $operatorOwned (or (and (kindIs "bool" $crNetworkPolicy.enabled) (not $crNetworkPolicy.enabled)) (eq $optOutAnnotation "false")) -}}
{{- /*
  Everything below, the namespace validations included, is about LiteLLM's exporter
  being blocked by litellm-policy, so all of it stands aside when there is no exporter
  or no policy that selects LiteLLM: a mistyped namespace with the exporter off blocks
  nothing, and failing the render for it would cite a rule that serves no traffic.
*/ -}}
{{- $checkApplies := and .Values.litellm.otel .Values.litellm.networkPolicy (not $crOptOut) -}}
{{- /*
  Both namespace routes are checked as a namespace name, a lowercase RFC 1123 label.
  That is tighter than the label-value rule the operator applies to the annotation,
  deliberately: a value the operator discards would stand this check aside and open
  nothing, and a value it keeps that no namespace can be called (Obs_NS is a valid label
  value) would open 4317/4318 to a namespace that cannot exist. Either way the exporter
  is blocked, and either way the render is the place to say so.
*/ -}}
{{- $namespaceNamePattern := "^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$" -}}
{{- $namespaceAnnotation := get $crAnnotations "kubeagents.x-k8s.io/otlp-collector-namespace" | toString | trim -}}
{{- if and $checkApplies $operatorOwned $namespaceAnnotation (not (regexMatch $namespaceNamePattern $namespaceAnnotation)) -}}
{{- fail (printf "platformAgent.annotations[\"kubeagents.x-k8s.io/otlp-collector-namespace\"]=%q is not a valid namespace name (a lowercase RFC 1123 label), so the operator would either ignore it or open OTLP egress to a namespace that cannot exist, and the LiteLLM OTLP exporter (litellm.otel=true) would be blocked. Give the collector's namespace name." $namespaceAnnotation) -}}
{{- end -}}
{{- /*
  The value route gets the same validation: an invalid namespace would stand this check
  aside, be stamped on the CR, and select nothing in either render.
*/ -}}
{{- $collectorNamespaceValue := .Values.telemetry.collectorNamespace | toString | trim -}}
{{- if and $checkApplies $collectorNamespaceValue (not (regexMatch $namespaceNamePattern $collectorNamespaceValue)) -}}
{{- fail (printf "telemetry.collectorNamespace=%q is not a valid namespace name (a lowercase RFC 1123 label), so the NetworkPolicy would select nothing and the LiteLLM OTLP exporter (litellm.otel=true) would be blocked. Give the collector's namespace name." $collectorNamespaceValue) -}}
{{- end -}}
{{- $collectorNamespace := or $collectorNamespaceValue (and $operatorOwned $namespaceAnnotation) -}}
{{- if and $checkApplies .Values.telemetry.otlpEndpoint (not $collectorNamespace) (not (include "kube-agents.otlpEndpointIsClusterLocal" .)) -}}
{{- $endpoint := .Values.telemetry.otlpEndpoint -}}
{{- /*
  A scheme this parser does not strip (grpc://, or HTTP:// in capitals) would leave a
  hostport with no port to read and pass as an implicit 443. LiteLLM's exporter speaks
  OTLP/HTTP over http:// or https://, so anything else is refused here rather than
  waved through.
*/ -}}
{{- if and (contains "://" $endpoint) (not (or (hasPrefix "http://" $endpoint) (hasPrefix "https://" $endpoint))) -}}
{{- fail (printf "telemetry.otlpEndpoint %q must start with http:// or https:// (lowercase): the LiteLLM OTLP exporter (litellm.otel=true) speaks OTLP/HTTP, and the NetworkPolicy render cannot read the port off any other scheme." $endpoint) -}}
{{- end -}}
{{- $hostport := include "kube-agents.otlpEndpointHostPort" . -}}
{{- /*
  The port is whatever follows the first ":" once a bracketed IPv6 literal is set aside,
  and it has to be all digits. Userinfo, a query string, or a fragment in the authority
  would leave the port unreadable (and the operator would read the host differently),
  so those are refused too rather than passed as an implicit 443.
*/ -}}
{{- $afterHost := regexReplaceAll "^\\[[^\\]]*\\]" $hostport "" -}}
{{- $port := "" -}}
{{- if contains ":" $afterHost -}}
{{- $port = splitList ":" $afterHost | rest | join ":" -}}
{{- end -}}
{{- if or (regexMatch "[?#@]" $hostport) (and (contains ":" $afterHost) (not (regexMatch "^[0-9]+$" $port))) -}}
{{- fail (printf "telemetry.otlpEndpoint %q: the NetworkPolicy render cannot read the port off it. Give it as http(s)://host[:port][/path], with no userinfo, query, or fragment." $endpoint) -}}
{{- end -}}
{{- if not $port -}}
{{- $port = ternary "80" "443" (hasPrefix "http://" $endpoint) -}}
{{- end -}}
{{- $host := include "kube-agents.otlpEndpointHost" . -}}
{{- /*
  The prefixes each render's 443 rule excepts, and only those: RFC 1918 in the static
  copy (litellm.yaml); RFC 1918, CGNAT and link-local in the operator's
  (platformagent_manifests.go). Change one alongside its rule.
*/ -}}
{{- $rfc1918Prefixes := "10\\.|192\\.168\\.|172\\.(1[6-9]|2[0-9]|3[01])\\." -}}
{{- $operatorOnlyPrefixes := "|169\\.254\\.|100\\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\\." -}}
{{- $exceptedPrefixes := ternary (printf "%s%s" $rfc1918Prefixes $operatorOnlyPrefixes) $rfc1918Prefixes $operatorOwned -}}
{{- $privateIPv4 := regexMatch (printf "^(%s)[0-9]+\\.[0-9]+(\\.[0-9]+)?$" $exceptedPrefixes) $host -}}
{{- /*
  The operator's ::/0 peer excepts fc00::/7, fe80::/10 and ff00::/8, all decidable off
  a bracketed literal's first hextet. The static copy has no IPv6 peer at all, and
  refuses every IPv6 literal further down.
*/ -}}
{{- $privateIPv6 := and $operatorOwned (regexMatch "^\\[(?i:f[cd]|fe[89ab]|ff)" $hostport) -}}
{{- /* A bracketed IPv6 literal cuts to "[…" with no dot; it is not a single-label host. */ -}}
{{- $singleLabel := and (not (contains "." $host)) (not (hasPrefix "[" $hostport)) -}}
{{- if or $privateIPv4 $privateIPv6 $singleLabel -}}
{{- $shape := "a single-label host" -}}
{{- if $privateIPv4 -}}{{- $shape = "a private IPv4 address" -}}{{- else if $privateIPv6 -}}{{- $shape = "a private IPv6 address" -}}{{- end -}}
{{- fail (printf "telemetry.otlpEndpoint %q names %s, which litellm-policy's port-443 rule does not reach (it excepts private ranges), so the LiteLLM OTLP exporter (litellm.otel=true) would be blocked. If this is an in-cluster collector, set telemetry.collectorNamespace to its namespace; otherwise give the collector's public host." $endpoint $shape) -}}
{{- end -}}
{{- /* The static copy's 443 rule has an IPv4 peer only; the operator's adds ::/0. */ -}}
{{- if and (hasPrefix "[" $hostport) (not $operatorOwned) -}}
{{- fail (printf "telemetry.otlpEndpoint %q is an IPv6 literal, and the static litellm-policy's port-443 rule reaches IPv4 destinations only, so the LiteLLM OTLP exporter (litellm.otel=true) would be blocked. Give the collector's hostname, or set litellm.networkPolicy=false if the policy is managed elsewhere." $endpoint) -}}
{{- end -}}
{{- if ne $port "443" -}}
{{- fail (printf "telemetry.otlpEndpoint %q names an external host on port %s, but litellm-policy permits egress to external hosts on port 443 only, so the LiteLLM OTLP exporter (litellm.otel=true) would be blocked. Use a port-443 endpoint, set telemetry.collectorNamespace if the collector is in fact in-cluster, or set litellm.networkPolicy=false if the policy is managed elsewhere." $endpoint $port) -}}
{{- end -}}
{{- end -}}
{{- end }}

{{/*
Renders a dict of optional CR fields as YAML, dropping the ones left unset.

"Unset" is null or the empty string; `false` and `0` are values and survive,
which is the whole reason this exists — `with` and plain truthiness drop both,
and a boolean knob nobody can set to false is not a knob.

Returns the empty string when every field is unset, so a caller can write
`{{- with (include ...) }}` and have the PARENT block disappear too. That
coupling is the point: guarding a parent by hand means enumerating its children
in an `or`, and the failure mode when a later field is added to one list and not
the other is silence — the template still emits valid YAML, just without the
field somebody set.

Takes a dict of field name to value.
*/}}
{{- define "kube-agents.compactFields" -}}
{{- $out := dict -}}
{{- range $key, $value := . -}}
{{- if not (or (kindIs "invalid" $value) (and (kindIs "string" $value) (eq $value ""))) -}}
{{- $_ := set $out $key $value -}}
{{- end -}}
{{- end -}}
{{- if $out -}}
{{- toYaml $out -}}
{{- end -}}
{{- end }}

{{/*
The LiteLLM gateway config, mirroring
k8s-operator/config/integrations/litellm/base/config.yaml.

Defined once and consumed twice — as the ConfigMap body and as the input to the
Deployment's checksum annotation — because those two must not be able to
disagree. Hashing the inputs (provider, model, callbacks) instead of the output
was the earlier shape and it missed any edit to this template itself: the
ConfigMap changed, the checksum did not, the Deployment did not roll. The
gateway mounts this with subPath, and a subPath ConfigMap mount never receives
in-place updates, so the running pod would have kept the old file indefinitely.

Takes a dict of provider, model, callbacks.
*/}}
{{- define "kube-agents.litellmConfig" -}}
model_list:
  - model_name: model-default
    litellm_params:
      model: {{ printf "%s/%s" .provider .model }}
  - model_name: hermes-agent
    litellm_params:
      model: {{ printf "%s/%s" .provider .model }}
  - model_name: {{ .model }}
    litellm_params:
      model: {{ printf "%s/%s" .provider .model }}
litellm_settings:
  callbacks: {{ .callbacks }}
{{- /*
  Prompt caching. Kept identical to the kustomize base
  (k8s-operator/config/integrations/litellm/base/config.yaml) — see that file
  for why the breakpoints live here rather than in the agent's own config, and
  why non-Anthropic backends are unaffected.
*/}}
router_settings:
  default_litellm_params:
    cache_control_injection_points:
      - location: message
        role: system
        control:
          type: ephemeral
          ttl: 1h
      - location: message
        index: -3
      - location: message
        index: -1
{{- end }}

{{/*
Selector labels for the operator Deployment. Kept minimal and stable:
selectors are immutable once the Deployment exists.
*/}}
{{- define "kube-agents.operatorSelectorLabels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}-operator
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
topologySpreadConstraints for a multi-replica workload this chart owns.

Renders nothing below two replicas, so the operator gets the field only if
someone raises operator.replicaCount. A constraint over a single pod is
satisfied by construction, and printing it would leave a reader working out that
it means nothing. Hindsight has no call site at all for the same reason taken
further: both its workloads carry a literal `replicas: 1` with no value behind
it, so a call there could never render and would read as coverage it does not
have.

The chart shipped PDBs and a default replicaCount of 2 for litellm and
github-token-minter without this, and the Workload Reliability Audit in this
repository found both on 2026-09-06: "replicas=2, no topologySpreadConstraints
or podAntiAffinity". The PDB does not cover the gap it names. maxUnavailable: 1
stalls a *drain* that would take both replicas, but a node that fails takes
whatever is on it, and nothing was keeping the two pods apart.

ScheduleAnyway, not DoNotSchedule — obtainability_audit_sop.md 3.8 calls that
mandatory, and the reason is the shape of the clusters this chart installs into.
A pool that cannot satisfy maxSkew: 1 leaves the second replica Pending
indefinitely, which is worse than the co-location this exists to avoid.

kubernetes.io/hostname and not the zone key, for the same section's reason: the
loss this guards against is a node going away under a drain or a repair, and a
zonal cluster has one zone to spread across.

The selector is the workload's own and is passed in rather than derived, as
already-rendered YAML rather than a dict so the operator can hand over
`kube-agents.operatorSelectorLabels` verbatim instead of a second copy of it. A
labelSelector that does not match the pods the constraint is attached to counts
some other population and skews against it; these selectors are also immutable
once the Deployment exists, so the caller is the only thing that knows the right
answer. Keep each call in step with its Deployment's spec.selector, the way
pdb.yaml's selectors already have to be.

matchLabelKeys scopes the skew to one ReplicaSet. Without it the constraint
counts old and new pods together during a rollout, and with maxSurge: 1,
maxUnavailable: 1 and two nodes holding one replica each, the surge pod lands
beside an old one, the controller prefers to delete the old pod that shares a
node, and the second new pod then sees a tie and can land on the same node —
both live replicas on one node until the next rollout, which the constraint
exists to prevent. Every image pin, config checksum or resource change is a
rollout, so this is ordinary use. pod-template-hash is the label the
Deployment controller stamps per revision; the field is on by default from
Kubernetes 1.27, inside the chart's 1.29 floor.
*/}}
{{- define "kube-agents.topologySpreadConstraints" -}}
{{- if and .enabled (gt (int .replicas) 1) -}}
topologySpreadConstraints:
  - maxSkew: 1
    topologyKey: kubernetes.io/hostname
    whenUnsatisfiable: ScheduleAnyway
    matchLabelKeys:
      - pod-template-hash
    labelSelector:
      matchLabels:
        {{- .selectorLabels | nindent 8 }}
{{- end }}
{{- end }}

{{/*
Admission-webhook object names, mirroring k8s-operator/config/webhook and
config/certmanager.

Defined here rather than inlined because four templates have to agree on them:
the Service the webhook configurations' clientConfig points at, the Certificate
whose dnsNames must match that Service, the Secret the Deployment mounts, and
the inject-ca-from annotation. A name that disagrees across any two of those
renders valid YAML and fails at admission time, which is the wrong place to find
out.

The webhook configurations are cluster-scoped, so they carry the namespace
component the chart already uses for the operator ClusterRole — two releases in
different namespaces would otherwise fight over one object, and the loser's
clientConfig would point every PlatformAgent admission in the cluster at the
wrong Service.
*/}}
{{- define "kube-agents.webhookServiceName" -}}
{{ .Release.Name }}-webhook-service
{{- end }}

{{- define "kube-agents.webhookCertificateName" -}}
{{ .Release.Name }}-serving-cert
{{- end }}

{{- define "kube-agents.webhookCertSecretName" -}}
{{ .Release.Name }}-webhook-certs
{{- end }}

{{- define "kube-agents.webhookConfigurationPrefix" -}}
{{ .Release.Name }}-{{ .Release.Namespace }}
{{- end }}

{{/*
Validates and resolves a Deployment's rollingUpdate fenceposts, returning a
YAML map with `maxSurge` and `maxUnavailable`. Callers parse the output with
`| fromYaml`.

Both fenceposts at zero leaves the Deployment no way to make progress and
the API server rejects it ("may not be 0 when maxSurge is 0"), so fail
the render rather than the apply. See values.yaml's rollingUpdate blocks.

A fencepost with no usable value takes the given default (defaultSurge,
defaultUnavailable) and renders explicitly. "No usable value" has to mean
the empty string as well as nil/invalid: `--set <scope>.maxUnavailable=` and
a values file's `maxUnavailable: ""` both reach here as an empty string,
which is a perfectly good `kind` and so survives a nil test. Rendering
either through would emit `maxUnavailable:` with nothing after it, and
Kubernetes then applies its own 25% default.

Both fields are IntOrString, which is what makes the zero test awkward:
`int` is cast.ToInt and reads "25%" as 0, so it would refuse a pair of
perfectly good percentages, while a list of literal spellings misses
"0.0". Compare numerically with the percent sign stripped — "0%"
resolves to 0 on the cluster, so it is the same misconfiguration spelled
differently. `float64` is cast.ToFloat64, which reports anything it
cannot parse as 0, so the numeric test is gated on both values actually
being numeric: without that, `maxSurge: abc` is refused as a zero and
the message names the wrong problem. Non-numeric input is left to the
API server, which is where it was rejected before this guard existed.

Takes a dict: {rollingUpdate, defaultSurge, defaultUnavailable, scope}.
*/}}
{{- define "kube-agents.rollingUpdateFenceposts" -}}
{{- $ru := .rollingUpdate | default dict -}}
{{- $surge := $ru.maxSurge -}}
{{- $unavail := $ru.maxUnavailable -}}
{{- $defaultSurge := .defaultSurge -}}
{{- if kindIs "invalid" $defaultSurge }}{{- $defaultSurge = 1 }}{{- end -}}
{{- $defaultUnavail := .defaultUnavailable -}}
{{- if kindIs "invalid" $defaultUnavail }}{{- $defaultUnavail = 0 }}{{- end -}}
{{- if or (kindIs "invalid" $surge) (eq (toString $surge) "") }}{{- $surge = $defaultSurge }}{{- end -}}
{{- if or (kindIs "invalid" $unavail) (eq (toString $unavail) "") }}{{- $unavail = $defaultUnavail }}{{- end -}}
{{- $surgeNum := trimSuffix "%" (trim (toString $surge)) -}}
{{- $unavailNum := trimSuffix "%" (trim (toString $unavail)) -}}
{{- $numeric := "^[0-9]+(\\.[0-9]+)?$" -}}
{{- if and (regexMatch $numeric $surgeNum) (regexMatch $numeric $unavailNum) -}}
{{- if and (eq (float64 $surgeNum) 0.0) (eq (float64 $unavailNum) 0.0) -}}
{{- fail (printf "%s: maxSurge (%v) and maxUnavailable (%v) may not both be zero — the Deployment would have no way to make progress, and the API server rejects it." .scope $surge $unavail) -}}
{{- end -}}
{{- end -}}
maxSurge: {{ $surge }}
maxUnavailable: {{ $unavail }}
{{- end }}

{{/*
Resource parsing helpers for quota preflight (#749).
Converts Kubernetes quantities to canonical integer units:
- CPU: millicores (e.g. "500m" -> 500, "1" -> 1000, "1.5" -> 1500)
- Memory / Storage: bytes (e.g. "128Mi" -> 134217728, "2Gi" -> 2147483648)

Both fail the render on a quantity they cannot parse rather than returning a number.
An earlier version fell through to `int64`, which yields 0 for anything it does not
understand: a `1Pi` quota then read as `hard 0` and the release was refused with a
message describing a cluster that does not exist. A quantity this cannot read is a bug
in this helper, and saying so is the only honest outcome.

Every conversion to an integer goes through kube-agents.clampInt64 rather than `int64`,
because the millicore and byte forms of a large quantity overflow where the quantity
itself does not. `1E` CPU is 10^21 millicores and `8Ei` is 2^63 bytes, both past
math.MaxInt64, and Go's float-to-int conversion wraps them to -9223372036854775808:
`hard` then read as negative, fell short of every requirement it dwarfs, and the render
was refused with a patch asking for a negative quota. Saturating keeps the comparison
honest — a quota that large cannot constrain this release either way.
*/}}
{{- /* Float to int64, saturating rather than wrapping. 2^63 is the first float64 above
       math.MaxInt64, which is not itself representable as a float64 — comparing against
       a rounded MaxInt64 would let the wrapping value through. */ -}}
{{- define "kube-agents.clampInt64" -}}
{{- $v := float64 . -}}
{{- if ge $v 9223372036854775808.0 -}}
9223372036854775807
{{- else -}}
{{- $v | int64 -}}
{{- end -}}
{{- end }}

{{- define "kube-agents.parseCpuMillis" -}}
{{- $raw := trim (toString .) -}}
{{- $numeric := "^[0-9]+(\\.[0-9]+)?([eE][-+]?[0-9]+)?$" -}}
{{- $decimalCores := dict "k" 1000.0 "M" 1000000.0 "G" 1000000000.0 "T" 1000000000000.0 "P" 1000000000000000.0 "E" 1000000000000000000.0 -}}
{{- if or (eq $raw "") (eq $raw "<nil>") -}}
0
{{- else if hasSuffix "m" $raw -}}
{{- $n := trimSuffix "m" $raw -}}
{{- if not (regexMatch $numeric $n) -}}
{{- fail (printf "quota preflight: cannot parse CPU quantity %q — set quotaPreflight.enabled=false to bypass, and please report it." $raw) -}}
{{- end -}}
{{- include "kube-agents.clampInt64" (float64 $n) -}}
{{- else -}}
{{- $out := "" -}}
{{- range $unit, $mult := $decimalCores -}}
{{- if and (eq $out "") (hasSuffix $unit $raw) -}}
{{- $n := trimSuffix $unit $raw -}}
{{- if not (regexMatch $numeric $n) -}}
{{- fail (printf "quota preflight: cannot parse CPU quantity %q — set quotaPreflight.enabled=false to bypass, and please report it." $raw) -}}
{{- end -}}
{{- $out = include "kube-agents.clampInt64" (mulf (mulf (float64 $n) $mult) 1000.0) -}}
{{- end -}}
{{- end -}}
{{- if eq $out "" -}}
{{- if not (regexMatch $numeric $raw) -}}
{{- fail (printf "quota preflight: cannot parse CPU quantity %q — set quotaPreflight.enabled=false to bypass, and please report it." $raw) -}}
{{- end -}}
{{- $out = include "kube-agents.clampInt64" (mulf (float64 $raw) 1000.0) -}}
{{- end -}}
{{- $out -}}
{{- end -}}
{{- end }}

{{- define "kube-agents.parseBytes" -}}
{{- $raw := trim (toString .) -}}
{{- $numeric := "^[0-9]+(\\.[0-9]+)?([eE][-+]?[0-9]+)?$" -}}
{{- $binary := dict "Ki" 1024.0 "Mi" 1048576.0 "Gi" 1073741824.0 "Ti" 1099511627776.0 "Pi" 1125899906842624.0 "Ei" 1152921504606846976.0 -}}
{{- $decimal := dict "k" 1000.0 "M" 1000000.0 "G" 1000000000.0 "T" 1000000000000.0 "P" 1000000000000000.0 "E" 1000000000000000000.0 -}}
{{- if or (eq $raw "") (eq $raw "<nil>") -}}
0
{{- else if hasSuffix "m" $raw -}}
{{- $n := trimSuffix "m" $raw -}}
{{- if not (regexMatch $numeric $n) -}}
{{- fail (printf "quota preflight: cannot parse quantity %q (memory, storage or count) — set quotaPreflight.enabled=false to bypass, and please report it." $raw) -}}
{{- end -}}
{{- include "kube-agents.clampInt64" (ceil (divf (float64 $n) 1000.0)) -}}
{{- else -}}
{{- $out := "" -}}
{{- range $unit, $mult := $binary -}}
{{- if and (eq $out "") (hasSuffix $unit $raw) -}}
{{- $n := trimSuffix $unit $raw -}}
{{- if not (regexMatch $numeric $n) -}}
{{- fail (printf "quota preflight: cannot parse quantity %q (memory, storage or count) — set quotaPreflight.enabled=false to bypass, and please report it." $raw) -}}
{{- end -}}
{{- $out = include "kube-agents.clampInt64" (mulf (float64 $n) $mult) -}}
{{- end -}}
{{- end -}}
{{- if eq $out "" -}}
{{- range $unit, $mult := $decimal -}}
{{- if and (eq $out "") (hasSuffix $unit $raw) -}}
{{- $n := trimSuffix $unit $raw -}}
{{- if not (regexMatch $numeric $n) -}}
{{- fail (printf "quota preflight: cannot parse quantity %q (memory, storage or count) — set quotaPreflight.enabled=false to bypass, and please report it." $raw) -}}
{{- end -}}
{{- $out = include "kube-agents.clampInt64" (mulf (float64 $n) $mult) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if eq $out "" -}}
{{- if not (regexMatch $numeric $raw) -}}
{{- fail (printf "quota preflight: cannot parse quantity %q (memory, storage or count) — set quotaPreflight.enabled=false to bypass, and please report it." $raw) -}}
{{- end -}}
{{- $out = include "kube-agents.clampInt64" (float64 $raw) -}}
{{- end -}}
{{- $out -}}
{{- end -}}
{{- end }}

{{/*
Count quotas (`pods`, `persistentvolumeclaims`) go through the same parser.

They are not plain integers on the wire. The API server round-trips every quota value
through resource.Quantity and writes back the canonical form, so a namespace created
with `pods: 1000` is read back as `pods: "1k"`. Sprig's `int64` is `cast.ToInt64`, which
answers 0 for a string it cannot parse rather than failing — so an earlier version read
that quota as `hard 0`, refused the release for a shortfall that did not exist, and
printed a patch lowering the namespace to 7 pods for whoever followed the instructions.
parseBytes already reads the decimal-SI suffixes this needs, and fails loudly on the rest.
*/}}
{{- define "kube-agents.parseCount" -}}
{{- include "kube-agents.parseBytes" . -}}
{{- end }}

{{/*
Format helpers for friendly error display and patch generation:
- CPU: converts millicores to e.g. "10000m" (or "10" if exact integer cores)
- Memory / Storage: converts bytes to Mi / Gi
scripts/generate_chart_footprint.py has the same two functions, so the numbers the
preflight prints and the numbers in footprint.yaml are written the same way.
*/}}
{{- define "kube-agents.formatCpu" -}}
{{- $m := int64 . -}}
{{- if and (gt $m 0) (eq (mod $m 1000) 0) -}}
{{- printf "%d" (div $m 1000) -}}
{{- else -}}
{{- printf "%dm" $m -}}
{{- end -}}
{{- end }}

{{- define "kube-agents.formatBytes" -}}
{{- $b := int64 . -}}
{{- if and (gt $b 0) (eq (mod $b 1073741824) 0) -}}
{{- printf "%dGi" (div $b 1073741824) -}}
{{- else if and (gt $b 0) (eq (mod $b 1048576) 0) -}}
{{- printf "%dMi" (div $b 1048576) -}}
{{- else -}}
{{- printf "%d" $b -}}
{{- end -}}
{{- end }}

{{/*
Two more byte formatters, for the quota diagnosis rather than for footprint.yaml.

formatBytes above only names a unit when the value divides exactly, and falls back to a
bare byte count otherwise. That is right for footprint.yaml, whose numbers are always
Mi-aligned, and wrong in the failure message: a namespace whose quota is written in
decimal SI (`requests.memory: 10G`) turns every figure into an eleven-digit byte count,
which is the opposite of the legible diagnosis this check exists to give.

Rounding in a patch value is not free, so the direction is chosen per use:

- formatBytesCeil rounds UP to whole Mi, and sizes the remediation patch. Rounding down
  would print a patch that is short of what the release needs, which is worse than an
  ugly number: the operator runs it and the install still fails.
- formatBytesApprox rounds toward zero and marks the result `~`, and is display-only.
  Nothing is computed from it, and the `~` keeps it from being read as exact.
*/}}
{{- define "kube-agents.formatBytesCeil" -}}
{{- $b := int64 . -}}
{{- if and (gt $b 0) (eq (mod $b 1048576) 0) -}}
{{- include "kube-agents.formatBytes" $b -}}
{{- else if le $b 0 -}}
{{- printf "%d" $b -}}
{{- else -}}
{{- printf "%dMi" (div (add $b 1048575) 1048576) -}}
{{- end -}}
{{- end }}

{{- define "kube-agents.formatBytesApprox" -}}
{{- $b := int64 . -}}
{{- if and (gt $b 0) (eq (mod $b 1048576) 0) -}}
{{- include "kube-agents.formatBytes" $b -}}
{{- else if eq $b 0 -}}
0
{{- else -}}
{{- printf "~%dMi" (div $b 1048576) -}}
{{- end -}}
{{- end }}

{{/*
Reads one resources block — requests and limits, CPU, memory and ephemeral-storage — from a
values subtree, and returns the parsed quantities as JSON.

Every field is optional at every level. values.schema.json declares each `resources` as a
bare object with no `required` keyword, so `--set litellm.resources.limits=null` — the
documented Helm way to drop a key, and the obvious edit for someone who does not want
limits charged against a quota — is valid input. Reaching through it with
`.Values.litellm.resources.limits.cpu` aborted the whole render with `nil pointer
evaluating interface {}.cpu`, and only in a namespace that has a ResourceQuota, which is
the one population this check exists for.

When requests are omitted but limits are specified, Kubernetes defaults the request to
match the limit at admission time, which is reflected here. When a quantity is omitted
from both requests and limits, it contributes zero to the sum (though note that if a
ResourceQuota constrains that resource, Kubernetes quota admission requires containers
to declare it unless defaulted by a LimitRange).

"Omitted" is decided by kube-agents.declaredQuantity rather than by Sprig's `default`,
for the same reason kube-agents.replicaCount exists: `default` calls 0 empty, and each
`resources` block is an open object in values.schema.json, so `requests: {cpu: 0}` is
valid input that arrives as a numeric zero. Chained through `default` it read as absent
and was charged the limit instead — a workload asking for nothing was summed as the
largest thing it could ever use.
*/}}
{{- define "kube-agents.declaredQuantity" -}}
{{- $v := .value -}}
{{- if or (kindIs "invalid" $v) (eq (toString $v) "") -}}
{{- toString .fallback -}}
{{- else -}}
{{- toString $v -}}
{{- end -}}
{{- end }}

{{- define "kube-agents.workloadResources" -}}
{{- $res := (. | default dict).resources | default dict -}}
{{- $req := (index $res "requests") | default dict -}}
{{- $lim := (index $res "limits") | default dict -}}
{{- $cpuLim := include "kube-agents.declaredQuantity" (dict "value" (index $lim "cpu") "fallback" "0") -}}
{{- $memLim := include "kube-agents.declaredQuantity" (dict "value" (index $lim "memory") "fallback" "0") -}}
{{- $ephLim := include "kube-agents.declaredQuantity" (dict "value" (index $lim "ephemeral-storage") "fallback" "0") -}}
{{- $cpuReq := include "kube-agents.declaredQuantity" (dict "value" (index $req "cpu") "fallback" $cpuLim) -}}
{{- $memReq := include "kube-agents.declaredQuantity" (dict "value" (index $req "memory") "fallback" $memLim) -}}
{{- $ephReq := include "kube-agents.declaredQuantity" (dict "value" (index $req "ephemeral-storage") "fallback" $ephLim) -}}
{{- dict
      "cpuRequest" (include "kube-agents.parseCpuMillis" $cpuReq | int64)
      "cpuLimit" (include "kube-agents.parseCpuMillis" $cpuLim | int64)
      "memoryRequest" (include "kube-agents.parseBytes" $memReq | int64)
      "memoryLimit" (include "kube-agents.parseBytes" $memLim | int64)
      "ephemeralRequest" (include "kube-agents.parseBytes" $ephReq | int64)
      "ephemeralLimit" (include "kube-agents.parseBytes" $ephLim | int64)
   | toJson -}}
{{- end }}

{{/*
Replica count, where an explicit 0 means 0.

`replicas | default 1` reads a falsy 0 as absent and charges a full replica for a workload
scaled to zero. AvailabilitySpec.Replicas is +kubebuilder:validation:Minimum=0
(k8s-operator/api/v1alpha1/common_types.go), so 0 is a value a user can legitimately set,
and the chart's own replicaCount keys take one too. Only an absent value defaults to 1.
*/}}
{{- define "kube-agents.replicaCount" -}}
{{- if kindIs "invalid" . -}}
1
{{- else -}}
{{- int64 . -}}
{{- end -}}
{{- end }}

{{/*
Preflight validation against namespace ResourceQuotas (#749).

Split into three templates so the parts that need no cluster can be tested without one:

- kube-agents.quotaRequirements — totals what the release needs, as JSON. Pure function of
  the values and footprint.yaml.
- kube-agents.quotaCheckItems — compares those totals against a list of ResourceQuota
  objects and fails the render on a shortfall. Takes the list as an argument.
- kube-agents.quotaPreflight — the entry point: looks the quotas up, then calls the two above.

Only the last one touches the cluster, so tests/test_quota_preflight.py can drive the other
two with synthetic quotas and assert the arithmetic and the pass/fail decision offline.

Fails the render if:
- hard < required (the quota cannot fit the release even if empty)
- OR (hard - used) < required AND .Release.IsInstall (on a fresh install, remaining headroom
  is insufficient). Install-only on purpose: on upgrade the release's own pods are already
  counted in `used`, so subtracting them again would refuse every upgrade of a release that
  exactly fits its quota. The cost of that exemption is that `used` on upgrade also holds
  any neighbouring workload's usage, which this cannot tell apart from the release's own —
  so in a namespace shared with other workloads an upgrade is checked against `hard` alone.

Quota keys understood: CPU, memory and ephemeral-storage (requests and limits), pods,
persistentvolumeclaims and requests.storage. Keys outside that set (services, secrets, other
count/<resource>) are not modelled, and are skipped rather than guessed at.

Inert when lookup returns empty — `helm template` without a cluster, or a namespace with no
ResourceQuota at all.

It is NOT inert when the installing identity cannot read ResourceQuotas. Helm's `lookup`
swallows a NotFound and returns nothing; every other API error, a 403 on
`list resourcequotas` among them, comes back as a template error and aborts the render. So
the check needs `get`/`list` on `resourcequotas` in the release namespace, and an identity
without it installs with `--set quotaPreflight.enabled=false`. Nothing here can soften that:
a Go template cannot catch the error `lookup` raises.
*/}}
{{- define "kube-agents.quotaRequirements" -}}
{{- $footprint := .Files.Get "files/footprint.yaml" | fromYaml -}}
{{- /* The footprint is the only source for the operator-rendered pods, which are most of
       the release. If it is missing or unparseable every one of them silently counts as
       zero and the preflight waves through a quota that cannot fit the release — the exact
       failure it exists to prevent, now with a green light in front of it. */ -}}
{{- if not (index $footprint "operatorRendered") -}}
  {{- fail "quota preflight: footprint.yaml is missing or unreadable in the chart, so the operator-rendered pods cannot be sized. Reinstall from an intact chart, or set quotaPreflight.enabled=false to skip the check." -}}
{{- end -}}
{{- $op := (index $footprint "operatorRendered") | default dict -}}

{{- $reqPods := 0 -}}
{{- $reqCpu := 0 -}}
{{- $limCpu := 0 -}}
{{- $reqMem := 0 -}}
{{- $limMem := 0 -}}
{{- $reqEph := 0 -}}
{{- $limEph := 0 -}}
{{- $reqPvc := 0 -}}
{{- $reqStorage := 0 -}}

{{- /* Largest single pod among the workloads that roll with a surge Pod. Used only to size
       the remediation patch, never the pass/fail threshold: a quota raised to exactly
       used+required fits the release at rest and then stalls its first rollout, which is the
       failure values.yaml warns about under hindsight.api.rollingUpdate. Rollouts are
       per-workload, so room for one surge Pod at a time is enough. */ -}}
{{- $surgeCpuReq := 0 -}}
{{- $surgeCpuLim := 0 -}}
{{- $surgeMemReq := 0 -}}
{{- $surgeMemLim := 0 -}}
{{- $surgeEphReq := 0 -}}
{{- $surgeEphLim := 0 -}}

{{- /* The chart's own workloads, as (resources subtree, pod count, rolls-with-a-surge-Pod).
       One list and one loop rather than a block each: the four blocks this replaced were
       identical but for the values path, which is how the dashboard came to be read from
       the wrong key and how a new workload comes to be missed. `hindsight.postgresql` is a
       StatefulSet, so it contributes no surge Pod. The pre-delete cleanup hook is a batch
       Job that runs at uninstall while the release still stands, so it needs headroom but
       no surge. */ -}}
{{- $chartWorkloads := list -}}
{{- if .Values.operator.enabled -}}
  {{- $chartWorkloads = append $chartWorkloads (dict "values" .Values.operator "pods" (include "kube-agents.replicaCount" .Values.operator.replicaCount | int64) "surges" true) -}}
{{- end -}}
{{- if .Values.litellm.enabled -}}
  {{- $chartWorkloads = append $chartWorkloads (dict "values" .Values.litellm "pods" (include "kube-agents.replicaCount" .Values.litellm.replicaCount | int64) "surges" true) -}}
{{- end -}}
{{- if include "kube-agents.hindsightEnabled" . -}}
  {{- $chartWorkloads = append $chartWorkloads (dict "values" .Values.hindsight.api "pods" 1 "surges" true) -}}
  {{- $chartWorkloads = append $chartWorkloads (dict "values" .Values.hindsight.postgresql "pods" 1 "surges" false) -}}
{{- end -}}
{{- if .Values.githubMinter.enabled -}}
  {{- $chartWorkloads = append $chartWorkloads (dict "values" .Values.githubMinter "pods" (include "kube-agents.replicaCount" .Values.githubMinter.replicaCount | int64) "surges" true) -}}
{{- end -}}
{{- if and .Values.platformAgent.enabled .Values.platformAgent.cleanupHook.enabled -}}
  {{- /* Pre-delete hook Job in templates/platform-agent-cr-cleanup.yaml: runs at helm uninstall
         while every release pod still exists, so quota admission needs headroom for it. */ -}}
  {{- $cleanupRes := dict "resources" (dict "requests" (dict "cpu" "50m" "memory" "64Mi") "limits" (dict "cpu" "200m" "memory" "128Mi")) -}}
  {{- $chartWorkloads = append $chartWorkloads (dict "values" $cleanupRes "pods" 1 "surges" false) -}}
{{- end -}}

{{- range $workload := $chartWorkloads -}}
  {{- $res := include "kube-agents.workloadResources" $workload.values | fromJson -}}
  {{- $replicas := $workload.pods | int64 -}}
  {{- $reqPods = add $reqPods $replicas -}}
  {{- $reqCpu = add $reqCpu (mul $res.cpuRequest $replicas) -}}
  {{- $limCpu = add $limCpu (mul $res.cpuLimit $replicas) -}}
  {{- $reqMem = add $reqMem (mul $res.memoryRequest $replicas) -}}
  {{- $limMem = add $limMem (mul $res.memoryLimit $replicas) -}}
  {{- $reqEph = add $reqEph (mul $res.ephemeralRequest $replicas) -}}
  {{- $limEph = add $limEph (mul $res.ephemeralLimit $replicas) -}}
  {{- /* A workload scaled to zero has no pod to surge from, so it sizes no patch. */ -}}
  {{- if and $workload.surges (gt $replicas (int64 0)) -}}
    {{- $surgeCpuReq = max $surgeCpuReq $res.cpuRequest -}}
    {{- $surgeCpuLim = max $surgeCpuLim $res.cpuLimit -}}
    {{- $surgeMemReq = max $surgeMemReq $res.memoryRequest -}}
    {{- $surgeMemLim = max $surgeMemLim $res.memoryLimit -}}
    {{- $surgeEphReq = max $surgeEphReq $res.ephemeralRequest -}}
    {{- $surgeEphLim = max $surgeEphLim $res.ephemeralLimit -}}
  {{- end -}}
{{- end -}}

{{- /* Hindsight's PostgreSQL claim, from the same key templates/hindsight.yaml renders the
       volumeClaimTemplate request from. */ -}}
{{- if include "kube-agents.hindsightEnabled" . -}}
  {{- $pgStorage := .Values.hindsight.postgresql.storage -}}
  {{- /* values.schema.json closes hindsight.postgresql to image, resources and storage, but
         `additionalProperties: false` rejects extra keys rather than requiring the ones
         listed, and the schema has no `required` anywhere. So `storage: null` is valid
         input that parses as 0 and under-counts requests.storage by the whole claim,
         passing a quota that cannot hold it. The StatefulSet would render an empty request
         from the same key, so failing here is the honest answer. */ -}}
  {{- if or (kindIs "invalid" $pgStorage) (eq (toString $pgStorage) "") -}}
    {{- fail "quota preflight: hindsight.postgresql.storage is empty, so the PostgreSQL claim cannot be sized. Set it to the size the StatefulSet should request, or set quotaPreflight.enabled=false to skip the check." -}}
  {{- end -}}
  {{- $reqPvc = add $reqPvc 1 -}}
  {{- $reqStorage = add $reqStorage (include "kube-agents.parseBytes" $pgStorage | int64) -}}
{{- end -}}

{{- /* Operator-rendered workloads (footprint.yaml) */ -}}
{{- if .Values.platformAgent.enabled -}}
  {{- /* The agent pod is the one operator-rendered workload that scales: the gateway
         Deployment takes spec.replicas from availability.replicas, while the shell
         StatefulSet and the credential proxy stay at 1 (see the platformagent-ha golden,
         where the gateway goes to 3 and the other two do not). The footprint records one
         pod's worth, so it is multiplied here — without this an HA install passes the
         check and then leaves its extra replicas Pending, which is the failure this
         whole template exists to prevent. An absent value means the operator's own
         default of 1; an explicit 0 means 0, and the CRD allows it. */ -}}
  {{- $agentReplicas := include "kube-agents.replicaCount" (((.Values.platformAgent.deployment | default dict).availability | default dict).replicas) | int64 -}}
  {{- $base := (index $op "agentPod" "base") | default dict -}}
  {{- $podReqCpu := $base.cpuMillisRequest | default 0 | int64 -}}
  {{- $podLimCpu := $base.cpuMillisLimit | default 0 | int64 -}}
  {{- $podReqMem := $base.memoryBytesRequest | default 0 | int64 -}}
  {{- $podLimMem := $base.memoryBytesLimit | default 0 | int64 -}}
  {{- $podReqEph := $base.ephemeralStorageBytesRequest | default 0 | int64 -}}
  {{- $podLimEph := $base.ephemeralStorageBytesLimit | default 0 | int64 -}}

  {{- /* The dashboard is another container in the agent pod rather than a pod of its own,
         so it scales with the same replica count and adds no pod. Its flag is
         harness.hermes.dashboardEnabled; reading it one level up at harness.dashboardEnabled
         matches nothing, leaves this branch dead, and counts the dashboard even when it is
         switched off. `null` there means "no opinion", so the CRD default (true) applies. */ -}}
  {{- $hermes := (index (.Values.platformAgent.harness | default dict) "hermes") | default dict -}}
  {{- $dashEnabled := true -}}
  {{- if kindIs "bool" (index $hermes "dashboardEnabled") -}}
    {{- $dashEnabled = index $hermes "dashboardEnabled" -}}
  {{- end -}}
  {{- if $dashEnabled -}}
    {{- $dash := (index $op "agentPod" "dashboard") | default dict -}}
    {{- $podReqCpu = add $podReqCpu ($dash.cpuMillisRequest | default 0 | int64) -}}
    {{- $podLimCpu = add $podLimCpu ($dash.cpuMillisLimit | default 0 | int64) -}}
    {{- $podReqMem = add $podReqMem ($dash.memoryBytesRequest | default 0 | int64) -}}
    {{- $podLimMem = add $podLimMem ($dash.memoryBytesLimit | default 0 | int64) -}}
    {{- $podReqEph = add $podReqEph ($dash.ephemeralStorageBytesRequest | default 0 | int64) -}}
    {{- $podLimEph = add $podLimEph ($dash.ephemeralStorageBytesLimit | default 0 | int64) -}}
  {{- end -}}

  {{- $reqPods = add $reqPods (mul ($base.pods | default 1 | int64) $agentReplicas) -}}
  {{- $reqCpu = add $reqCpu (mul $podReqCpu $agentReplicas) -}}
  {{- $limCpu = add $limCpu (mul $podLimCpu $agentReplicas) -}}
  {{- $reqMem = add $reqMem (mul $podReqMem $agentReplicas) -}}
  {{- $limMem = add $limMem (mul $podLimMem $agentReplicas) -}}
  {{- $reqEph = add $reqEph (mul $podReqEph $agentReplicas) -}}
  {{- $limEph = add $limEph (mul $podLimEph $agentReplicas) -}}
  {{- /* Only an HA gateway surges. The operator gives the gateway Deployment a
         RollingUpdate strategy only when availability.replicas is above 1 and renders
         `strategy: Recreate` otherwise (resolveDeploymentReplicasAndStrategy in
         k8s-operator/internal/controller/manifest_helpers.go; the default-CR golden
         platformagent.yaml shows Recreate, the platformagent-ha one RollingUpdate). A
         Recreate rollout deletes the old Pod before creating the new one, so it needs no
         extra room. Counting the agent pod here at one replica made it win the max on
         every default install — it is the largest workload in the release — and the
         printed patch then asked for an agent pod's worth of CPU, memory and ephemeral
         storage that the release can never consume. */ -}}
  {{- if gt $agentReplicas (int64 1) -}}
    {{- $surgeCpuReq = max $surgeCpuReq $podReqCpu -}}
    {{- $surgeCpuLim = max $surgeCpuLim $podLimCpu -}}
    {{- $surgeMemReq = max $surgeMemReq $podReqMem -}}
    {{- $surgeMemLim = max $surgeMemLim $podLimMem -}}
    {{- $surgeEphReq = max $surgeEphReq $podReqEph -}}
    {{- $surgeEphLim = max $surgeEphLim $podLimEph -}}
  {{- end -}}

  {{- /* Workloads rendered by the operator outside the agent pod (e.g. shellSandbox,
         credentialProxy, and any future workload added to operatorRendered). Iterating
         generic keys here ensures that any workload summed into extract_footprint is
         automatically counted by the preflight without requiring manual template edits.
         agentPod and storage are handled separately above and below. */ -}}
  {{- range $key, $workload := $op -}}
    {{- if and (ne $key "agentPod") (ne $key "storage") -}}
      {{- $reqPods = add $reqPods (include "kube-agents.replicaCount" $workload.pods | int64) -}}
      {{- $reqCpu = add $reqCpu ($workload.cpuMillisRequest | default 0 | int64) -}}
      {{- $limCpu = add $limCpu ($workload.cpuMillisLimit | default 0 | int64) -}}
      {{- $reqMem = add $reqMem ($workload.memoryBytesRequest | default 0 | int64) -}}
      {{- $limMem = add $limMem ($workload.memoryBytesLimit | default 0 | int64) -}}
      {{- $reqEph = add $reqEph ($workload.ephemeralStorageBytesRequest | default 0 | int64) -}}
      {{- $limEph = add $limEph ($workload.ephemeralStorageBytesLimit | default 0 | int64) -}}
    {{- end -}}
  {{- end -}}

  {{- /* Claims are release-scoped rather than per-replica, so they are not multiplied. */ -}}
  {{- $storage := (index $op "storage") | default dict -}}
  {{- $reqPvc = add $reqPvc ($storage.persistentVolumeClaims | default 0 | int64) -}}
  {{- $reqStorage = add $reqStorage ($storage.storageBytesRequest | default 0 | int64) -}}
{{- end -}}

{{- dict
      "pods" $reqPods
      "requestsCpu" $reqCpu "limitsCpu" $limCpu
      "requestsMemory" $reqMem "limitsMemory" $limMem
      "requestsEphemeral" $reqEph "limitsEphemeral" $limEph
      "persistentVolumeClaims" $reqPvc "requestsStorage" $reqStorage
      "surgeRequestsCpu" $surgeCpuReq "surgeLimitsCpu" $surgeCpuLim
      "surgeRequestsMemory" $surgeMemReq "surgeLimitsMemory" $surgeMemLim
      "surgeRequestsEphemeral" $surgeEphReq "surgeLimitsEphemeral" $surgeEphLim
   | toJson -}}
{{- end }}

{{- define "kube-agents.quotaCheckItems" -}}
{{- $ctx := .ctx -}}
{{- $r := .required -}}
{{- /* Every deficient quota, not the first one. `fail` inside the loop reported one quota
       per render, so a namespace with two of them was a patch-and-retry cycle — and the
       whole value of this check is telling the operator what to fix in one pass. */ -}}
{{- $quotaReports := list -}}
{{- range $quota := .items -}}
  {{- $spec := index $quota "spec" | default dict -}}
  {{- $scopes := index $spec "scopes" -}}
  {{- $scopeSelector := index $spec "scopeSelector" -}}
  {{- if or $scopes $scopeSelector -}}
    {{- /* Scoped quota: it applies to a subset of pods this template cannot identify, so
           comparing the whole release against it would be wrong in both directions. */ -}}
  {{- else -}}
    {{- $hard := index $spec "hard" | default dict -}}
    {{- $status := index $quota "status" | default dict -}}
    {{- $used := index $status "used" | default dict -}}
    {{- $shortfalls := list -}}
    {{- $patchEntries := list -}}

    {{- range $key, $hardRaw := $hard -}}
      {{- $req := 0 -}}
      {{- /* Named $surgeRoom rather than the fencepost name used by
             kube-agents.rollingUpdateFenceposts: tests/test_deployments_rollout_quota.py
             scans this whole file for an unguarded reassignment of that name, which would be
             a rollingUpdate fencepost pinned for every install. This is a different quantity
             — the headroom the remediation patch leaves — and sharing the name would make
             that check unreadable. */ -}}
      {{- $surgeRoom := 0 -}}
      {{- $isCpu := false -}}
      {{- $isBytes := false -}}
      {{- $isCount := false -}}
      {{- /* Claim-shaped keys. A PersistentVolumeClaim outlives the release that created it:
             the shell StatefulSet sets persistentVolumeClaimRetentionPolicy Retain/Retain, so
             `helm uninstall` leaves its claims behind and they appear in `used` on the next
             install — claims this release will reuse by name rather than create again.
             Charging them twice refused a reinstall into a namespace sized exactly for the
             release, and the patch it printed asked for 50% more claims than the release
             will ever hold. `hard` still has to fit the release, which is the comparison
             that catches a quota genuinely too small. */ -}}
      {{- $isClaimShaped := false -}}

      {{- if eq $key "limits.cpu" -}}
        {{- $req = int64 $r.limitsCpu -}}
        {{- $surgeRoom = int64 $r.surgeLimitsCpu -}}
        {{- $isCpu = true -}}
      {{- else if or (eq $key "requests.cpu") (eq $key "cpu") -}}
        {{- $req = int64 $r.requestsCpu -}}
        {{- $surgeRoom = int64 $r.surgeRequestsCpu -}}
        {{- $isCpu = true -}}
      {{- else if eq $key "limits.memory" -}}
        {{- $req = int64 $r.limitsMemory -}}
        {{- $surgeRoom = int64 $r.surgeLimitsMemory -}}
        {{- $isBytes = true -}}
      {{- else if or (eq $key "requests.memory") (eq $key "memory") -}}
        {{- $req = int64 $r.requestsMemory -}}
        {{- $surgeRoom = int64 $r.surgeRequestsMemory -}}
        {{- $isBytes = true -}}
      {{- else if eq $key "limits.ephemeral-storage" -}}
        {{- $req = int64 $r.limitsEphemeral -}}
        {{- /* A surge Pod brings its ephemeral storage with it, same as its CPU and memory. */ -}}
        {{- $surgeRoom = int64 $r.surgeLimitsEphemeral -}}
        {{- $isBytes = true -}}
      {{- else if or (eq $key "requests.ephemeral-storage") (eq $key "ephemeral-storage") -}}
        {{- $req = int64 $r.requestsEphemeral -}}
        {{- $surgeRoom = int64 $r.surgeRequestsEphemeral -}}
        {{- $isBytes = true -}}
      {{- else if or (eq $key "requests.storage") (eq $key "storage") -}}
        {{- $req = int64 $r.requestsStorage -}}
        {{- /* No surge room: a surge Pod mounts the existing claim rather than creating one. */ -}}
        {{- $isBytes = true -}}
        {{- $isClaimShaped = true -}}
      {{- else if or (eq $key "pods") (eq $key "count/pods") -}}
        {{- $req = int64 $r.pods -}}
        {{- /* One surge Pod, for the same reason as the CPU and memory surge. */ -}}
        {{- $surgeRoom = 1 -}}
        {{- $isCount = true -}}
      {{- else if or (eq $key "persistentvolumeclaims") (eq $key "count/persistentvolumeclaims") -}}
        {{- $req = int64 $r.persistentVolumeClaims -}}
        {{- $isCount = true -}}
        {{- $isClaimShaped = true -}}
      {{- end -}}

      {{- if or $isCpu $isBytes $isCount -}}
        {{- $hardVal := 0 -}}
        {{- $usedVal := 0 -}}
        {{- if $isCpu -}}
          {{- $hardVal = include "kube-agents.parseCpuMillis" $hardRaw | int64 -}}
          {{- $usedVal = include "kube-agents.parseCpuMillis" (index $used $key | default "0") | int64 -}}
        {{- else if $isBytes -}}
          {{- $hardVal = include "kube-agents.parseBytes" $hardRaw | int64 -}}
          {{- $usedVal = include "kube-agents.parseBytes" (index $used $key | default "0") | int64 -}}
        {{- else if $isCount -}}
          {{- $hardVal = include "kube-agents.parseCount" $hardRaw | int64 -}}
          {{- $usedVal = include "kube-agents.parseCount" (index $used $key | default "0") | int64 -}}
        {{- end -}}

        {{- $availVal := sub $hardVal $usedVal -}}
        {{- $failed := false -}}
        {{- $reason := "" -}}

        {{- if lt $hardVal $req -}}
          {{- $failed = true -}}
          {{- $reason = "quota hard capacity is less than required" -}}
        {{- else if and $ctx.Release.IsInstall (not $isClaimShaped) (lt $availVal $req) -}}
          {{- $failed = true -}}
          {{- $reason = "available headroom (hard - used) is less than required for fresh install" -}}
        {{- end -}}

        {{- if $failed -}}
          {{- $patchTarget := 0 -}}
          {{- if and $ctx.Release.IsInstall (not $isClaimShaped) -}}
            {{- /* used + required + one surge Pod. Exactly used+required fits the release at
                   rest and then stalls its first rolling update. */ -}}
            {{- $patchTarget = add $usedVal $req $surgeRoom -}}
          {{- else -}}
            {{- /* On upgrade — and for claim-shaped keys on install, where retained PVCs from a
                   previous install may already sit in `used` — adding `used` asks for the
                   release twice: a release needing 6 pods with 4 running was told to patch to
                   11 where 7 does, and a 22Gi claim requirement with 22Gi retained asked for
                   44Gi. What a neighbouring workload holds is in `used` too and cannot be told
                   apart from the release's own, so this is the release's need plus a surge Pod
                   — right in a namespace the release has to itself, and short by the neighbours'
                   share in one it does not. */ -}}
            {{- $patchTarget = add $req $surgeRoom -}}
          {{- end -}}
          {{- $reqFormatted := "" -}}
          {{- $hardFormatted := "" -}}
          {{- $availFormatted := "" -}}
          {{- $patchVal := "" -}}
          {{- if $isCpu -}}
            {{- $reqFormatted = include "kube-agents.formatCpu" $req -}}
            {{- $hardFormatted = include "kube-agents.formatCpu" $hardVal -}}
            {{- $availFormatted = include "kube-agents.formatCpu" $availVal -}}
            {{- $patchVal = include "kube-agents.formatCpu" $patchTarget -}}
          {{- else if $isBytes -}}
            {{- $reqFormatted = include "kube-agents.formatBytesApprox" $req -}}
            {{- /* The quota's own spelling, not a re-rendering of it: `hard 10G` is what
                   `kubectl describe resourcequota` shows, so echoing it verbatim is both
                   shorter and easier to match up than any unit this could pick. */ -}}
            {{- $hardFormatted = toString $hardRaw -}}
            {{- $availFormatted = include "kube-agents.formatBytesApprox" $availVal -}}
            {{- $patchVal = include "kube-agents.formatBytesCeil" $patchTarget -}}
          {{- else -}}
            {{- $reqFormatted = printf "%d" $req -}}
            {{- $hardFormatted = printf "%d" $hardVal -}}
            {{- $availFormatted = printf "%d" $availVal -}}
            {{- $patchVal = printf "%d" $patchTarget -}}
          {{- end -}}

          {{- $line := printf "  - %s: required %s, hard %s, available %s (%s)" $key $reqFormatted $hardFormatted $availFormatted $reason -}}
          {{- $shortfalls = append $shortfalls $line -}}
          {{- $patchEntries = append $patchEntries (printf "%q:%q" $key $patchVal) -}}
        {{- end -}}
      {{- end -}}
    {{- end -}}

    {{- if gt (len $shortfalls) 0 -}}
      {{- $qName := (index (index $quota "metadata" | default dict) "name") | default "resourcequota" -}}
      {{- $patchBody := printf "{\"spec\":{\"hard\":{%s}}}" (join "," $patchEntries) -}}
      {{- $patchCmd := printf "kubectl patch resourcequota %s -n %s --type=strategic --patch '%s'" $qName $ctx.Release.Namespace $patchBody -}}
      {{- $report := printf "ResourceQuota %q in namespace %q has insufficient capacity for release %q:\n%s\n\nRemediation: increase the quota with:\n  %s\n(those values leave room for one rollout surge Pod, except for claim counts and storage, which a surge Pod does not add to)" $qName $ctx.Release.Namespace $ctx.Release.Name (join "\n" $shortfalls) $patchCmd -}}
      {{- $quotaReports = append $quotaReports $report -}}
    {{- end -}}
  {{- end -}}
{{- end -}}
{{- if gt (len $quotaReports) 0 -}}
  {{- fail (printf "%s\n\nor bypass this check with --set quotaPreflight.enabled=false" (join "\n\n" $quotaReports)) -}}
{{- end -}}
{{- end }}

{{- define "kube-agents.quotaPreflight" -}}
{{- if .Values.quotaPreflight.enabled -}}
{{- $rawQuotas := lookup "v1" "ResourceQuota" .Release.Namespace "" -}}
{{- $quotas := $rawQuotas | default dict -}}
{{- $items := list -}}
{{- if kindIs "map" $quotas -}}
  {{- $items = index $quotas "items" | default list -}}
{{- end -}}
{{- if gt (len $items) 0 -}}
  {{- $required := include "kube-agents.quotaRequirements" . | fromJson -}}
  {{- include "kube-agents.quotaCheckItems" (dict "ctx" . "items" $items "required" $required) -}}
{{- end -}}
{{- end -}}
{{- end }}
