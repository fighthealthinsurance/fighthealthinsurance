#!/bin/bash
set -ex

SCRIPT_DIR="$(dirname "$0")"

BUILDX_CMD=${BUILDX_CMD:-push}

# --no-build: assume the container images are already built and pushed, and only
# run the kubectl deploy steps. This skips every build step -- template/static
# setup (mypy, migrations, collectstatic, npm build) and the image builds -- so
# the script can run on a deploy-only host or CI job that has just kubectl and
# envsubst (no Python/Node build toolchain).
NO_BUILD=false
for arg in "$@"; do
    case "$arg" in
        --no-build)
            NO_BUILD=true
            ;;
        -h|--help)
            echo "Usage: $0 [--no-build]"
            echo "  --no-build  Skip all build steps (template/static setup and image builds);"
            echo "              assume images are already built and pushed, and only deploy."
            exit 0
            ;;
        *)
            echo "Unknown argument: $arg" >&2
            echo "Usage: $0 [--no-build]" >&2
            exit 1
            ;;
    esac
done

# Prepare build artifacts (mypy, migrations check, collectstatic, npm build).
# These are only needed to build the images, so skip them with --no-build; a
# deploy-only host may not have the Python/Node build toolchain installed.
if [ "$NO_BUILD" = true ]; then
    echo "--no-build: skipping template/static setup (setup_templates.sh)"
else
    source "${SCRIPT_DIR}/setup_templates.sh"
fi

# BUILDKIT_NO_CLIENT_TOKEN=true
FHI_VERSION=v0.18.2a


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

# Build the dev containers (skipped with --no-build; assumes image already exists)
if [ "$NO_BUILD" = true ]; then
    echo "--no-build: skipping django image build (${FHI_BASE}:${FHI_VERSION})"
else
    source "${SCRIPT_DIR}/build_django.sh"
fi

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
# (skipped with --no-build; assumes image already exists)
if [ "$NO_BUILD" = true ]; then
    echo "--no-build: skipping ray image build (${RAY_BASE}:${FHI_VERSION})"
else
    source "${SCRIPT_DIR}/build_ray.sh"
fi

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
