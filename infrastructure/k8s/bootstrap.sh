#!/usr/bin/env bash
set -euo pipefail
# Resolve paths from this script, regardless of the caller's working directory
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

# Target the local cluster explicitly for all Helm and kubectl operations
context="kind-local"

# Reuse the local kind cluster if it exists; otherwise create it
clusters="$(kind get clusters)"
if grep -Fxq 'local' <<< "$clusters"; then
  printf 'Reusing existing kind cluster local.\n'
else
  kind create cluster --config kind/local.yaml
fi

# Install Flux Operator
helm upgrade --install flux-operator \
  oci://ghcr.io/controlplaneio-fluxcd/charts/flux-operator \
  --kube-context "$context" \
  --version 0.61.0 \
  --namespace flux-system \
  --create-namespace \
  --wait

# Install the FluxInstance resource; check operator-managed readiness below
# Omit Helm's --wait so its custom-resource watcher does not own this check
helm upgrade --install flux-instance \
  oci://ghcr.io/controlplaneio-fluxcd/charts/flux-instance \
  --kube-context "$context" \
  --version 0.61.0 \
  --namespace flux-system \
  --set instance.cluster.size=small

# Wait for the operator to finish installing the Flux controllers
kubectl --context "$context" -n flux-system wait \
  --for=condition=Ready fluxinstance/flux \
  --timeout=3m

# Apply the Git source and root reconciliation for Flux CD
kubectl --context "$context" apply -f flux/
