#!/bin/sh
set -eu

# Idempotent one-shot bootstrap for the initial LibreChat local administrator.
# Passes the admin password exclusively over stdin to stock `npm run create-user`,
# never on argv, stdout, stderr, or environment variables visible in process args.

fail_closed() {
  printf 'ERROR: %s\n' "$1" >&2
  exit 1
}

print_recovery_guidance() {
  cat >&2 <<'EOF'
Interactive upstream user recovery commands (run with UI + MongoDB running):
  docker compose exec -it librechat npm run list-users
  docker compose exec -it librechat npm run reset-password
  docker compose exec -it librechat npm run delete-user
  docker compose exec -it librechat npm run create-user
EOF
}

read_secret_file() {
  var_name="$1"
  file_path="$2"
  exact_hex_len="${3:-}"

  if [ ! -f "$file_path" ] || [ ! -r "$file_path" ]; then
    fail_closed "Required secret file for ${var_name} is missing or unreadable: ${file_path}"
  fi

  line_count="$(awk 'END { print NR }' "$file_path")"
  if [ "$line_count" -ne 1 ]; then
    fail_closed "Secret file for ${var_name} must contain a single non-empty line: ${file_path}"
  fi

  value="$(tr -d '\r\n' < "$file_path")"
  if [ -z "$value" ]; then
    fail_closed "Secret file for ${var_name} must not be empty: ${file_path}"
  fi

  if [ -n "$exact_hex_len" ]; then
    actual_len="${#value}"
    if [ "$actual_len" -ne "$exact_hex_len" ]; then
      fail_closed "Secret file for ${var_name} must be ${exact_hex_len} hex characters (got ${actual_len}): ${file_path}"
    fi
    case "$value" in
      (*[!0-9a-fA-F]*)
        fail_closed "Secret file for ${var_name} must contain hexadecimal characters only: ${file_path}"
        ;;
    esac
  fi

  export "${var_name}=${value}"
}

ADMIN_EMAIL="${LIBRECHAT_ADMIN_EMAIL:-}"
ADMIN_NAME="${LIBRECHAT_ADMIN_NAME:-}"
ADMIN_USERNAME="${LIBRECHAT_ADMIN_USERNAME:-}"
MONGO_URI="${MONGO_URI:-}"
APP_DIR="${LIBRECHAT_APP_DIR:-/app}"
PW_FILE="${LIBRECHAT_ADMIN_PASSWORD_FILE:-/run/secrets/librechat_admin_password}"

[ -n "$MONGO_URI" ] || fail_closed "MONGO_URI must be set."
[ -n "$ADMIN_EMAIL" ] || fail_closed "LIBRECHAT_ADMIN_EMAIL must be set."
[ -n "$ADMIN_NAME" ] || fail_closed "LIBRECHAT_ADMIN_NAME must be set."
[ -n "$ADMIN_USERNAME" ] || fail_closed "LIBRECHAT_ADMIN_USERNAME must be set."

case "$ADMIN_EMAIL" in
  (*[[:space:]]*) fail_closed "LIBRECHAT_ADMIN_EMAIL must not contain whitespace." ;;
  (*@*) ;;
  (*) fail_closed "LIBRECHAT_ADMIN_EMAIL must be a valid email address." ;;
esac

case "$ADMIN_USERNAME" in
  (*[!A-Za-z0-9._-]*)
    fail_closed "LIBRECHAT_ADMIN_USERNAME must use only [A-Za-z0-9._-]."
    ;;
esac

if [ ! -f "$PW_FILE" ] || [ ! -r "$PW_FILE" ]; then
  fail_closed "Admin bootstrap password file is missing or unreadable: ${PW_FILE}"
fi

pw_lines="$(awk 'END { print NR }' "$PW_FILE")"
if [ "$pw_lines" -ne 1 ]; then
  fail_closed "Admin bootstrap password file must contain a single line: ${PW_FILE}"
fi

pw_len="$(tr -d '\r\n' < "$PW_FILE" | wc -c | tr -d '[:space:]')"
if [ "$pw_len" -lt 12 ]; then
  fail_closed "Admin bootstrap password must be at least 12 characters (got ${pw_len})."
fi

# AuthService imported by create-user expects LibreChat crypto/JWT env vars.
read_secret_file "JWT_SECRET" "${LIBRECHAT_JWT_SECRET_FILE:-/run/secrets/librechat_jwt_secret}"
read_secret_file "JWT_REFRESH_SECRET" "${LIBRECHAT_JWT_REFRESH_SECRET_FILE:-/run/secrets/librechat_jwt_refresh_secret}"
read_secret_file "CREDS_KEY" "${LIBRECHAT_CREDS_KEY_FILE:-/run/secrets/librechat_creds_key}" 64
read_secret_file "CREDS_IV" "${LIBRECHAT_CREDS_IV_FILE:-/run/secrets/librechat_creds_iv}" 32

