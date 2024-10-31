#!/bin/bash
curl https://api.github.com/repos/lobehub/lobe-chat/tags | jq -r '.[1].name' | cut -c2-