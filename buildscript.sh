#!/bin/bash
set -euo pipefail

# Get arguments
GITHUB_WORKSPACE="$1"
PACKAGE_NAME="$2"
PKGBUILD_PATH="$3"
COMMIT_MESSAGE="$4"


AUR_REPO="ssh://aur@aur.archlinux.org/${PACKAGE_NAME}.git"

echo "==== Starting build for ${PACKAGE_NAME} ===="
echo "Using PKGBUILD from: ${PKGBUILD_PATH}"

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
sudo pacman -U --noconfirm ${PACKAGE_NAME}*.pkg.tar.zst

# Generate .SRCINFO
echo "Generating .SRCINFO..."
makepkg --printsrcinfo > .SRCINFO

# Commit and push changes
echo "Committing and pushing changes..."
git add PKGBUILD .SRCINFO
git commit -m "${COMMIT_MESSAGE}"
git push origin master

echo "==== Successfully built and published ${PACKAGE_NAME} ===="
