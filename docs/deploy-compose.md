# Deploy RCAnnon with Docker Compose

Use this guide after the [browser demo](getting-started.md) when you want to analyze your own Kubernetes evidence. You need a Kubernetes API reachable from Docker, a read-only kubeconfig for the target, and an OpenAI-compatible API key. Run the commands from the repository root.

## 1. Connect the Kubernetes cluster

Place the read-only kubeconfig at `infrastructure/compose/kubeconfig/config`. Keep any referenced CA or client certificate files in the same directory, or embed them in the kubeconfig. The container runs as UID `10001`. Make the files readable to that user. See [kubeconfig mount requirements](../infrastructure/compose/README.md#mounting-read-only-target-kubeconfig-and-ca-files).

Create a root `.env` with these values from your cluster and model provider. Replace `my-context` and `my-namespace` with your kubeconfig context and target namespace. `target` is the name you will use in chat.

```dotenv
RCANNON_KUBE_CONTEXT=my-context
RCANNON_TARGET_ALIAS=target
RCANNON_ALLOWED_SCOPES={"target":["my-namespace"]}
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4o-mini
```

## 2. Add your model API key and start

Save the key as the only line in `infrastructure/compose/secrets/llm_api_key.secret`. The mounted file must be readable to UID `10001`. Set `LIBRECHAT_ADMIN_PASSWORD` in `.env` before the first start to choose a password for the management UI.

```sh
docker compose --profile librechat -f compose.yaml \
  -f infrastructure/compose/compose.kube.yaml \
  -f infrastructure/compose/compose.openai.yaml \
  up --detach --wait
```

Open [RCAnnon](http://127.0.0.1:3080), sign in as `admin@rcannon.local` with the password you set, and select **rcannon-optimal**. Start a chat with your alias and namespace.

```text
/rca context=target namespace=my-namespace -- The API workload is restarting
```

### If you use the bundled Kind lab

[Bootstrap the lab](../infrastructure/k8s/README.md#bootstrap) first. It generates the kubeconfig and `compose.override.yaml`. Use that override in place of `infrastructure/compose/compose.kube.yaml` and set `RCANNON_ALLOWED_SCOPES={"lab":["demo"]}` in `.env`. Send the request with `context=lab namespace=demo`.

### If startup or diagnosis fails

- **LibreChat won't start.** Run `docker compose ps -a` and check `docker compose logs librechat-bootstrap`. A first start creates the local administrator. An existing installation keeps its account and password.
- **Insufficient Evidence.** Verify your target alias and namespace in `.env`. Check [Docker reachability to the Kubernetes API](../infrastructure/compose/README.md#linux-and-macos-reachability-to-kind-and-remote-clusters), especially if the kubeconfig uses a loopback address.
- **Provider unavailable.** Confirm that your provider supports Chat Completions and lists the exact model ID you supplied. See [live model configuration](../infrastructure/compose/README.md#enabling-live-openai-compatible-api-inference).

Use the same Compose files with `down` to stop the stack and keep its data.
