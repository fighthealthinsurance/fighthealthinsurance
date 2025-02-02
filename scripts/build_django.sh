#!/bin/bash
set -ex

pwd

BUILDX_CMD=${BUILDX_CMD:-"push"}
PLATFORM=${PLATFORM:-linux/amd64,linux/arm64}
RELEASE=${FHI_VERSION:-$IMAGE}

# Build the web app
IMAGE=holdenk/fight-health-insurance:${FHI_VERSION}
(docker manifest inspect "${IMAGE}" && sleep 1) || docker buildx build --platform="${PLATFORM}" -t "${IMAGE}" -f k8s/Dockerfile --build-arg RELEASE="${RELEASE}" "--${BUILDX_CMD}" .
