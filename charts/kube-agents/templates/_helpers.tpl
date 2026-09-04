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

Anything else — an external vendor endpoint, a bare hostname — fails the render, but only
when litellm.otel is on. Silently falling back to gke-managed-otel would emit a policy that
blocks the very collector the user just configured, and the symptom would be zero spans
with a green install. With litellm.otel off (the default) there is no LiteLLM exporter for
the policy to block, so failing the whole install over an egress rule nothing uses would
punish a user who only meant to repoint the agents.
*/}}
{{- define "kube-agents.otlpCollectorNamespace" -}}
{{- if .Values.telemetry.collectorNamespace -}}
{{- .Values.telemetry.collectorNamespace -}}
{{- else if not .Values.telemetry.otlpEndpoint -}}
gke-managed-otel
{{- else -}}
{{- $host := .Values.telemetry.otlpEndpoint | trimPrefix "https://" | trimPrefix "http://" -}}
{{- $host = (splitList "/" $host | first) -}}
{{- $host = (splitList ":" $host | first) -}}
{{- $parts := splitList "." $host -}}
{{- /*
  Only two shapes are an in-cluster Service: exactly <svc>.<ns>, or <svc>.<ns>.svc[...].
  Anything with a third label that is not "svc" is a public DNS name, and reading its
  second label as a namespace would quietly open egress to a namespace named "vendor".
*/ -}}
{{- if or (eq (len $parts) 2) (and (ge (len $parts) 3) (eq (index $parts 2) "svc")) -}}
{{- index $parts 1 -}}
{{- else if not .Values.litellm.otel -}}
gke-managed-otel
{{- else -}}
{{- fail (printf "telemetry.otlpEndpoint %q does not name an in-cluster Service, so the LiteLLM NetworkPolicy cannot tell which namespace to allow egress to. Set telemetry.collectorNamespace, or set litellm.networkPolicy=false if the policy is managed elsewhere." .Values.telemetry.otlpEndpoint) -}}
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
