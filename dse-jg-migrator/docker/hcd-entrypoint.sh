#!/bin/bash
# Enables password authentication in the HCD image, then hands over to the image's own entrypoint.
#
# The HCD image ships cassandra.yaml with the AdvancedAuthenticator and AdvancedAuthorizer present but their
# `enabled: true` lines commented out, so any client is accepted without credentials. This wrapper uncomments
#   authenticator.parameters.enabled: true
#   authenticator.parameters.default_scheme: internal
#   authorizer.parameters.enabled: true
# and leaves the rest of the file — and the image's own network auto-configuration — untouched.
# Idempotent: on a restart the lines are already uncommented and the edit is a no-op.
# Fails the container start if the expected lines cannot be found (e.g. a new image layout), rather than
# starting an open database.
set -euo pipefail

CONF="${CASSANDRA_CONF:-/opt/hcd/resources/cassandra/conf}/cassandra.yaml"

# Prints the uncommented auth lines found, as "<block>.<key>", one per line.
enabled_auth_lines() {
    awk '
        /^[a-z_]+:/ { block = $1; sub(":", "", block) }
        block == "authenticator" && /^    enabled: true[[:space:]]*$/ { print "authenticator.enabled" }
        block == "authenticator" && /^    default_scheme: internal[[:space:]]*$/ { print "authenticator.default_scheme" }
        block == "authorizer" && /^    enabled: true[[:space:]]*$/ { print "authorizer.enabled" }
    ' "$CONF"
}

tmp="$(mktemp "${CONF}.XXXXXX")"
awk '
    /^[a-z_]+:/ { block = $1; sub(":", "", block) }
    block == "authenticator" && /^#     enabled: true[[:space:]]*$/ { $0 = "    enabled: true" }
    block == "authenticator" && /^#     default_scheme: internal[[:space:]]*$/ { $0 = "    default_scheme: internal" }
    block == "authorizer" && /^#     enabled: true[[:space:]]*$/ { $0 = "    enabled: true" }
    { print }
' "$CONF" >"$tmp"
cat "$tmp" >"$CONF" # keep the file's owner and mode
rm -f "$tmp"

found="$(enabled_auth_lines | sort -u | tr '\n' ' ')"
if [[ "$found" != "authenticator.default_scheme authenticator.enabled authorizer.enabled " ]]; then
    echo "ERROR: hcd-entrypoint.sh could not enable authentication in $CONF (found: ${found:-none})." >&2
    echo "       The image's cassandra.yaml layout has changed; update docker/hcd-entrypoint.sh." >&2
    exit 1
fi
echo "INFO: authentication and authorization enabled in $CONF"

exec /entrypoint.sh "$@"
