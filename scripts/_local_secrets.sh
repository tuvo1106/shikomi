# Sourced by prod-up.sh / k8s-up.sh: export stable secrets for local prod-shaped runs.
#
# ENV=prod refuses the public dev defaults, so the local prod-shaped stacks need real
# ones for JWT_SECRET (signs access tokens) and TOTP_ENCRYPTION_KEY (encrypts two-factor
# secrets at rest). Each is generated once into a gitignored file and reused: a fresh
# JWT_SECRET per run would swap the signing key without the rotation path
# (JWT_PREVIOUS_SECRETS) and 401 every live session, and a fresh TOTP key per run would
# make every enrolled user's stored 2FA secret undecryptable. Export either variable
# yourself to override; delete its file to force a new one.
_local_secret() {  # _local_secret VAR FILE GENERATOR...
  local var="$1" file="$2"; shift 2
  if [ -z "${!var:-}" ]; then
    if [ ! -s "$file" ]; then
      (umask 077; "$@" > "$file")
    fi
    export "$var=$(cat "$file")"
  fi
  export "${var?}"
}

JWT_SECRET_FILE="${JWT_SECRET_FILE:-.jwt-secret.local}"
TOTP_KEY_FILE="${TOTP_KEY_FILE:-.totp-key.local}"
_local_secret JWT_SECRET "$JWT_SECRET_FILE" openssl rand -hex 32
# A Fernet key is urlsafe-base64 of 32 random bytes (no `cryptography` needed on the host).
_local_secret TOTP_ENCRYPTION_KEY "$TOTP_KEY_FILE" \
  python3 -c 'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())'
