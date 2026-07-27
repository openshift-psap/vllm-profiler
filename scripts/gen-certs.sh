#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   NS=vllm-profiler SVC=env-injector ./gen-certs.sh
#
# Creates a self-signed CA and server cert for ${SVC}.${NS}.svc,
# then creates/updates secret ${SVC}-certs in that namespace and
# prints the CA bundle for patching the webhook.

NS="${NS:-vllm-profiler}"
SVC="${SVC:-env-injector}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

cat > "${TMP}/openssl.cnf" <<EOF
[ req ]
default_bits       = 2048
prompt             = no
default_md         = sha256
distinguished_name = dn
req_extensions     = v3_req

[ dn ]
CN = ${SVC}.${NS}.svc

[ v3_req ]
keyUsage = keyEncipherment, dataEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt_names

[ alt_names ]
DNS.1 = ${SVC}.${NS}.svc
DNS.2 = ${SVC}.${NS}.svc.cluster.local
EOF

openssl genrsa -out "${TMP}/ca.key" 2048 >/dev/null 2>&1
openssl req -x509 -new -nodes -key "${TMP}/ca.key" -subj "/CN=${SVC}-ca" -days 3650 -out "${TMP}/ca.crt" >/dev/null 2>&1

openssl genrsa -out "${TMP}/tls.key" 2048 >/dev/null 2>&1
openssl req -new -key "${TMP}/tls.key" -out "${TMP}/server.csr" -config "${TMP}/openssl.cnf" >/dev/null 2>&1
openssl x509 -req -in "${TMP}/server.csr" -CA "${TMP}/ca.crt" -CAkey "${TMP}/ca.key" -CAcreateserial -out "${TMP}/tls.crt" -days 365 -extensions v3_req -extfile "${TMP}/openssl.cnf" >/dev/null 2>&1

oc -n "${NS}" delete secret "${SVC}-certs" --ignore-not-found
oc -n "${NS}" create secret tls "${SVC}-certs" --cert="${TMP}/tls.crt" --key="${TMP}/tls.key"

echo "CA bundle (base64):"
base64 -w0 < "${TMP}/ca.crt"
echo


