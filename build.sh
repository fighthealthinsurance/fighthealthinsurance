#!/bin/bash
set -ex
mypy -p fighthealthinsurance
./manage.py migrate
./manage.py makemigrations
./manage.py migrate
./manage.py validate_templates
./manage.py collectstatic --no-input

pushd ./static/js; npm i; npm run build; popd
FHI_VERSION=v0.9.3d
export FHI_VERSION
# Build ray cluster first so that the cluster can come up before the job that registers the workers
source build_ray.sh
# The raycluster operator doesn't handle upgrades well so delete + recreate instead.
kubectl delete raycluster -n totallylegitco raycluster-kuberay || echo "No raycluster present"
kubectl apply -f cluster.yaml
# Build the web app
IMAGE=holdenk/fight-health-insurance:${FHI_VERSION}
(docker pull "${IMAGE}" && sleep 10) || docker buildx build --platform=linux/amd64,linux/arm64 -t "${IMAGE}" . --push
kubectl apply -f deploy.yaml
