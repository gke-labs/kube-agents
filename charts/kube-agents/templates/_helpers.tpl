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
and they are decidable at render time: an IPv4 literal inside private, CGNAT,
loopback, or link-local space, and a single-label hostname, which resolves through the
Pod's search domain to a Service in its own namespace. Both are in-cluster collectors
in disguise, and telemetry.collectorNamespace is the remedy, as it was before this
check existed. A DNS name that happens to resolve to private space is not decidable
here, and the docs say so.
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
{{- $namespaceAnnotation := get $crAnnotations "kubeagents.x-k8s.io/otlp-collector-namespace" | toString | trim -}}
{{- if and $operatorOwned $namespaceAnnotation (not (regexMatch "^[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$" $namespaceAnnotation)) -}}
{{- fail (printf "platformAgent.annotations[\"kubeagents.x-k8s.io/otlp-collector-namespace\"]=%q is not a valid label value, so the operator would ignore it and emit no OTLP egress rule. Give the collector's namespace name." $namespaceAnnotation) -}}
{{- end -}}
{{- /*
  The value route gets the same validation: an invalid namespace would stand this check
  aside, be stamped on the CR, and be ignored by the operator, which then emits no rule.
*/ -}}
{{- $collectorNamespaceValue := .Values.telemetry.collectorNamespace | toString | trim -}}
{{- if and $collectorNamespaceValue (not (regexMatch "^[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$" $collectorNamespaceValue)) -}}
{{- fail (printf "telemetry.collectorNamespace=%q is not a valid namespace name; the NetworkPolicy would select nothing and the operator would ignore it. Give the collector's namespace name." $collectorNamespaceValue) -}}
{{- end -}}
{{- $collectorNamespace := or $collectorNamespaceValue (and $operatorOwned $namespaceAnnotation) -}}
{{- if and .Values.litellm.otel .Values.litellm.networkPolicy .Values.telemetry.otlpEndpoint (not $crOptOut) (not $collectorNamespace) (not (include "kube-agents.otlpEndpointIsClusterLocal" .)) -}}
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
{{- $privateIPv4 := regexMatch "^(10\\.|127\\.|169\\.254\\.|192\\.168\\.|172\\.(1[6-9]|2[0-9]|3[01])\\.|100\\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\\.)[0-9]+\\.[0-9]+(\\.[0-9]+)?$" $host -}}
{{- /* A bracketed IPv6 literal cuts to "[…" with no dot; it is not a single-label host. */ -}}
{{- $singleLabel := and (not (contains "." $host)) (not (hasPrefix "[" $hostport)) -}}
{{- if or $privateIPv4 $singleLabel -}}
{{- fail (printf "telemetry.otlpEndpoint %q names %s, which litellm-policy's port-443 rule does not reach (it excepts private ranges), so the LiteLLM OTLP exporter (litellm.otel=true) would be blocked. If this is an in-cluster collector, set telemetry.collectorNamespace to its namespace; otherwise give the collector's public host." $endpoint (ternary "a private IPv4 address" "a single-label host" $privateIPv4)) -}}
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
