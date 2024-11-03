#!/bin/bash
set -ex
mypy -p fighthealthinsurance
./manage.py migrate
./manage.py makemigrations
./manage.py migrate
./manage.py validate_templates
./manage.py collectstatic --no-input

pushd ./static/js; npm i; npm run build; popd
FHI_VERSION=v0.8.0a
export FHI_VERSION
source build_ray.sh
IMAGE=holdenk/fight-health-insurance:${FHI_VERSION}
docker pull "${IMAGE}" || docker buildx build --platform=linux/amd64,linux/arm64 -t "${IMAGE}" . --push
kubectl apply -f deploy.yaml
# The raycluster operator doesn't handle upgrades well so delete + recreate instead.
kubectl delete raycluster -n totallylegitco raycluster-kuberay || echo "No raycluster present"
kubectl apply -f cluster.yaml
