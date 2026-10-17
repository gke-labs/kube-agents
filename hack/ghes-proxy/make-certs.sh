#!/usr/bin/env bash
# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Makes a private CA and a server certificate for the GHES proxy, at run time.
#
#   hack/ghes-proxy/make-certs.sh <hostname> <out-dir> [days]
#
# Writes to <out-dir>:
#   ca.crt    the CA certificate. Give it to the clients: the forge's
#             caBundleRef Secret, the token minter, and BENCH_FORGE_CA_FILE.
#   tls.crt   the server certificate for <hostname>, signed by the CA.
#   tls.key   the server private key (mode 0600).
#
# The CA private key signs one certificate and is then deleted, so nothing
# can sign a second one. <out-dir> must be outside this repository, so that
# no key can be committed. The certificates expire after [days] (default 30).
set -euo pipefail

usage() {
  echo "usage: $0 <hostname> <out-dir> [days]" >&2
  exit 2
}

[ "$#" -ge 2 ] && [ "$#" -le 3 ] || usage
host=$1
out=$2
days=${3:-30}

if ! [[ "$host" =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*$ ]]; then
  echo "make-certs: '$host' is not a bare hostname (no scheme, port or path)" >&2
  exit 2
fi
if ! [[ "$days" =~ ^[1-9][0-9]*$ ]]; then
  echo "make-certs: days '$days' is not a positive integer" >&2
  exit 2
fi

created=
if [ ! -d "$out" ]; then
  mkdir -p "$out"
  created=1
fi
out=$(cd "$out" && pwd -P)
repo=$(cd "$(dirname "$0")/../.." && pwd -P)
case "$out/" in
  "$repo"/*)
    [ -z "$created" ] || rmdir "$out"
    echo "make-certs: $out is inside the repository ($repo); write the keys somewhere else" >&2
    exit 2
    ;;
esac
for f in ca.crt tls.crt tls.key; do
  if [ -e "$out/$f" ]; then
    echo "make-certs: $out/$f exists; use an empty directory" >&2
    exit 2
  fi
done

umask 077
work=$(mktemp -d "$out/.make-certs.XXXXXX")
trap 'rm -rf "$work"' EXIT

# openssl, with its output kept and shown only when it fails: a failed step
# must say why, not exit silently.
ossl() {
  if ! openssl "$@" >"$work/openssl.log" 2>&1; then
    echo "make-certs: 'openssl $1' failed:" >&2
    cat "$work/openssl.log" >&2
    exit 1
  fi
}

ossl req -x509 -new -nodes -sha256 -days "$days" \
  -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
  -keyout "$work/ca.key" -out "$work/ca.crt" \
  -subj "/CN=ghes-proxy eval CA for $host" \
  -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -addext "subjectKeyIdentifier=hash"

ossl req -new -nodes -sha256 \
  -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
  -keyout "$work/tls.key" -out "$work/tls.csr" \
  -subj "/CN=$host"

cat >"$work/ext.cnf" <<EOF
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature
extendedKeyUsage=serverAuth
subjectAltName=DNS:$host
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
EOF

ossl x509 -req -sha256 -days "$days" \
  -in "$work/tls.csr" -CA "$work/ca.crt" -CAkey "$work/ca.key" -set_serial "0x$(openssl rand -hex 16)" \
  -extfile "$work/ext.cnf" -out "$work/tls.crt"

ossl verify -CAfile "$work/ca.crt" "$work/tls.crt"

mv "$work/ca.crt" "$work/tls.crt" "$work/tls.key" "$out/"
chmod 0644 "$out/ca.crt" "$out/tls.crt"
chmod 0600 "$out/tls.key"
echo "make-certs: wrote $out/ca.crt, $out/tls.crt and $out/tls.key for $host, valid $days days; the CA key is deleted"
