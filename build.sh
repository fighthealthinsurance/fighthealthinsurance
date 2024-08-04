#!/bin/bash
set -ex
mypy -p fighthealthinsurance
./manage.py migrate
./manage.py makemigrations
./manage.py migrate
./manage.py validate_templates
./manage.py collectstatic --no-input

pushd ./static/js; npm i; npm run build; popd
IMAGE=holdenk/fight-health-insurance:v0.1.9i
docker pull "${IMAGE}" || docker buildx build --platform=linux/amd64,linux/arm64 -t "${IMAGE}" . --push
kubectl apply -f deploy.yaml
