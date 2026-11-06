#!/bin/sh
# Write the frontend's settings from the pod's environment, which comes from windops-settings, so
# the same image works in any environment. nginx serves the file as /config.json. A value that
# is not a plain name stops the pod from starting, so nothing odd reaches the page.
set -eu
for value in "${WINDOPS_KEYCLOAK_REALM:-}" "${WINDOPS_WEB_CLIENT_ID:-}"; do
  if ! printf '%s\n' "$value" | grep -Eq '^[A-Za-z0-9._-]{1,64}$'; then
    echo "windops: WINDOPS_KEYCLOAK_REALM and WINDOPS_WEB_CLIENT_ID must be set to plain names" >&2
    exit 1
  fi
done
mkdir -p /tmp/windops
printf '{"realm":"%s","clientId":"%s"}\n' "$WINDOPS_KEYCLOAK_REALM" "$WINDOPS_WEB_CLIENT_ID" >/tmp/windops/config.json
