{{- define "aigw.name" -}}{{ .Chart.Name }}{{- end -}}
{{- define "aigw.labels" -}}
app.kubernetes.io/name: {{ include "aigw.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end -}}
{{- define "aigw.env" -}}
- name: AIGW_ROLE
  value: {{ .role | quote }}
envFrom:
- configMapRef:
    name: {{ .root.Release.Name }}-config
- secretRef:
    name: {{ .root.Values.existingSecret }}
{{- end -}}
