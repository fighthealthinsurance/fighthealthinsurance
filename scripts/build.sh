#!/bin/bash
set -ex

SCRIPT_DIR="$(dirname "$0")"

BUILDX_CMD=${BUILDX_CMD:-push}

source "${SCRIPT_DIR}/setup_templates.sh"

# BUILDKIT_NO_CLIENT_TOKEN=true
FHI_VERSION=v0.12.11f

MYORG=${MYORG:-totallylegitco}
RAY_BASE=${RAY_BASE:-${MYORG}/fhi-ray}
FHI_BASE=${FHI_BASE:-${MYORG}/fhi-base}
FHI_DOCKER_USERNAME=${FHI_DOCKER_USERNAME:-holdenk}
FHI_DOCKER_EMAIL=${FHI_DOCKER_EMAIL:-"holden@pigscanfly.ca"}

export BUILDKIT_NO_CLIENT_TOKEN
export FHI_VERSION
export FHI_BASE
export RAY_BASE
export MYORG

# Build the django dev container first
FHI_VERSION_OG=${FHI_VERSION}
FHI_VERSION=${FHI_VERSION}-dev
export FHI_VERSION

# Build the dev containers
source "${SCRIPT_DIR}/build_django.sh"

# Deploy dev
envsubst < k8s/deploy_dev.yaml | kubectl delete -f - || echo "No existing dev deployment present"
envsubst < k8s/deploy_dev.yaml | kubectl apply -f -
read -rp "Have you checked dev and are ready to deploy to staging? (y/n) " yn
case $yn in
    [Yy]* ) echo "Proceeding...";;
    [Nn]* ) echo "Exiting..."; exit;;
    * ) echo "Invalid response. Please enter y or n.";;
esac

# Reset to non-dev
FHI_VERSION_OG=${FHI_VERSION}
FHI_VERSION=${FHI_VERSION_OG}
export FHI_VERSION

# Build the ray container -- we don't use it in staging *BUT*
# better to have built than be stuck with a half deployed system if
# dockerhub is having a day.
source "${SCRIPT_DIR}/build_ray.sh"

# Deploy a staging env
envsubst < k8s/deploy_staging.yaml | kubectl apply -f -
read -rp "Have you checked staging and are ready to deploy to prod? (y/n) " yn

case $yn in
    [Yy]* ) echo "Proceeding...";;
    [Nn]* ) echo "Exiting..."; exit;;
    * ) echo "Invalid response. Please enter y or n.";;
esac

# The raycluster operator doesn't handle upgrades well so delete + recreate instead.
kubectl delete raycluster -n totallylegitco raycluster-kuberay || echo "No raycluster present"
envsubst < k8s/ray/cluster.yaml | kubectl apply -f -

# Deploy a staging env
envsubst < k8s/deploy.yaml | kubectl apply -f -
