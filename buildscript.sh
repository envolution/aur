#!/bin/bash
set -euo pipefail

# Ensure the script receives exactly 4 arguments
if [ "$#" -ne 5 ]; then
    echo "Usage: $0 <GITHUB_TOKEN> <GITHUB_WORKSPACE> <PACKAGE_NAME> <PKGBUILD_PATH> <COMMIT_MESSAGE>"
    exit 1
fi



# Define variables
GITHUB_TOKEN="$1"
echo "=== GITHUB TOKEN IS $1 ==="
GITHUB_WORKSPACE="$2"
PACKAGE_NAME="$3"
PKGBUILD_PATH="$4"
COMMIT_MESSAGE="$5"
RELEASE_TAG="v$(date +%Y%m%d%H%M%S)"
RELEASE_NAME="Release $RELEASE_TAG"
RELEASE_BODY="Automated release of ${PACKAGE_NAME}"
AUR_REPO="ssh://aur@aur.archlinux.org/${PACKAGE_NAME}.git"

# Enable debugging if DEBUG environment variable is set
if [ "${DEBUG:-}" == "true" ]; then
    set -x
fi

# Create and move to a clean build directory
BUILD_DIR="/tmp/build-${PACKAGE_NAME}"
rm -rf "${BUILD_DIR}"
mkdir -p "${BUILD_DIR}"
cd "${BUILD_DIR}"

# Clone AUR repository
echo "Cloning AUR repository for ${PACKAGE_NAME}..."
git clone "$AUR_REPO" .

# Update PKGBUILD if needed
if [ -f "${GITHUB_WORKSPACE}/${PKGBUILD_PATH}" ]; then
    echo "Copying PKGBUILD from ${PKGBUILD_PATH}"
    cp "${GITHUB_WORKSPACE}/${PKGBUILD_PATH}" ./PKGBUILD
fi

# Update source files
echo "Updating package checksums..."
updpkgsums

# Build package
echo "Building package..."
makepkg -s --noconfirm

# Install the package
echo "Installing package..."
sudo pacman -U --noconfirm ./${PACKAGE_NAME}*.pkg.tar.zst

# Generate .SRCINFO
echo "Generating .SRCINFO..."
makepkg --printsrcinfo > .SRCINFO

# Stage tracked files that have changes
git add PKGBUILD .SRCINFO

# Check for changes and commit
echo "Checking for changes to commit..."
if [ -z "$(git rev-parse --verify HEAD 2>/dev/null)" ]; then
    echo "Initial commit, committing selected files..."
    git commit -m "${COMMIT_MESSAGE}"
else
    if git diff-index --cached --quiet HEAD --; then
        echo "No changes detected. Skipping commit and push."
    else
        echo "Changes detected. Committing and pushing selected files..."
        git fetch
        git commit -m "${COMMIT_MESSAGE}"
        git push origin master
    fi
fi

# Authenticate using the GitHub token
#echo "${GITHUB_TOKEN}" | gh auth login --with-token

# Create a new release
#gh release create "${RELEASE_TAG}" ./${PACKAGE_NAME}*.pkg.tar.zst --title "${RELEASE_NAME}" --notes "${RELEASE_BODY}" -R "envolution/aur"

echo "==== Build and release process for ${PACKAGE_NAME} completed ===="
