#!/bin/bash

# Initialize flags
tags=false
releases=false

# Save all arguments
all_args=("$@")

# Parse optional flags first
while [[ $# -gt 0 ]]; do
    case "$1" in
        -tags)
            tags=true
            shift
            ;;
        -releases)
            releases=true
            shift
            ;;
        *)
            # Not an optional flag, move to next
            shift
            ;;
    esac
done

# Reset the argument list to original state
set -- "${all_args[@]}"

# Filter out the flags to get clean positional args
positional_args=()
for arg in "$@"; do
    if [[ "$arg" != "-tags" && "$arg" != "-releases" ]]; then
        positional_args+=("$arg")
    fi
done

if [ ${#positional_args[@]} -lt "2" ]; then
# Validate input arguments
    echo "Usage: $0 owner repo <strip-string>"
    echo "Example: $0 Cisco-Talos clamav clamav-"
    echo "optional flags -tags -releases - to match version from github tags or releases"
    exit 1
fi

# Now you can use both types of arguments
#echo "First positional arg: ${positional_args[0]}"
#echo "Second positional arg: ${positional_args[1]}"
#echo "Third positional arg: ${positional_args[2]}"

REPONAME=${positional_args[0]}
REPO=${positional_args[1]}
STRIPSTRING=${positional_args[2]}

if [ "$tags" = true ]; then
    echo "Tags flag is enabled"
fi

if [ "$releases" = true ]; then
    echo "Releases flag is enabled"
fi


# Function to print errors and exit
error_exit() {
    echo "Error: $1" >&2
    exit 1
}


# Fetch default branch for the repository
BRANCH=$(curl -s "https://api.github.com/repos/${REPONAME}/${REPO}" | jq -r '.default_branch' 2>/dev/null)
if [ -z "$BRANCH" ] || [ "$BRANCH" = "null" ]; then
    error_exit "Failed to fetch default branch for ${REPONAME}/${REPO}."
fi

# Fetch most recent commit
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
if [ -z "$latest_release" ] && [ -n "$tags" ]; then
    echo "r${commit_count}+g${current_commit}"
    exit 0
fi

#Fetch latest tag
lt=$(gh api /repos/${REPONAME}/${REPO}/tags --jq '.[0].name' 2>/dev/null || echo '')
latest_tag=$lt

# Extract release tag name
release_name=$(echo "$latest_release" | jq -r '.tag_name')
if [ -z "$release_name" ] || [ "$release_name" = "null" ]; then
    if [ -n "$latest_tag" ]; then
        _tmpname=${latest_tag}
        latest_tag=${_tmpname#"${STRIPSTRING:-}"}
        echo "${latest_tag}+r${commit_count}+g${current_commit}"
    else
        echo "r${commit_count}+g${current_commit}"
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
    release_name="${_tmpname#${STRIPSTRING:-}}"
    echo "${release_name}+r${commit_count}+g${current_commit}"
elif [ -n "$latest_tag" ]; then
    _tmpname=$latest_tag
    latest_tag="${_tmpname#${STRIPSTRING:-}}"
    echo "${latest_tag}+r${commit_count}+g${current_commit}"
else
    echo "r${commit_count}+g${current_commit}"
fi
