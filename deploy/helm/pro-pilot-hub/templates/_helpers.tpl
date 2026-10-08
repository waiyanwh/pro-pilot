{{- define "pro-pilot-hub.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "pro-pilot-hub.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- if eq .Release.Name (include "pro-pilot-hub.name" .) }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name (include "pro-pilot-hub.name" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "pro-pilot-hub.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "pro-pilot-hub.labels" -}}
helm.sh/chart: {{ include "pro-pilot-hub.chart" . }}
{{ include "pro-pilot-hub.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "pro-pilot-hub.selectorLabels" -}}
app.kubernetes.io/name: {{ include "pro-pilot-hub.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: hub
{{- end }}

{{- define "pro-pilot-hub.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "pro-pilot-hub.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}
