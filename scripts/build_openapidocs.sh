#!/bin/bash
set -ex
./manage.py spectacular --color --file schema.yml
docker run -p 80:8080 -e SWAGGER_JSON=/schema.yml -v "${PWD}/schema.yml:/schema.yml" swaggerapi/swagger-ui
