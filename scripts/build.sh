#!/bin/bash
set -ex

SCRIPT_DIR="$(dirname "$0")"

BUILDX_CMD=${BUILDX_CMD:-push}

source "${SCRIPT_DIR}/setup_templates.sh"

FHI_VERSION=v0.9.8a

export FHI_VERSION


# Build ray cluster first so that the cluster can come up before the job that registers the workers
source "${SCRIPT_DIR}/build_ray.sh"
# The raycluster operator doesn't handle upgrades well so delete + recreate instead.
kubectl delete raycluster -n totallylegitco raycluster-kuberay || echo "No raycluster present"
envsubst < k8s/ray/cluster.yaml | kubectl apply -f -

# Build the django container
source "${SCRIPT_DIR}/build_django.sh"
envsubst < k8s/deploy.yaml | kubectl apply -f -
