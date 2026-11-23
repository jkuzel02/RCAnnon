#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
secrets_dir="$(cd -- "${script_dir}/../secrets" && pwd)"

mkdir -p "${secrets_dir}"
chmod 0750 "${secrets_dir}"

generate_secret_file() {
  local target_file="$1"
  if [[ -s "${target_file}" ]]; then
    printf 'Keeping existing %s\n' "${target_file##*/}"
    return 0
  fi

  local tmp_file
  tmp_file="$(mktemp "${secrets_dir}/.tmp.XXXXXX")"
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32 > "${tmp_file}"
  else
    od -An -tx1 -N32 /dev/urandom | tr -d ' \n' > "${tmp_file}"
    printf '\n' >> "${tmp_file}"
  fi
  chmod 0644 "${tmp_file}"
  mv "${tmp_file}" "${target_file}"
  printf 'Created %s\n' "${target_file##*/}"
}

generate_secret_file "${secrets_dir}/ui_bearer_token.secret"
generate_secret_file "${secrets_dir}/operator_bearer_token.secret"
