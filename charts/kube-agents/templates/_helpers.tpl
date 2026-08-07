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
back to imageRegistry, since a single-prefix mirror is the common case, and
mirrors what third_party_registry_prefix does in k8s-operator/scripts/common.sh.
Takes the root context.
*/}}
{{- define "kube-agents.thirdPartyImageRegistry" -}}
{{- $g := .Values.global | default dict -}}
{{- $g.thirdPartyImageRegistry | default $g.imageRegistry | default "" | trimSuffix "/" -}}
{{- end }}

{{/*
Rewrite an image repository onto a registry prefix, keeping only the trailing
image name: quay.io/jetstack/cert-manager-webhook under "reg.example.com/m"
becomes reg.example.com/m/cert-manager-webhook. That flat layout is what
scripts/mirror_images.sh writes and what the operator assumes when it derives
the credential-proxy reference from the agent one. An empty registry returns
the repository untouched, so a default install renders byte-identically.

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
Selector labels for the operator Deployment. Kept minimal and stable:
selectors are immutable once the Deployment exists.
*/}}
{{- define "kube-agents.operatorSelectorLabels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}-operator
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
