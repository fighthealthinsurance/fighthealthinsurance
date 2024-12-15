#!/bin/bash
set -ex

BUILDX_CMD=${BUILDX_CMD:-"--push"}
PLATFORM=${PLATFORM:-linux/amd64,linux/arm64}

# Build the web app
IMAGE=holdenk/fight-health-insurance:${FHI_VERSION}
(docker pull "${IMAGE}" && sleep 10) || docker buildx build --platform="${PLATFORM}" -t "${IMAGE}" -f k8s/Dockerfile "${BUILDX_CMD}" .
