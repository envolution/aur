#!/usr/bin/env bash

set -o errexit
set -o pipefail

# Function to fetch the latest commit information from a GitHub repository
fetch_latest_commit_info() {
    declare -r repo="$1"
    declare -r branch="$2"
    declare -r repo_url="https://github.com/${repo}.git"

    # Fetch the latest commit reference
    declare latest_ref
    latest_ref=$(git ls-remote "$repo_url" "$branch" | awk '{print $1}')

    if [[ -z "$latest_ref" ]]; then
        echo "Error: Failed to fetch latest commit reference." >&2
        return 1
    fi

    # Extract the short commit hash (first 7 characters)
    declare -r short_hash="${latest_ref:0:7}"

    # Fetch the date of the latest commit using GitHub API
    declare last_date
    last_date=$(curl -s "https://api.github.com/repos/${repo}/commits?per_page=1" | 
                grep -o '"date": "[^"]*"' | 
                cut -d'"' -f4 | 
                cut -dT -f1 | 
                tr -d '-' | 
                head -n 1)

    if [[ -z "$last_date" ]]; then
        echo "Error: Failed to fetch commit date." >&2
        return 1
    fi

    # Output the result in the format: YYYYMMDD-SHORTHASH
    echo "${last_date}-${short_hash}"
}

# Function to display usage information
display_usage() {
    cat << EOF
Usage: $0 [REPOSITORY] [BRANCH]

Fetch the latest commit information from a GitHub repository.

Arguments:
  REPOSITORY  GitHub repository in the format "owner/repo"
  BRANCH      Branch name (default: master)

Example:
  $0 octocat/Hello-World main
EOF
}

# Main script execution
main() {

    if [[ "$1" == "-h" || "$1" == "--help" ]]; then
        display_usage
        exit 0
    fi

    declare repo="${1}"
    declare branch="${2}"

    if [[ ! "$repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
        echo "Error: Invalid repository format. Please use 'owner/repo'." >&2
        display_usage
        exit 1
    fi

    if [[ -z "$branch" ]]; then
        echo "Error: Branch name cannot be empty." >&2
        display_usage
        exit 1
    fi

    fetch_latest_commit_info "$repo" "$branch"
}

main "$@"
