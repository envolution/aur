#!/bin/bash
set -euo pipefail

# Usage: script.sh <GITHUB_WORKSPACE> <PACKAGE_NAME> <PKGBUILD_PATH> <COMMIT_MESSAGE>
# Example: ./script.sh /path/to/workspace mypackage path/to/PKGBUILD "Update package"

# Enable debugging if DEBUG environment variable is set
if [ "${DEBUG:-}" == "true" ]; then
    set -x
fi

# Get arguments
GITHUB_WORKSPACE="$1"
PACKAGE_NAME="$2"
PKGBUILD_PATH="$3"
COMMIT_MESSAGE="$4"
AUR_REPO="ssh://aur@aur.archlinux.org/${PACKAGE_NAME}.git"

echo "==== Starting build process for package: ${PACKAGE_NAME} ===="
echo "Using PKGBUILD located at: ${PKGBUILD_PATH}"

# Create and move to a clean build directory
BUILD_DIR="/tmp/build-${PACKAGE_NAME}"
echo "Creating and navigating to build directory: ${BUILD_DIR}"
rm -rf "${BUILD_DIR}"
mkdir -p "${BUILD_DIR}"
cd "${BUILD_DIR}"

# Clone AUR repository
echo "Cloning AUR repository for package: ${PACKAGE_NAME}..."
git clone "$AUR_REPO" .

# Update PKGBUILD if needed
if [ -f "${GITHUB_WORKSPACE}/${PKGBUILD_PATH}" ]; then
    echo "Copying PKGBUILD from workspace: ${GITHUB_WORKSPACE}/${PKGBUILD_PATH}"
    cp "${GITHUB_WORKSPACE}/${PKGBUILD_PATH}" ./PKGBUILD
fi

# Update source files
echo "Updating package checksums..."
updpkgsums

# Build package
echo "Building package: ${PACKAGE_NAME}..."
makepkg -s --noconfirm

# Install the package
echo "Installing package: ${PACKAGE_NAME}..."
sudo pacman -U --noconfirm ./${PACKAGE_NAME}*.pkg.tar.zst

# Generate .SRCINFO
echo "Generating .SRCINFO file..."
makepkg --printsrcinfo > .SRCINFO

# Commit and push changes if there are any
echo "Checking for changes to commit..."
if git diff-index --quiet HEAD --; then
    echo "No changes detected. Skipping commit and push."
else
    echo "Changes detected. Committing and pushing changes..."
    git add PKGBUILD .SRCINFO
    git fetch
    git commit -m "${COMMIT_MESSAGE}"
    git push origin master
fi

echo "==== Build process for package: ${PACKAGE_NAME} completed ===="
