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
USERNAME=$1
REPO=$2

# Fetch default branch for the repository
BRANCH=$(curl -s "https://api.github.com/repos/${USERNAME}/${REPO}" | jq -r '.default_branch' 2>/dev/null)
if [ -z "$BRANCH" ] || [ "$BRANCH" = "null" ]; then
    error_exit "Failed to fetch default branch for ${USERNAME}/${REPO}."
fi

# Fetch the commit count
response=$(curl -sI "https://api.github.com/repos/${USERNAME}/${REPO}/commits?sha=${BRANCH}&per_page=1&page=1")
if [ -z "$response" ]; then
    error_exit "Failed to retrieve commit information for ${USERNAME}/${REPO}."
fi

commit_count=$(echo "$response" | grep -i '^Link:' | sed -n 's/.*page=\([0-9]*\)>; rel="last".*/\1/p')
[[ "$commit_count" =~ ^[0-9]+$ ]] || commit_count=1

# Fetch the latest release information
latest_release=$(gh api "repos/${USERNAME}/${REPO}/releases/latest" 2>/dev/null || gh api "repos/${USERNAME}/${REPO}/releases" --jq '.[0]' 2>/dev/null)
if [ -z "$latest_release" ]; then
    # Fallback to commits if no releases found
    current_commit=$(gh api "repos/${USERNAME}/${REPO}/commits/${BRANCH}" --jq '.sha[0:7]' 2>/dev/null)
    if [ -z "$current_commit" ]; then
        error_exit "Failed to retrieve commit information for ${USERNAME}/${REPO}."
    fi
    echo "r${commit_count}.${current_commit}"
    exit 0
fi

# Extract release tag name
release_name=$(echo "$latest_release" | jq -r '.tag_name')
if [ -z "$release_name" ] || [ "$release_name" = "null" ]; then
    # Fallback if no tag name found
    current_commit=$(gh api "repos/${USERNAME}/${REPO}/commits/${BRANCH}" --jq '.sha[0:7]' 2>/dev/null)
    if [ -z "$current_commit" ]; then
        error_exit "Failed to retrieve commit information for ${USERNAME}/${REPO}."
    fi
    echo "r${commit_count}.${current_commit}"
    exit 0
fi

# Fetch the SHA for the release
release_sha=$(gh api "repos/${USERNAME}/${REPO}/git/refs/tags/${release_name}" --jq '.object.sha' 2>/dev/null || echo '')
if [ -z "$release_sha" ]; then
    release_sha=$(echo "$latest_release" | jq -r '.target_commitish')
    if [ -z "$release_sha" ] || [ "$release_sha" = "null" ]; then
        error_exit "Could not retrieve a valid SHA for the release tag."
    fi
fi

# Fetch current commit hash on the default branch
current_commit=$(gh api "repos/${USERNAME}/${REPO}/commits/${BRANCH}" --jq '.sha[0:7]' 2>/dev/null)
if [ -z "$current_commit" ]; then
    error_exit "Failed to retrieve current commit hash for ${USERNAME}/${REPO}."
fi

# Output the version string
if [ -n "$release_sha" ]; then
    # Format: <tag>.r<N>.g<commit>
    _tmpname=$release_name
    [ -n "$3" ] && release_name="${_tmpname#$3}"
    echo "${release_name}+r${commit_count}+g${current_commit}"
else
    echo "r${commit_count}+${current_commit}"
fi
