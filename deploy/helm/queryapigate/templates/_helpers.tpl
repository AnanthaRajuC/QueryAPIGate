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
{{- if or (hasKey .Values.secret.values "QUERYAPIGATE_REDIS_URL") .Values.redis.inExistingSecret -}}true{{- end -}}
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
{{- fail "More than one pod needs Redis, or rate limits, max_concurrent and quotas are counted per pod: set QUERYAPIGATE_REDIS_URL in secret.values - or, if it is in your existingSecret, redis.inExistingSecret: true." -}}
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
{{- with .Values.configAsCode -}}
{{- if and .enabled (not .file) (not .existingConfigMap) -}}
{{- fail "configAsCode.enabled needs the file: configAsCode.file (the YAML, inline) or configAsCode.existingConfigMap." -}}
{{- end -}}
{{- if and .file .existingConfigMap -}}
{{- fail "configAsCode.file and configAsCode.existingConfigMap are two ways to give the file - choose one." -}}
{{- end -}}
{{- end -}}
{{- range $name := list "QUERYAPIGATE_CONFIG_FILE" "QUERYAPIGATE_CONFIG_PRUNE" "QUERYAPIGATE_CONFIG_LOCKED" -}}
{{- if hasKey $.Values.config $name -}}
{{- fail (printf "Set %s through configAsCode, not config - the chart mounts the file and sets it." $name) -}}
{{- end -}}
{{- end -}}
{{- /* config is plain env in the Deployment, read by anyone who can get deployments: no secrets there. */ -}}
{{- range $name := list "QUERYAPIGATE_API_KEY" "QUERYAPIGATE_SECRET_KEY" "QUERYAPIGATE_DATABASE_URL" "QUERYAPIGATE_REDIS_URL" "QUERYAPIGATE_AUDIT_KEY" "QUERYAPIGATE_MASK_KEY" "QUERYAPIGATE_JWT_SECRET" "QUERYAPIGATE_SSO_CLIENT_SECRET" "OTEL_EXPORTER_OTLP_HEADERS" "OTEL_EXPORTER_OTLP_TRACES_HEADERS" "OTEL_EXPORTER_OTLP_LOGS_HEADERS" -}}
{{- if hasKey $.Values.config $name -}}
{{- fail (printf "%s is a secret: put it in your existingSecret or secret.values, not config - config is plain text in the Deployment." $name) -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* The environment every QueryAPIGate process gets: the Secret, then config, then what the chart itself knows. */}}
{{- define "queryapigate.env" -}}
{{- $root := .root -}}
envFrom:
  - secretRef: {name: {{ include "queryapigate.secretName" $root }}}
env:
  - {name: QUERYAPIGATE_HOME, value: /data}
  {{- if and $root.Values.ingress.enabled (not (hasKey $root.Values.config "QUERYAPIGATE_TRUST_PROXY")) }}
  - {name: QUERYAPIGATE_TRUST_PROXY, value: "1"}   # one proxy in front: the ingress controller
  {{- end }}
  {{- range $name, $value := $root.Values.config }}
  - {name: {{ $name }}, value: {{ $value | toString | quote }}}
  {{- end }}
  {{- if $root.Values.vaultToken.enabled }}
  - {name: QUERYAPIGATE_VAULT_K8S_TOKEN_FILE, value: /var/run/secrets/vault/token}
  {{- end }}
  {{- if and .server $root.Values.configAsCode.enabled }}
  # the API server applies the file as it starts, once per file (ADR 0014)
  - {name: QUERYAPIGATE_CONFIG_FILE, value: {{ printf "/etc/queryapigate/config/%s" $root.Values.configAsCode.key | quote }}}
  {{- if $root.Values.configAsCode.prune }}
  - {name: QUERYAPIGATE_CONFIG_PRUNE, value: "1"}
  {{- end }}
  {{- if $root.Values.configAsCode.locked }}
  - {name: QUERYAPIGATE_CONFIG_LOCKED, value: "1"}
  {{- end }}
  {{- end }}
{{- end -}}

{{- define "queryapigate.configMapName" -}}
{{- .Values.configAsCode.existingConfigMap | default (printf "%s-config" (include "queryapigate.fullname" .)) -}}
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
  {{- if and (eq .component "server") .root.Values.configAsCode.enabled .root.Values.configAsCode.file }}
  # a change to the file rolls the API server pods, and the first new one applies it
  checksum/config-file: {{ .root.Values.configAsCode.file | sha256sum | trunc 16 }}
  {{- end }}
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


{{/* The projected token for Vault's Kubernetes auth (ADR 0015): the volume, and where it's mounted. */}}
{{- define "queryapigate.vaultTokenVolume" -}}
{{- if .Values.vaultToken.enabled }}
- name: vault-token
  projected:
    sources:
      - serviceAccountToken:
          path: token
          expirationSeconds: {{ .Values.vaultToken.expirationSeconds }}
          {{- with .Values.vaultToken.audience }}
          audience: {{ . | quote }}
          {{- end }}
{{- end }}
{{- end -}}

{{- define "queryapigate.vaultTokenMount" -}}
{{- if .Values.vaultToken.enabled }}
- {name: vault-token, mountPath: /var/run/secrets/vault, readOnly: true}
{{- end }}
{{- end -}}
