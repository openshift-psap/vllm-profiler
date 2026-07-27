#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   NS=vllm-profiler SVC=env-injector ./patch-ca-bundle.sh
#
# Reads the CA from the ${SVC}-certs secret and patches the
# MutatingWebhookConfiguration caBundle field.

NS="${NS:-vllm-profiler}"
SVC="${SVC:-env-injector}"
MWC="${MWC:-env-injector-webhook}"

CA_BUNDLE="$(oc -n "${NS}" get secret "${SVC}-certs" -o jsonpath='{.data.tls\.crt}' | base64 -d | base64 -w0)"

oc get mutatingwebhookconfiguration "${MWC}" -o json \
  | jq --arg ca "${CA_BUNDLE}" '.webhooks[].clientConfig.caBundle = $ca' \
  | oc apply -f -

echo "Patched ${MWC} caBundle."


