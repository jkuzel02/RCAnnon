# Deploy with Docker Compose

Run the **RCAnnon API** with Docker Compose using two commands: optionally spin up a local Kubernetes target cluster, then start the API container.

## 1. Optional: Spin Up a Local Kind Target Cluster

```sh
bash infrastructure/k8s/bootstrap.sh
```

This creates a local `kind` cluster (`lab`, namespace `demo`) with failing sample workloads and exports a scoped read-only kubeconfig for RCAnnon. See [Kubernetes Lab](../infrastructure/k8s/README.md) for details.

## 2. Configure Your OpenAI API Key

```sh
cp .env.example .env
# Edit .env and set OPENAI_API_KEY=...
```

## 3. Start the RCAnnon API

```sh
docker compose up --detach --wait
```

Compose automatically initializes API bearer tokens (`secrets-init`) and exposes `rcannon` on loopback `http://127.0.0.1:8000` (override with `RCANNON_PORT` in `.env`).

## 4. Call the API with `curl`

Read the generated bearer tokens from the running container:

```sh
UI_TOKEN=$(docker compose exec -T rcannon cat /run/rcannon-secrets/rcannon_ui_bearer_token)
OP_TOKEN=$(docker compose exec -T rcannon cat /run/rcannon-secrets/rcannon_operator_bearer_token)
```

Verify health, list models, run an OpenAI-compatible investigation, and inspect recorded runs:

```sh
curl -fsS http://127.0.0.1:8000/healthz

curl -fsS \
  -H "Authorization: Bearer $UI_TOKEN" \
  http://127.0.0.1:8000/v1/models

curl -fsS http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $UI_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "rcannon-optimal",
    "messages": [
      {"role": "user", "content": "/rca context=lab namespace=demo -- checkout-api is failing"}
    ]
  }'

curl -fsS \
  -H "Authorization: Bearer $OP_TOKEN" \
  http://127.0.0.1:8000/api/v1/runs
```

If you connect your own frontend or OpenAI-compatible client, point its custom endpoint URL to `http://127.0.0.1:8000/v1` (or `http://rcannon:8000/v1` on a shared Docker network) with `$UI_TOKEN` as the API key.

## 5. Stop the Stack

```sh
docker compose down
```

For production overrides, custom builds, secret rotation, and Kubernetes mounts, see [Compose Operations](../infrastructure/compose/README.md).
