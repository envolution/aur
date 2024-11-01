#!/bin/bash
curl https://api.github.com/repos/lobehub/lobe-chat/tags | jq -r '.[0].name' | cut -c2-