#!/bin/bash
curl https://api.github.com/repos/zenorogue/hyperrogue/tags | jq -r '.[1].name'
