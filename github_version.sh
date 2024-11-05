#!/bin/bash

# Usage: ./ghv.sh owner repo
# Requires: GitHub CLI (gh) and jq

set -euo pipefail

# Function to print errors and exit
error_exit() {
    echo "Error: $1" >&2
    exit 1
}

# Validate input arguments
if [ "$#" -lt "2" ]; then
    echo "Usage: $0 owner repo <strip-string>"
    echo "Example: $0 Cisco-Talos clamav clamav-"
    exit 1
fi

# Initialize variables
REPONAME=$1
REPO=$2

# Fetch default branch for the repository
BRANCH=$(curl -s "https://api.github.com/repos/${REPONAME}/${REPO}" | jq -r '.default_branch' 2>/dev/null)
if [ -z "$BRANCH" ] || [ "$BRANCH" = "null" ]; then
    error_exit "Failed to fetch default branch for ${REPONAME}/${REPO}."
fi

current_commit=$(gh api "repos/${REPONAME}/${REPO}/commits/${BRANCH}" --jq '.sha[0:9]' 2>/dev/null)
if [ -z "$current_commit" ]; then
    error_exit "Failed to retrieve commit information for ${REPONAME}/${REPO}."
fi

# Fetch the commit count
response=$(curl -sI "https://api.github.com/repos/${REPONAME}/${REPO}/commits?sha=${BRANCH}&per_page=1&page=1")
if [ -z "$response" ]; then
    error_exit "Failed to retrieve commit information for ${REPONAME}/${REPO}."
fi

commit_count=$(echo "$response" | grep -i '^Link:' | sed -n 's/.*page=\([0-9]*\)>; rel="last".*/\1/p')
[[ "$commit_count" =~ ^[0-9]+$ ]] || commit_count=1

# Fetch the latest release information
latest_release=$(gh api "repos/${REPONAME}/${REPO}/releases/latest" 2>/dev/null || gh api "repos/${REPONAME}/${REPO}/releases" --jq '.[0]' 2>/dev/null)
if [ -z "$latest_release" ]; then
    echo "r${commit_count}.${current_commit}"
    exit 0
fi

#Fetch latest tag
lt=$(gh api /repos/KDE/kdeconnect-kde/tags --jq '.[0].name' 2>/dev/null || echo '')
latest_tag=$lt

# Extract release tag name
release_name=$(echo "$latest_release" | jq -r '.tag_name')
if [ -z "$release_name" ] || [ "$release_name" = "null" ]; then
    if [ -n "$latest_tag" ]; then
	_tmpname=${latest_tag}
	latest_tag=${_tmpname#${3:-}}
        echo "${latest_tag}+r${commit_count}+${current_commit}"
    else
        echo "r${commit_count}.${current_commit}"
    fi
    exit 0
fi

# Fetch the SHA for the release
release_sha=$(gh api "repos/${REPONAME}/${REPO}/git/refs/tags/${release_name}" --jq '.object.sha' 2>/dev/null || echo '')
if [ -z "$release_sha" ]; then
    release_sha=$(echo "$latest_release" | jq -r '.target_commitish')
    if [ -z "$release_sha" ] || [ "$release_sha" = "null" ]; then
        error_exit "Could not retrieve a valid SHA for the release tag."
    fi
fi

# Output the version string
if [ -n "$release_sha" ]; then
    # Format: <tag>.r<N>.g<commit>
    _tmpname=$release_name
    release_name="${_tmpname#${3:-}}"
    echo "${release_name}+r${commit_count}+g${current_commit}"
elif [ -n "$latest_tag" ]; then
    _tmpname=$latest_tag
    latest_tag="${_tmpname#${3:-}}"
    echo "${latest_tag}+r${commit_count}+${current_commit}"
else
    echo "r${commit_count}+${current_commit}"
fi
