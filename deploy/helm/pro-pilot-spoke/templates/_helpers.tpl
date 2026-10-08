{{- define "pro-pilot-spoke.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "pro-pilot-spoke.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- if eq .Release.Name (include "pro-pilot-spoke.name" .) }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name (include "pro-pilot-spoke.name" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "pro-pilot-spoke.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "pro-pilot-spoke.labels" -}}
helm.sh/chart: {{ include "pro-pilot-spoke.chart" . }}
{{ include "pro-pilot-spoke.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "pro-pilot-spoke.selectorLabels" -}}
app.kubernetes.io/name: {{ include "pro-pilot-spoke.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: spoke
{{- end }}

{{- define "pro-pilot-spoke.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default "pro-pilot-spoke" .Values.serviceAccount.name }}
{{- else }}
{{- required "serviceAccount.name is required when serviceAccount.create=false" .Values.serviceAccount.name }}
{{- end }}
{{- end }}
