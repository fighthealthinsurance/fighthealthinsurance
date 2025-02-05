#!/bin/bash
set -ex

SCRIPT_DIR="$(dirname "$0")"

BUILDX_CMD=${BUILDX_CMD:-push}

source "${SCRIPT_DIR}/setup_templates.sh"

FHI_VERSION=v0.10.3b
MYORG=${MYORG:-holdenk}
RAY_BASE=${RAY_BASE:-${MYORG}/fhi-ray}
FHI_BASE=${FHI_BASE:-${MYORG}/fhi-base}
FHI_DOCKER_USERNAME=${FHI_DOCKER_USERNAME:-holdenk}
FHI_DOCKER_EMAIL=${FHI_DOCKER_EMAIL:-"holden@pigscanfly.ca"}


export FHI_VERSION
export FHI_BASE
export RAY_BASE
export MYORG

# Build the django container first
source "${SCRIPT_DIR}/build_django.sh"

# Deploy a staging env
envsubst < k8s/deploy_staging.yaml | kubectl apply -f -
read -rp "Have you checked staging and are ready to deploy to prod? (y/n) " yn

case $yn in
    [Yy]* ) echo "Proceeding...";;
    [Nn]* ) echo "Exiting..."; exit;;
    * ) echo "Invalid response. Please enter y or n.";;
esac

# Build and deploy ray before the "main" app comes up.
source "${SCRIPT_DIR}/build_ray.sh"
# The raycluster operator doesn't handle upgrades well so delete + recreate instead.
kubectl delete raycluster -n totallylegitco raycluster-kuberay || echo "No raycluster present"
envsubst < k8s/ray/cluster.yaml | kubectl apply -f -

# Deploy a staging env
envsubst < k8s/deploy.yaml | kubectl apply -f -
