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
Selector labels for the operator Deployment. Kept minimal and stable:
selectors are immutable once the Deployment exists.
*/}}
{{- define "kube-agents.operatorSelectorLabels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}-operator
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
