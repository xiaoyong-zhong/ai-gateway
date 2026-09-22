[CmdletBinding()]
param()

# P0 uses LiteLLM SpendLogs for model/token usage and Envoy native metrics for
# ingress traffic. Higress AI Statistics is not an acceptance dependency in
# the current all-in-one Docker test topology.
Write-Output 'SKIP: AI Statistics is intentionally excluded from local P0 acceptance; use LiteLLM SpendLogs for model usage and P0 Prometheus/Envoy metrics for ingress.'
Write-Output 'This script does not treat an empty metric response as success.'
exit 0
