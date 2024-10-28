#!/bin/sh
curl -s https://api.github.com/repos/RTimothyEdwards/XCircuit/tags | jq -r '.[0].name'
