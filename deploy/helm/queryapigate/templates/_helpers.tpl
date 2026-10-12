{{/* Names, labels and the pieces every Deployment shares. */}}

{{- define "queryapigate.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "queryapigate.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "queryapigate.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
app.kubernetes.io/name: {{ include "queryapigate.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/* Selector labels for one component: server, events or mcp. */}}
{{- define "queryapigate.selectorLabels" -}}
app.kubernetes.io/name: {{ include "queryapigate.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "queryapigate.image" -}}
{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}
{{- end -}}

{{- define "queryapigate.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- .Values.serviceAccount.name | default (include "queryapigate.fullname" .) -}}
{{- else -}}
{{- .Values.serviceAccount.name | default "default" -}}
{{- end -}}
{{- end -}}

{{- define "queryapigate.secretName" -}}
{{- .Values.existingSecret | default (include "queryapigate.fullname" .) -}}
{{- end -}}

{{/* Whether QUERYAPIGATE_REDIS_URL is set somewhere the chart can see, or said to be in the existing Secret. */}}
{{- define "queryapigate.hasRedis" -}}
{{- if or (hasKey .Values.config "QUERYAPIGATE_REDIS_URL") (hasKey .Values.secret.values "QUERYAPIGATE_REDIS_URL") .Values.redis.inExistingSecret -}}true{{- end -}}
{{- end -}}

{{/* Fail at install, not at runtime, on a setup that can't work (ADR 0013). */}}
{{- define "queryapigate.validate" -}}
{{- $replicas := int .Values.replicas -}}
{{- if .Values.autoscaling.enabled -}}{{- $replicas = int .Values.autoscaling.maxReplicas -}}{{- end -}}
{{- if and (eq .Values.store.type "sqlite") (gt $replicas 1) -}}
{{- fail "store.type sqlite keeps the store in one file on one volume - it runs one pod. For several, set store.type: postgres (QUERYAPIGATE_DATABASE_URL in the secret) and Redis." -}}
{{- end -}}
{{- if and (eq .Values.store.type "sqlite") .Values.autoscaling.enabled -}}
{{- fail "autoscaling needs store.type: postgres - SQLite runs one pod." -}}
{{- end -}}
{{- if and (gt $replicas 1) (not (include "queryapigate.hasRedis" .)) -}}
{{- fail "More than one pod needs Redis, or rate limits, max_concurrent and quotas are counted per pod: set QUERYAPIGATE_REDIS_URL in secret.values or config - or, if it is in your existingSecret, redis.inExistingSecret: true." -}}
{{- end -}}
{{- if and (not .Values.existingSecret) (not .Values.secret.create) -}}
{{- fail "Set existingSecret to a Secret holding QUERYAPIGATE_API_KEY (and the other secrets), or secret.create: true with secret.values." -}}
{{- end -}}
{{- if and .Values.existingSecret .Values.secret.create -}}
{{- fail "existingSecret and secret.create are two ways to give the secrets - choose one." -}}
{{- end -}}
{{- if and (eq .Values.store.type "postgres") .Values.secret.create (not (hasKey .Values.secret.values "QUERYAPIGATE_DATABASE_URL")) -}}
{{- fail "store.type postgres needs QUERYAPIGATE_DATABASE_URL in secret.values (or in your existingSecret)." -}}
{{- end -}}
{{- if and (eq .Values.store.type "postgres") (hasKey .Values.config "QUERYAPIGATE_DATABASE_URL") -}}
{{- fail "QUERYAPIGATE_DATABASE_URL holds the store's password: put it in the secret, not config." -}}
{{- end -}}
{{- end -}}

{{/* The environment every QueryAPIGate process gets: the Secret, then config, then what the chart itself knows. */}}
{{- define "queryapigate.env" -}}
envFrom:
  - secretRef: {name: {{ include "queryapigate.secretName" . }}}
env:
  - {name: QUERYAPIGATE_HOME, value: /data}
  {{- if and .Values.ingress.enabled (not (hasKey .Values.config "QUERYAPIGATE_TRUST_PROXY")) }}
  - {name: QUERYAPIGATE_TRUST_PROXY, value: "1"}   # one proxy in front: the ingress controller
  {{- end }}
  {{- range $name, $value := .Values.config }}
  - {name: {{ $name }}, value: {{ $value | toString | quote }}}
  {{- end }}
{{- end -}}

{{/* The pod-level settings every Deployment shares. */}}
{{- define "queryapigate.podSpec" -}}
serviceAccountName: {{ include "queryapigate.serviceAccountName" . }}
automountServiceAccountToken: {{ .Values.serviceAccount.automountToken }}
enableServiceLinks: false   # else a Service named queryapigate sets QUERYAPIGATE_PORT=tcp://... in the pod
securityContext: {{- toYaml .Values.podSecurityContext | nindent 2 }}
{{- with .Values.imagePullSecrets }}
imagePullSecrets: {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.priorityClassName }}
priorityClassName: {{ . }}
{{- end }}
{{- with .Values.nodeSelector }}
nodeSelector: {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.tolerations }}
tolerations: {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.affinity }}
affinity: {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}

{{- define "queryapigate.podMetadata" -}}
annotations:
  checksum/config: {{ toJson .root.Values.config | sha256sum | trunc 16 }}
  {{- if .root.Values.secret.create }}
  checksum/secret: {{ toJson .root.Values.secret.values | sha256sum | trunc 16 }}
  {{- end }}
  {{- with .root.Values.podAnnotations }}
  {{- toYaml . | nindent 2 }}
  {{- end }}
labels:
  {{- include "queryapigate.selectorLabels" . | nindent 2 }}
  {{- with .root.Values.podLabels }}
  {{- toYaml . | nindent 2 }}
  {{- end }}
{{- end -}}
