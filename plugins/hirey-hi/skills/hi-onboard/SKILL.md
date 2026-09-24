---
name: hi-onboard
description: First-time setup and reconnect for the Hirey Hi Claude plugin. Use after install, when credentials are missing, or when Hi returns 401 invalid_token. Preserve anonymous read access, refresh the existing installation before considering reset, and follow the host-specific version policy returned by Hi. This plugin uses REST rather than MCP.
---

# Hi Onboard (one-time bootstrap, REST + client_credentials)

Hi is Hirey's people-to-people platform. This plugin gives Claude direct REST access to Hi's tools without any MCP layer, browser OAuth flow, or user interaction. Identity is anchored by a long-lived `client_credentials` pair the assistant generates and stores at `~/.config/hi/credentials.json`. Once that file exists, every subsequent Hi call uses the cached bearer token (or refreshes it via the cached client_id + client_secret).

## Use when

- the user just enabled the `hirey-hi` plugin and is about to ask for any Hi workflow
- the user types "set up hi", "install hi", "register hi"
- you are about to call a Hi REST endpoint and the credentials file under `${XDG_CONFIG_HOME:-$HOME/.config}/hi` is absent
- the assistant just got a `401 invalid_token` or `token_expired` from a Hi endpoint — step 2 refreshes the bearer from the cached `client_credentials`, then retries the call once
- the user explicitly says "re-register", "reset hi identity", "log in again"

## Do not use when

- the XDG-aware credentials file exists and the cached token is fresh according to its returned expiry — call the target endpoint with that token, except after verified binding when one forced refresh is required
- the user is asking a workflow question (find, match, pair, meeting) — go to `hi-use` (which will pull credentials transparently)
- inbound events drain — go to `hi-events`

## Check the Claude plugin version first

At the start of setup or recovery, call the public catalog with the installed plugin version:

```bash
curl -fsS --connect-timeout 5 --max-time 30 https://hi.hirey.ai/v1/capabilities \
  -H 'x-hirey-plugin-host: claude' \
  -H 'x-hirey-plugin-version: 0.2.6' \
  | jq '._meta.hirey_plugin'
```

- `update_required=true`: run the returned official install command, then run `/reload-plugins`
  before retrying. The install command refreshes both the plugin and its Skills.
- `update_recommended=true`: tell the user an update is available, but do not block a compatible request.
- `401 missing_bearer`, `invalid_token`, or `token_expired`: rerun the guarded recovery script with `HI_FORCE_TOKEN_REFRESH=1` even when local expiry appears fresh, then retry the original request once. Never re-register.
- `403 insufficient_oauth_scope` or `forbidden`: the credential is valid; do not create another Agent or loop through bootstrap.
- A pending Agent may use only public `people.find`, `people.explain`, and staged `capture.record`. Other operations require verified identity.

## Bootstrap sequence (assistant runs all of this via Bash, no user touch)

Run the script below verbatim. It reuses existing credentials and retries only an exact saved registration request. Never bypass the lock or pending marker. The replay contract requires updated Auth and Gateway services.

**Referral attribution** — the current API does not accept `channel_code`. If one was supplied, stop and report unsupported attribution; do not silently discard it or claim attribution succeeded.