inspect_users() {
  (
    cd "$APP_DIR"
    RCANNON_TARGET_EMAIL="$ADMIN_EMAIL" RCANNON_TARGET_USERNAME="$ADMIN_USERNAME" node -e '
const mongoose = require("mongoose");

(async () => {
  const email = String(process.env.RCANNON_TARGET_EMAIL || "").trim().toLowerCase();
  const username = String(process.env.RCANNON_TARGET_USERNAME || "").trim();
  await mongoose.connect(process.env.MONGO_URI, { autoIndex: false });
  const users = mongoose.connection.db.collection("users");

  const totalUsers = await users.countDocuments({});
  const adminCount = await users.countDocuments({ role: "ADMIN" });
  const projection = { email: 1, username: 1, role: 1, emailVerified: 1 };
  const byEmail = await users.findOne({ email }, { projection });
  const byUsername = await users.findOne({ username }, { projection });

  const summary = {
    totalUsers,
    adminCount,
    emailExists: Boolean(byEmail),
    usernameExists: Boolean(byUsername),
    sameRecord: Boolean(byEmail && byUsername && String(byEmail._id) === String(byUsername._id)),
    emailRole: byEmail ? byEmail.role : null,
    usernameRole: byUsername ? byUsername.role : null,
    emailVerified: byEmail ? Boolean(byEmail.emailVerified) : false
  };
  await mongoose.disconnect();
  process.stdout.write("__RCANNON_STATE__=" + JSON.stringify(summary) + "\n");
})().catch(async (err) => {
  console.error(err && err.message ? err.message : String(err));
  try { await mongoose.disconnect(); } catch (_) {}
  process.exit(2);
});
'
  ) | sed -n 's/^__RCANNON_STATE__=//p' | tail -n 1
}

status_json="$(inspect_users)"
[ -n "$status_json" ] || fail_closed "Failed to inspect MongoDB user state."

evaluate_state() {
  printf '%s' "$1" | (
    cd "$APP_DIR"
    node -e '
const fs = require("fs");
const mode = process.argv[1];
const raw = fs.readFileSync(0, "utf8").trim();
if (!raw) process.exit(2);
const state = JSON.parse(raw);

if (mode === "before") {
  if (state.totalUsers === 0) {
    process.stdout.write("CREATE\n");
    process.exit(0);
  }
  if (
    state.emailExists &&
    state.usernameExists &&
    state.sameRecord &&
    state.emailRole === "ADMIN" &&
    state.emailVerified === true
  ) {
    process.stdout.write("IDEMPOTENT\n");
    process.exit(0);
  }
  process.stdout.write("CONFLICT\n");
  process.exit(0);
}

if (mode === "after") {
  const ok =
    state.totalUsers === 1 &&
    state.adminCount === 1 &&
    state.emailExists &&
    state.usernameExists &&
    state.sameRecord &&
    state.emailRole === "ADMIN" &&
    state.emailVerified === true;
  process.exit(ok ? 0 : 1);
}
process.exit(2);
' "$2"
  )
}

action="$(evaluate_state "$status_json" "before")"

case "$action" in
  IDEMPOTENT)
    printf 'Admin bootstrap already satisfied (existing ADMIN account verified); no changes made.\n'
    exit 0
    ;;
  CONFLICT)
    printf 'ERROR: Existing MongoDB user state conflicts with configured admin identity (%s / %s): %s\n' \
      "$ADMIN_EMAIL" "$ADMIN_USERNAME" "$status_json" >&2
    print_recovery_guidance
    exit 1
    ;;
  CREATE)
    (
      cd "$APP_DIR"
      { tr -d '\r\n' < "$PW_FILE"; printf '\n'; } | \
        npm run create-user -- "$ADMIN_EMAIL" "$ADMIN_NAME" "$ADMIN_USERNAME" --email-verified=true
    )
    after_json="$(inspect_users)"
    if ! evaluate_state "$after_json" "after"; then
      printf 'ERROR: Post-bootstrap verification failed (expected 1 verified ADMIN user): %s\n' "$after_json" >&2
      print_recovery_guidance
      exit 1
    fi
    printf 'Admin bootstrap complete: created verified ADMIN user (%s / %s).\n' "$ADMIN_EMAIL" "$ADMIN_USERNAME"
    exit 0
    ;;
  *)
    fail_closed "Unexpected bootstrap evaluation state."
    ;;
esac
