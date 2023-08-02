#!/bin/bash
set -ex
mypy -p fighthealthinsurance
./manage.py migrate
./manage.py makemigrations
./manage.py migrate
./manage.py validate_templates
./manage.py collectstatic --no-input

pushd ./static/js; npm i; npm run build; popd
# Hack, for now.
#docker buildx build --platform=linux/amd64,linux/arm64 -t holdenk/fight-health-insurance:0.0.1 . --push
docker buildx build --platform=linux/amd64,linux/arm64 -t holdenk/fight-health-insurance:v0.0.4 . --push
kubectl apply -f deploy.yaml
