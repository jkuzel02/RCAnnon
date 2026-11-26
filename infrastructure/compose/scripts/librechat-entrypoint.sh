#!/bin/sh
set -eu

# Load runtime secrets from read-only Docker secret files before launching LibreChat.
# Secret values are never logged, echoed, or passed as command-line arguments.

read_secret_file() {
  var_name="$1"
  file_path="$2"
  exact_hex_len="${3:-}"

  if [ ! -f "$file_path" ] || [ ! -r "$file_path" ]; then
    printf 'FATAL: Required secret file for %s is missing or unreadable: %s\n' "$var_name" "$file_path" >&2
    exit 1
  fi

  line_count="$(awk 'END { print NR }' "$file_path")"
  if [ "$line_count" -ne 1 ]; then
    printf 'FATAL: Secret file for %s must contain a single non-empty line: %s\n' "$var_name" "$file_path" >&2
    exit 1
  fi

  value="$(tr -d '\r\n' < "$file_path")"
  if [ -z "$value" ]; then
    printf 'FATAL: Secret file for %s must not be empty: %s\n' "$var_name" "$file_path" >&2
    exit 1
  fi

  if [ -n "$exact_hex_len" ]; then
    actual_len="${#value}"
    if [ "$actual_len" -ne "$exact_hex_len" ]; then
      printf 'FATAL: Secret file for %s must be %s hex characters (got %s): %s\n' \
        "$var_name" "$exact_hex_len" "$actual_len" "$file_path" >&2
      exit 1
    fi
    case "$value" in
      (*[!0-9a-fA-F]*)
        printf 'FATAL: Secret file for %s must contain hexadecimal characters only: %s\n' \
          "$var_name" "$file_path" >&2
        exit 1
        ;;
    esac
  fi

  export "${var_name}=${value}"
}

read_secret_file "RCANNON_UI_BEARER_TOKEN" "${RCANNON_UI_BEARER_TOKEN_FILE:-/run/secrets/rcannon_ui_bearer_token}"
read_secret_file "JWT_SECRET" "${LIBRECHAT_JWT_SECRET_FILE:-/run/secrets/librechat_jwt_secret}"
read_secret_file "JWT_REFRESH_SECRET" "${LIBRECHAT_JWT_REFRESH_SECRET_FILE:-/run/secrets/librechat_jwt_refresh_secret}"
read_secret_file "CREDS_KEY" "${LIBRECHAT_CREDS_KEY_FILE:-/run/secrets/librechat_creds_key}" 64
read_secret_file "CREDS_IV" "${LIBRECHAT_CREDS_IV_FILE:-/run/secrets/librechat_creds_iv}" 32

if [ "$#" -gt 0 ]; then
  exec "$@"
fi

exec npm run backend
