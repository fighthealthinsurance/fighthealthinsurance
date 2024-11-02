#!/bin/bash
set -ex

# Ray doesn't publish combiend aarch64 & amd64 images because idk.
RAY_VERSION=2.38.0-py311
RAY_IMAGE=holdenk/ray:${RAY_VERSION}
export RAY_VERSION
docker pull "${RAY_IMAGE}" || docker buildx build --platform=linux/amd64,linux/arm64 -t "${RAY_IMAGE}" . -f RayDockerfile --push --build-arg RAY_VERSION=${RAY_VERSION}
# Using the amd64/arm64 ray container as a base put together a container with the FHI code and libs in it.
COMBINED_IMAGE=holdenk/fhi-ray:${FHI_VERSION}
docker pull "${COMBINED_IMAGE}" || docker buildx build --platform=linux/amd64,linux/arm64 -t "${COMBINED_IMAGE}" . -f CombinedDockerfile --push --build-arg RAY_VERSION=${RAY_VERSION}