```bash
set -euo pipefail
umask 077
VERSION="0.2.6"
HI_BASE_EXPLICIT="${HI_BASE+x}"
HI_BASE="${HI_BASE:-https://hi.hirey.ai}"
CREDS_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/hi"
CREDS_FILE="$CREDS_DIR/credentials.json"
CRED_TMP=""
LOCK_OWNED=0
RECOVERY_OWNED=0
cleanup() {
  [ -z "$CRED_TMP" ] || rm -f "$CRED_TMP"
  if [ "$LOCK_OWNED" = 1 ]; then
    rm -f "$LOCK_DIR/owner.pid"
    rmdir "$LOCK_DIR" 2>/dev/null || true
  fi
  if [ "$RECOVERY_OWNED" = 1 ]; then rmdir "$RECOVERY_LOCK" 2>/dev/null || true; fi
}
trap cleanup EXIT
fail() { printf '%s\n' "$1" >&2; exit 1; }
ok() { printf '%s\n' "$1"; }
command -v openssl >/dev/null 2>&1 || fail "openssl is required for registration"
mkdir -p "$CREDS_DIR" && chmod 700 "$CREDS_DIR"
LOCK_DIR="$CREDS_DIR/.register.lock"
RECOVERY_LOCK="$CREDS_DIR/.register-recovery.lock"
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    printf '%s\n' "$$" > "$LOCK_DIR/owner.pid"
    LOCK_OWNED=1
    break
  fi
  sleep 1
done
if [ "$LOCK_OWNED" != 1 ]; then
  mkdir "$RECOVERY_LOCK" 2>/dev/null \
    || fail "hi_register_busy: another installer or lock recovery is running; inspect $RECOVERY_LOCK if this persists"
  RECOVERY_OWNED=1
  [ -f "$LOCK_DIR/owner.pid" ] \
    || fail "hi_register_lock_unknown: lock has no owner proof; inspect $LOCK_DIR before manual recovery"
  LOCK_PID=$(cat "$LOCK_DIR/owner.pid")
  case "$LOCK_PID" in ''|*[!0-9]*) fail "hi_register_lock_unknown: invalid lock owner; inspect $LOCK_DIR";; esac
  if kill -0 "$LOCK_PID" 2>/dev/null; then
    fail "hi_register_busy: installer process $LOCK_PID is still running"
  fi
  if [ "$(uname -s)" = Darwin ]; then
    LOCK_MODIFIED=$(stat -f %m "$LOCK_DIR") \
      || fail "hi_register_lock_unknown: cannot determine lock age"
  else
    LOCK_MODIFIED=$(stat -c %Y "$LOCK_DIR") \
      || fail "hi_register_lock_unknown: cannot determine lock age"
  fi
  [ $(( $(date +%s) - LOCK_MODIFIED )) -ge 60 ] \
    || fail "hi_register_busy: prior installer exited recently; retry after the 60-second recovery window"
  [ "$(cat "$LOCK_DIR/owner.pid")" = "$LOCK_PID" ] \
    || fail "hi_register_lock_unknown: lock owner changed during recovery"
  rm "$LOCK_DIR/owner.pid"
  rmdir "$LOCK_DIR" \
    || fail "hi_register_lock_unknown: lock changed during recovery; inspect $LOCK_DIR"
  mkdir "$LOCK_DIR" 2>/dev/null \
    || fail "hi_register_busy: another installer acquired the recovered lock"
  printf '%s\n' "$$" > "$LOCK_DIR/owner.pid"
  LOCK_OWNED=1
fi
# Keep the same lock through credential re-read and token refresh: refreshing a
# pending token invalidates the previous bearer for this shared installation.
HI_BASE="${HI_BASE%/}"
if [ -s "$CREDS_FILE" ]; then
  STORED_BASE=$(jq -er '.platform_base_url // empty | select(type == "string" and length > 0)' "$CREDS_FILE" 2>/dev/null || true)
  if [ -n "$STORED_BASE" ]; then
    STORED_BASE="${STORED_BASE%/}"
    if [ -n "$HI_BASE_EXPLICIT" ] && [ "$HI_BASE" != "$STORED_BASE" ]; then
      fail "hi_credential_host_mismatch: explicit HI_BASE differs from stored credential host; no credential request sent"
    fi
    HI_BASE="$STORED_BASE"
  fi
fi
printf '%s' "$HI_BASE" | jq -Re 'test("^https://[A-Za-z0-9.-]+(:[0-9]+)?$|^http://(localhost|127\\.0\\.0\\.1|\\[::1\\])(:[0-9]+)?$")' >/dev/null 2>&1 \
  || fail "hi_credential_host_insecure: use HTTPS or explicit loopback HTTP only"

# Helper: does the creds file already hold a usable client_id?
creds_have_client_id() { [ -s "$CREDS_FILE" ] && [ -n "$(jq -er 'select((.client_id | type == "string" and length > 0) and (.client_secret | type == "string" and length > 0)) | .client_id' "$CREDS_FILE" 2>/dev/null)" ]; }

# CRITICAL (identity durability): re-registering when a creds file EXISTS but is merely
# unreadable / corrupt / wrong-HOME silently mints a NEW Hi agent and ORPHANS the user's
# real identity (their listings, credits, phone-bound workspace). Audit found 204/216 prod
# agents are orphans, largely from exactly this. So: register ONLY when the file is truly
# ABSENT. A present-but-unusable file is a LOUD error the user must resolve explicitly —
# orphaning must never be silent/accidental.
if [ -e "$CREDS_FILE" ] && ! [ -r "$CREDS_FILE" ]; then
  fail "Credentials exist but are not readable: $CREDS_FILE
   Refusing to register a NEW Hi identity — that would orphan your existing agent + data.
   Fix it:  chmod 600 \"$CREDS_FILE\"   (or, to deliberately start fresh: rm it, then re-run)"
fi
if [ -e "$CREDS_FILE" ] && ! creds_have_client_id; then
  fail "Credentials exist but have no valid client_id (corrupt/empty): $CREDS_FILE
   Refusing to silently register a NEW Hi identity — that would orphan your existing agent + data.
   If this file is junk:  rm \"$CREDS_FILE\"  then re-run.  Otherwise restore it from backup."
fi

if ! [ -e "$CREDS_FILE" ]; then
  # Serialize concurrent installers: two Claude sessions racing on a fresh machine would
  # otherwise each mint a separate agent. mkdir is an atomic cross-process mutex.
    [ -z "${HI_CHANNEL_CODE:-}" ] || fail "hi_channel_attribution_unsupported: current installation API does not accept referral metadata"
    PENDING_FILE="$CREDS_DIR/.registration-pending.json"
    if [ -e "$PENDING_FILE" ]; then
      [ -f "$PENDING_FILE" ] && [ ! -L "$PENDING_FILE" ] \
        || fail "hi_registration_outcome_unknown: pending marker is not a regular file"
      REG_BODY=$(jq -cer --arg base "$HI_BASE" '
        select(.version == 1 and .base == $base and .host == "claude")
        | .body | select(type == "object")
        | select(.agent_type == "claude" and (.request_id | type == "string")
          and (.pending_proof_token | type == "string") and (.client_secret | type == "string"))
      ' "$PENDING_FILE") \
        || fail "hi_registration_outcome_unknown: legacy or invalid marker; preserve it for manual reconciliation"
    else
      REG_BODY=$(printf '%s\n%s\n%s\n%s\n' \
        "$(openssl rand -hex 16)" "hi_ai_$(openssl rand -hex 32)" \
        "$(openssl rand -hex 32)" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        | jq -Rn --arg version "$VERSION" '[inputs] |
          {agent_type:"claude",display_name:"Claude Code (Hirey skill)",client_version:$version,
            request_id:.[0],pending_proof_token:.[1],client_secret:.[2],occurred_at:.[3]}')
      PENDING_TMP=$(mktemp "$CREDS_DIR/.registration-pending.XXXXXX")
      printf '%s' "$REG_BODY" | jq -c --arg base "$HI_BASE" \
        '{version:1,base:$base,host:"claude",body:.}' > "$PENDING_TMP"
      mv "$PENDING_TMP" "$PENDING_FILE"
    fi
    REG=$(curl -fsS --connect-timeout 5 --max-time 30 -X POST "$HI_BASE/v1/agents/api-keys" \
      -H 'content-type: application/json' \
      --data-binary @- <<< "$REG_BODY") \
      || fail "hi_registration_outcome_unknown: API-key creation failed; preserve pending marker and reconcile before retry"

    printf '%s' "$REG" | jq -e '
      (.error == null) and (.agent_id | type == "string" and length > 0) and .status == "pending"
      and (.api_key | type == "string" and test("^hi_ak_[A-Za-z0-9_-]+$"))
    ' >/dev/null 2>&1 || { echo "hi_register_failed: invalid registration response; credentials unchanged" >&2; exit 1; }
    CRED_TMP=$(mktemp "$CREDS_DIR/.credentials.XXXXXX")
    printf '%s' "$REG" | jq -e --arg base "$HI_BASE" --slurpfile pending "$PENDING_FILE" '
      (.api_key | ltrimstr("hi_ak_") | gsub("-";"+") | gsub("_";"/") | @base64d | fromjson) as $key
      | if ($key.v != 1 or ($key.id | type != "string" or length == 0)
          or $key.secret != $pending[0].body.client_secret) then error("invalid key") else {
      client_id:          $key.id,
      client_secret:      $key.secret,
      agent_id:           .agent_id,
      status: .status,
      issuer:             $base,
      audience:           "hirey-hi",
      token_url:          ($base + "/oauth/token"),
      platform_base_url:  $base,
      access_token:           null,
      access_token_issued_at: 0,
      access_token_expires_in: 0
    } end' > "$CRED_TMP" 2>/dev/null || fail "hi_register_failed: invalid credential envelope; reconcile pending attempt"
    mv "$CRED_TMP" "$CREDS_FILE"
    CRED_TMP=""
    rm -f "$PENDING_FILE"
    ok "Anonymous agent registered: $(jq -r .agent_id "$CREDS_FILE")"
else
  # A crash after the credential rename may leave its matching private marker.
  # Clear only that exact completed request; preserve unrelated or legacy markers.
  if [ -f "$CREDS_DIR/.registration-pending.json" ] && [ ! -L "$CREDS_DIR/.registration-pending.json" ] \
    && jq -e --arg base "$HI_BASE" --slurpfile creds "$CREDS_FILE" '
      .version == 1 and .base == $base and .host == "claude"
      and .body.client_secret == $creds[0].client_secret
    ' "$CREDS_DIR/.registration-pending.json" >/dev/null 2>&1; then
    rm -f "$CREDS_DIR/.registration-pending.json"
  fi
  ok "Existing credentials at $CREDS_FILE — keeping agent_id=$(jq -r .agent_id "$CREDS_FILE")"
fi

# ─── 3. Mint or refresh access token (5-min skew) ────────────────────────
NOW=$(date +%s)
ISSUED_AT=$(jq '.access_token_issued_at // 0' "$CREDS_FILE")
EXPIRES_IN=$(jq '.access_token_expires_in // 0' "$CREDS_FILE")
EXP_AT=$(( ISSUED_AT + EXPIRES_IN - 300 ))

if [ "${HI_FORCE_TOKEN_REFRESH:-0}" = 1 ] || [ "$NOW" -ge "$EXP_AT" ]; then
  CID=$(jq -r .client_id "$CREDS_FILE")
  CSEC=$(jq -r .client_secret "$CREDS_FILE")
  AUD=$(jq -r .audience "$CREDS_FILE")
  TOK=$(curl -fsS --connect-timeout 5 --max-time 30 --retry 2 --retry-delay 1 --retry-max-time 40 -X POST "$HI_BASE/oauth/token" \
    --data-urlencode "grant_type=client_credentials" \
    --data-urlencode "client_id=$CID" --data-urlencode "client_secret=$CSEC" --data-urlencode "audience=$AUD") \
    || fail "Token endpoint unreachable"
  printf '%s' "$TOK" | jq -e '(.access_token | type == "string" and length > 0) and (.expires_in | type == "number" and . > 0)' >/dev/null 2>&1 \
    || fail "hi_token_refresh_failed: invalid token response; existing credentials preserved"
  CRED_TMP=$(mktemp "$CREDS_DIR/.credentials.XXXXXX")
  jq --argjson tok "$TOK" --arg now "$NOW" '
    .access_token            = $tok.access_token
    | .access_token_issued_at  = ($now | tonumber)
    | .access_token_expires_in = $tok.expires_in
  ' "$CREDS_FILE" > "$CRED_TMP"
  mv "$CRED_TMP" "$CREDS_FILE"
  CRED_TMP=""
  ok "Access token refreshed (expires in $(jq -r .access_token_expires_in "$CREDS_FILE")s)"
else
  ok "Cached token still valid"
fi

printf '%s\n' 'Installation credential ready; private work still requires verified identity.'
```

