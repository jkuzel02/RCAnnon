#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

export RCANNON_UI_PORT="${RCANNON_DEMO_PORT:-3081}"
compose=(docker compose --project-name rcannon-demo --profile librechat --env-file /dev/null
  -f compose.yaml -f infrastructure/compose/compose.demo.yaml)

case "${1:-up}" in
  up)
    "${compose[@]}" up --detach --wait --wait-timeout 180
    prompt="$("${compose[@]}" exec -T demo node /demo/incident.mjs --seed)"
    printf '\nOpen http://127.0.0.1:%s\nEmail: admin@rcannon.local\nPassword: rcannon-local-admin\n\nSelect rcannon-optimal and paste:\n%s\n\nStop the demo with: bash try-rcannon.sh down\n' \
      "$RCANNON_UI_PORT" "$prompt"
    ;;
  down)
    "${compose[@]}" down
    ;;
  *)
    printf 'Usage: bash try-rcannon.sh [up|down]\n' >&2
    exit 2
    ;;
esac
