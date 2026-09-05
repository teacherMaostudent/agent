#!/bin/sh
set -eu

# This job runs only in the local identity overlay. It reconciles the platform
# administrator and the BFF service account so an already-persisted Keycloak
# realm receives the same identity contract as a fresh import.
KCADM=/opt/keycloak/bin/kcadm.sh
SERVER=http://keycloak:8080
REALM=agent-platform

until "$KCADM" config credentials \
  --server "$SERVER" --realm master \
  --user "$KC_BOOTSTRAP_ADMIN_USERNAME" \
  --password "$KC_BOOTSTRAP_ADMIN_PASSWORD" >/dev/null 2>&1; do
  sleep 2
done

client_uuid="$($KCADM get clients -r "$REALM" -q clientId=agent-web-identity-admin \
  --fields id --format csv --noquotes 2>/dev/null | tr -d '\r' | head -n 1)"
if [ -z "$client_uuid" ]; then
  "$KCADM" create clients -r "$REALM" \
    -s clientId=agent-web-identity-admin -s enabled=true -s publicClient=false \
    -s standardFlowEnabled=false -s directAccessGrantsEnabled=false \
    -s serviceAccountsEnabled=true -s clientAuthenticatorType=client-secret \
    -s secret="$IDENTITY_ADMIN_CLIENT_SECRET" >/dev/null
  client_uuid="$($KCADM get clients -r "$REALM" -q clientId=agent-web-identity-admin \
    --fields id --format csv --noquotes | tr -d '\r' | head -n 1)"
else
  "$KCADM" update "clients/$client_uuid" -r "$REALM" \
    -s enabled=true -s publicClient=false -s serviceAccountsEnabled=true \
    -s clientAuthenticatorType=client-secret -s secret="$IDENTITY_ADMIN_CLIENT_SECRET" >/dev/null
fi

service_username="service-account-agent-web-identity-admin"
for role in view-users query-users manage-users view-realm; do
  "$KCADM" add-roles -r "$REALM" --uusername "$service_username" \
    --cclientid realm-management --rolename "$role" >/dev/null 2>&1 || true
done

web_client_uuid="$($KCADM get clients -r "$REALM" -q clientId=agent-web-bff \
  --fields id --format csv --noquotes 2>/dev/null | tr -d '\r' | head -n 1)"
if [ -n "$web_client_uuid" ]; then
  "$KCADM" update "clients/$web_client_uuid" -r "$REALM" \
    -s 'attributes={"pkce.code.challenge.method":"S256","post.logout.redirect.uris":"http://127.0.0.1:9010/*"}' >/dev/null
fi

user_id="$($KCADM get users -r "$REALM" -q username="$PLATFORM_ADMIN_USERNAME" \
  --fields id --format csv --noquotes 2>/dev/null | tr -d '\r' | head -n 1)"
if [ -z "$user_id" ]; then
  "$KCADM" create users -r "$REALM" -s username="$PLATFORM_ADMIN_USERNAME" \
    -s enabled=true -s emailVerified=true >/dev/null
  user_id="$($KCADM get users -r "$REALM" -q username="$PLATFORM_ADMIN_USERNAME" \
    --fields id --format csv --noquotes | tr -d '\r' | head -n 1)"
fi

# Realm imports are not re-applied to an existing Keycloak database. Reconcile every human role
# before mapping the administrator so upgrades behave the same as a fresh installation.
for role in agent-user agent-reviewer knowledge-reviewer platform-operator governance-auditor platform-super-admin; do
  if ! "$KCADM" get "roles/$role" -r "$REALM" >/dev/null 2>&1; then
    "$KCADM" create roles -r "$REALM" -s name="$role" >/dev/null
  fi
done

permissions='["rag:read","rag:ingest:approve","file:scan","tool:invoke","ops:read","release:read","release:validate","release:version:publish","release:create","release:promote","release:pause","release:rollback","model:route:read","model:route:release","model:route:monitor","model:route:rollback","quota:read","quota:write","audit:export","audit:export:requeue","eval:golden:review","agent:review","run:review:approve","run:review:assign","run:review:transfer","run:review:comment","run:review:label","evidence:content:read","run:share","run:tenant:read","tenant:read","tenant:write","identity:users:read","identity:users:write","knowledge:read","knowledge:compile","knowledge:review"]'
"$KCADM" update "users/$user_id" -r "$REALM" -s enabled=true -s emailVerified=true \
  -s 'attributes.tenant_id=["demo"]' -s "attributes.permissions=$permissions" >/dev/null
"$KCADM" set-password -r "$REALM" --username "$PLATFORM_ADMIN_USERNAME" \
  --new-password "$PLATFORM_ADMIN_PASSWORD" --temporary=false >/dev/null

for role in agent-user agent-reviewer knowledge-reviewer platform-operator governance-auditor platform-super-admin; do
  "$KCADM" add-roles -r "$REALM" --uusername "$PLATFORM_ADMIN_USERNAME" \
    --rolename "$role" >/dev/null 2>&1 || true
done

echo "Local platform identity reconciliation completed."