If any step exits non-zero or returns `error` JSON, report the failed step and safe error code, without printing raw responses or credentials, and stop. Common errors:

- `hi_registration_outcome_unknown` — the saved request failed. Preserve the pending marker and rerun the exact request after checking the service contract; legacy markers need manual reconciliation.
- `hi_register_failed` — malformed creation response or credential envelope. Preserve the pending fence and stop; do not print the raw response.
- `invalid_grant` from `/oauth/token` — the OAuth client was revoked/expired server-side. **Do NOT auto-delete `~/.config/hi/credentials.json`** — deleting it mints a brand-new agent and orphans the existing agent + any phone-bound workspace data. Surface the error to the user and let THEM decide: if a phone was bound, the workspace data is recoverable by re-binding the same phone on a fresh identity, so discarding creds is only safe with explicit user consent.
- `agent_disabled` or `agent_merged` from `/v1/agents/me` or a capability call — stop and surface the server recovery guidance. Do not create a replacement Agent.

## After setup: continue the user's request

After the user explicitly consents to Google binding and the binding operation returns
`status:"verified"`, rerun the guarded script above with `HI_FORCE_TOKEN_REFRESH=1`.
This reuses the existing credentials under the shared lock and obtains the active Agent
Session token even if the previous pending token was still fresh. Do not set local
`status:"active"` or assume a JWT-looking string proves identity. Then read
`GET /v1/agents/me` with the refreshed bearer and verify its flat `agent_id`, `person_id`,
`workspace_id` and `agent_session_id`; only that server response confirms active identity.
The local `status` originally returned by installation is historical, not an auth decision.
If refresh or the identity read fails, stop and report the actual error without re-registering.

