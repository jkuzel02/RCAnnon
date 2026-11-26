# Getting Started with RCAnnon

Two commands get you from a fresh clone to a running Kubernetes lab and the RCAnnon web UI.

> **Using your own cluster?** Skip Step 1 and point RCAnnon at a read-only kubeconfig later with [Compose Operations](../infrastructure/compose/README.md).

## 1. Spin Up a Local Kubernetes Cluster (Optional)

Need a cluster to inspect right away? Provision the built-in disposable **Kind + Flux + Prometheus + Grafana** lab (one easy way to get started; requires `docker`, `kind`, `kubectl`, and `helm`—see [OS setup](../infrastructure/k8s/README.md)):

```sh
bash infrastructure/k8s/bootstrap.sh
```

This also applies read-only RBAC and auto-generates `infrastructure/compose/kubeconfig/config` + `compose.override.yaml` so `docker compose up -d` connects straight to `kind-local` without manual kubeconfig edits.

## 2. Configure & Start RCAnnon

Copy `.env.example` to `.env`, paste your `OPENAI_API_KEY` (or leave it empty to try offline mock mode), and start Docker Compose:

```sh
cp .env.example .env
docker compose up -d
```

Open **`http://127.0.0.1:3080`**:
- **Email:** `admin@rcannon.local`
- **Password:** `rcannon-local-admin` (or whatever you set in `.env`)
- Pick a model (`rcannon-base`, `rcannon-optimal`, or `rcannon-mas`) and submit an `/rca` prompt:

```text
/rca context=lab namespace=demo -- foo-app is failing
```

> `docker compose up -d` generates isolated per-service tokens and seeds the local UI account automatically—no extra setup scripts or manual bootstrap steps needed.

---

## What You Just Spun Up

### In Kubernetes (`kind-local`)

If you ran `bootstrap.sh`, Flux CD reconciles the following into your local `kind-local` cluster:

| Component | Purpose | Access / Check |
| --- | --- | --- |
| **Demo apps (`demo` namespace)** | Sample workloads (`foo-app`, `bar-app`) | `https://foo.localhost` / `https://bar.localhost` |
| **Grafana & Prometheus (`monitoring`)** | Cluster metrics & dashboards (user: `admin`) | `https://grafana.localhost` |
| **Flux CD (`flux-system`)** | GitOps reconciliation UI & controllers | `https://flux.localhost` |
| **Traefik & cert-manager** | Local HTTPS Gateway for `*.localhost` | `kubectl --context kind-local get pods -A` |

Delete the lab cluster anytime with `kind delete cluster --name local && rm -f compose.override.yaml infrastructure/compose/kubeconfig/config`.

### In Docker (`docker compose`)

`docker compose up -d` starts three long-running services plus two automatic one-shot setup containers:

| Container | Role | Port / Exposure |
| --- | --- | --- |
| `ui` (LibreChat `v0.8.9`) | Local login & chat UI | `http://127.0.0.1:3080` (loopback only) |
| `api` (RCAnnon FastAPI) | Read-only K8s collector, evidence hashing, 3 LangGraph workflows | Internal Docker network only |
| `mongodb` (MongoDB `8.0`) | User accounts & chat history (`rcannon_mongo_data`) | Internal Docker network only |
| `secrets-init` / `ui-bootstrap` | One-shot setup creating per-service tokens & admin login (exits `0`) | `network_mode: none` / internal DB only |

Stop the stack anytime with `docker compose down` (add `-v` to wipe persisted volumes).

---

## Choosing a Workflow Model

| Model Alias | Strategy | Calls | Output |
| --- | --- | --- | --- |
| `rcannon-base` | Single-agent free-form | 1 | Narrative Markdown |
| `rcannon-optimal` | Structured single-agent | 1 | 5-heading RCA report |
| `rcannon-mas` | 3 parallel specialists -> coordinator | 4 | 5-heading RCA + claim cross-checks |

## Troubleshooting

- **`Live Kubernetes collector is not configured.`**: No cluster kubeconfig is wired into `api`. Run `bash infrastructure/k8s/bootstrap.sh` (for local Kind) or mount your own kubeconfig via `compose.kube.yaml`, then run `docker compose up -d`.
- **`api_unreachable`**: Your mounted kubeconfig `server:` points to `127.0.0.1` (unreachable inside containers) or `api` is not on the cluster's Docker network. For local Kind, re-run `bash infrastructure/k8s/bootstrap.sh && docker compose up -d`—it configures `https://local-control-plane:6443` on the `kind` network automatically.

## Next Steps

| Need | Where to Go |
| --- | --- |
| Connect `api` to your Kind or remote cluster (`compose.kube.yaml`) | [Compose Operations](../infrastructure/compose/README.md) |
| Run with prebuilt GHCR images (`0.1.5`, `latest`) or inside a management K8s cluster | [Container Image](../docs/container-image.md) | <!-- x-release-please-version -->
| Understand trust boundaries, redaction, and `EvidenceSnapshot/v1` contracts | [Architecture](../docs/architecture.md) |
| Build from source, run offline tests, or contribute | [Contributing](../CONTRIBUTING.md) |
