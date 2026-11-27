#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

export RCANNON_DEMO_PORT="${RCANNON_DEMO_PORT:-8000}"
compose=(docker compose --project-name rcannon-demo --env-file /dev/null
  -f compose.yaml -f infrastructure/compose/compose.demo.yaml)

case "${1:-up}" in
  up)
    "${compose[@]}" up --detach --wait --wait-timeout 180
    snap_id="$("${compose[@]}" exec -T demo python /demo/incident.py --seed)"
    base_url="http://127.0.0.1:${RCANNON_DEMO_PORT}"
    cat <<EOF

RCAnnon API demo is running at ${base_url} (snapshot: ${snap_id})

Try these cURL commands:

  # 1. Check API health and load generated bearer tokens
  curl -fsS ${base_url}/healthz
  UI_TOKEN=\$(docker compose --project-name rcannon-demo exec -T rcannon cat /run/rcannon-secrets/rcannon_ui_bearer_token)
  OP_TOKEN=\$(docker compose --project-name rcannon-demo exec -T rcannon cat /run/rcannon-secrets/rcannon_operator_bearer_token)

  # 2. List OpenAI-compatible workflow aliases
  curl -fsS ${base_url}/v1/models \\
    -H "Authorization: Bearer \${UI_TOKEN}"

  # 3. Run an OpenAI-compatible RCA completion against the seeded snapshot
  curl -fsS ${base_url}/v1/chat/completions \\
    -H "Authorization: Bearer \${UI_TOKEN}" \\
    -H "Content-Type: application/json" \\
    -d '{"model":"rcannon-optimal","messages":[{"role":"user","content":"/rca snapshot=${snap_id}"}]}'

  # 4. Run the typed operator API directly (e.g. mas workflow)
  curl -fsS ${base_url}/api/v1/runs \\
    -H "Authorization: Bearer \${OP_TOKEN}" \\
    -H "Content-Type: application/json" \\
    -d '{"variant":"mas","snapshot_id":"${snap_id}"}'

Stop the demo with: bash try-rcannon.sh down
EOF
    ;;
  down)
    "${compose[@]}" down
    ;;
  *)
    printf 'Usage: bash try-rcannon.sh [up|down]\n' >&2
    exit 2
    ;;
esac