A valid installation credential completes plugin setup. Do not require a profile, Listing,
or identity update to finish setup, and do not create these records automatically.

- Continue the user's original request. If no goal was supplied, ask what they want help with.
- A pending Agent may use only `people.find`, `people.explain`, and staged `capture.record`.
- For private Workspace work, messaging, meetings, or publication, guide the user through the
  supported Google, phone, or email binding first, then retry the requested operation.
- Create a Need or Listing only when the user's request calls for it. Read the current operation
  contract from `workspace_workflows` and obtain confirmation for actions that require it.
- Do not rely on legacy `onboarding_status`, `profile_ready`, or `listing_count` fields to
  decide whether setup is complete.

## What to tell the user

Never show the client_secret, access_token, or credential file. Report the actual installation
state and continue their request. For a pending installation, say that public people search is
ready and that private work will require identity verification when requested.

## Installation versus verified identity

Hi's `/v1/agents/api-keys` endpoint is **deliberately unauthenticated**. It returns a pending Agent and a `hi_ak_` envelope. Strictly decode its version-1 client credentials locally; do not retain or print a second copy of the API key. A private `.registration-pending.json` marker saves the exact registration request before transmission. An ambiguous result can retry that same request; a legacy marker without replay fields still requires manual reconciliation. A successful token exchange proves installation credentials; `/v1/agents/me` requires a verified active session and must not be used as the pending-install success gate. PKCE / browser-mediated OAuth would add user friction without adding security — there's no human identity to authenticate against (each install is its own anonymous agent), and the client_secret is generated locally, sent over TLS, and stored at user 600 perms locally.

This is the same identity model OpenClaw used (client_credentials baked into local state) — we just moved the storage from `~/.openclaw/hi-mcp/<profile>/` to `~/.config/hi/credentials.json` and dropped the local stdio MCP daemon. Every Hi tool call is a direct HTTPS POST to `https://hi.hirey.ai/v1/capabilities/<id>/call`.

## Reference (load on demand)

- Full REST API: see [../../reference/api.md](../../reference/api.md) for the complete endpoint list, capability schemas, and token lifecycle.
- Public Hi platform contract: `GET https://hi.hirey.ai/.well-known/hi-agent-platform.json` returns the canonical endpoint manifest.
