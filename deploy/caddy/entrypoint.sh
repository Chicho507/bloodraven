#!/bin/sh
set -eu
# The secret is mounted only in the proxy, never passed as a build argument.
token_file=/run/secrets/cloudflare_api_token
if [ ! -r "$token_file" ] || [ ! -s "$token_file" ]; then
    echo 'Falta el archivo local del token DNS de Cloudflare.' >&2
    exit 1
fi
CF_API_TOKEN=$(cat "$token_file")
if [ -z "$CF_API_TOKEN" ]; then
    echo 'El token DNS de Cloudflare está vacío.' >&2
    exit 1
fi
export CF_API_TOKEN
exec "$@"
