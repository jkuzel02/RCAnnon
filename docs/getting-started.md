# Getting Started with RCAnnon

Try RCAnnon in your browser with Docker Compose. You can start without Kubernetes or an API key.

## Try the web app

From a fresh clone with Docker running, start the published image in mock mode.

<!-- x-release-please-start-version -->
```sh
git clone https://github.com/jkuzel02/RCAnnon.git
cd RCAnnon
export RCANNON_IMAGE=ghcr.io/jkuzel02/rcannon:0.1.7
export RCANNON_PROVIDER_MODE=mock
docker compose -f compose.yaml up -d --no-build --wait
```
<!-- x-release-please-end -->

Open [RCAnnon](http://127.0.0.1:3080) and sign in with email `admin@rcannon.local` and password `rcannon-local-admin`. Select `rcannon-optimal` and send this request.

```text
/rca context=lab namespace=demo -- Review the foo-app workload
```

**Insufficient Evidence** confirms the request reached RCAnnon. Connect the lab below to see a report with Kubernetes data.

To stop here and keep your chat history, run `docker compose -f compose.yaml down`.

## Try a report with Kubernetes data

Install `kind`, `kubectl`, and `helm` using the [local lab guide](../infrastructure/k8s/README.md). From the same terminal, run these commands.

```sh
bash infrastructure/k8s/bootstrap.sh
docker compose up -d --no-build --wait
kubectl --context kind-local -n demo get pods
```

The bootstrap script connects RCAnnon to the lab with read-only access. Once the `foo-app` pods appear, resend the request in the web app. RCAnnon collects real lab evidence and returns a sample report using mock inference.

When finished, run `docker compose down` and follow the [lab cleanup steps](../infrastructure/k8s/README.md#clean-up).

## Use your own setup

- [Connect your own Kubernetes cluster](../infrastructure/compose/README.md#mounting-read-only-target-kubeconfig-and-ca-files)
- [Enable an OpenAI-compatible model provider](../infrastructure/compose/README.md#enabling-live-openai-compatible-api-inference)
- [Deploy the container in a management cluster](container-image.md#in-a-separate-management-kubernetes-cluster)
- [Build from source and run tests](../CONTRIBUTING.md#local-development-quick-start)
