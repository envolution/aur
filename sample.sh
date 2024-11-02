curl https://aur.archlinux.org/rpc/v5/search/envolution?by=maintainer | jq '{version: 2, data: (.results | map({(.Name): {version: .Version}}) | add)}' > oldver.json
