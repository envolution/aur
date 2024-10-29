#!/bin/bash

extract_version() {
    local url="$1"
    local pattern='download/([^/]+)/scilab'

    if [[ $url =~ $pattern ]]; then
        echo "${BASH_REMATCH[1]}"
    else
        echo ""
    fi
}

# Example usage
html=$(curl -L https://www.scilab.org/latest)
pattern='href="(\S+linux-gnu\.tar\.xz)"'
if [[ $html =~ $pattern ]]; then
    url="${BASH_REMATCH[1]}"
    version=$(extract_version "$url")
    echo "$version"
    exit 0
else
    exit 1
fi
