#!/bin/bash
set -euo pipefail

# Ensure the script receives exactly 4 arguments
if [ "$#" -ne 5 ]; then
    echo "Usage: $0 <GITHUB_TOKEN> <GITHUB_WORKSPACE> <PACKAGE_NAME> <PKGBUILD_PATH> <COMMIT_MESSAGE>"
    exit 1
fi



# Define variables
GITHUB_TOKEN="$1"
GITHUB_WORKSPACE="$2"
PACKAGE_NAME="$3"
PKGBUILD_PATH="$4"
COMMIT_MESSAGE="$5"
RELEASE_TAG="v$(date +%Y%m%d%H%M%S)"
RELEASE_NAME="Release $RELEASE_TAG"
RELEASE_BODY="Automated release of ${PACKAGE_NAME}"

# Enable debugging if DEBUG environment variable is set
if [ "${DEBUG:-}" == "true" ]; then
    set -x
fi

# Build the package
cd "${GITHUB_WORKSPACE}/${PKGBUILD_PATH}"
makepkg -s --noconfirm

# Install the package
sudo pacman -U --noconfirm ./${PACKAGE_NAME}*.pkg.tar.zst

# Generate .SRCINFO
makepkg --printsrcinfo > .SRCINFO

# Stage tracked files that have changes
git add -u

# Check for changes and commit
echo "Checking for changes to commit..."
if git diff-index --cached --quiet HEAD --; then
    echo "No changes detected. Skipping commit and push."
else
    echo "Changes detected. Committing and pushing changes..."
    git fetch
    git commit -m "${COMMIT_MESSAGE}"
    git push origin master
fi

# Authenticate using the GitHub token
echo "${GITHUB_TOKEN}" | gh auth login --with-token

# Create a new release
gh release create "${RELEASE_TAG}" ./${PACKAGE_NAME}*.pkg.tar.zst --title "${RELEASE_NAME}" --notes "${RELEASE_BODY}"

echo "==== Build and release process for ${PACKAGE_NAME} completed ===="
