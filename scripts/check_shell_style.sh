#!/bin/bash
find . -type f -name "*.sh" -not -path "./.tox/*" -exec shellcheck -e SC1091 {} +
