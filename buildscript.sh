#!/bin/bash
set -euo pipefail

# Ensure the script receives exactly 7 arguments
if [ "$#" -ne 7 ]; then
    echo "Usage: $0 <GITHUB_REPO> <GITHUB_TOKEN> <GITHUB_WORKSPACE> <BUILD:0:1> <PACKAGE_NAME> <PKGBUILD_PATH> <COMMIT_MESSAGE>"
    exit 1
fi

# Define variables
GITHUB_REPOSITORY="$1"
GH_TOKEN="$2"
GITHUB_WORKSPACE="$3"
BUILD=$4
PACKAGE_NAME="$5"
PKGBUILD_PATH="$6"
COMMIT_MESSAGE="$7"
RELEASE_TAG="v$(date +%Y%m%d%H%M%S)"
RELEASE_NAME="Release $RELEASE_TAG"
RELEASE_BODY="Automated release of ${PACKAGE_NAME}"
AUR_REPO="ssh://aur@aur.archlinux.org/${PACKAGE_NAME}.git"
FAILURE=0

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

# Generate .SRCINFO
echo "Generating .SRCINFO..."
makepkg --printsrcinfo > .SRCINFO

# Stage tracked files that have changes
git add PKGBUILD .SRCINFO

# Check for changes and commit
echo "== Checking for changes to commit =="
if [ -z "$(git rev-parse --verify HEAD 2>/dev/null)" ]; then
    echo "== Initial commit, committing selected files =="
    git commit -m "${COMMIT_MESSAGE}"

else

    if git diff-index --cached --quiet HEAD --; then
        echo "== No changes detected. Skipping commit and push =="
    else

        echo "== Changes detected. Committing and pushing selected files =="
        if [ $BUILD == "build" ]; then

            echo "== ${PACKAGE_NAME} has been configured to be compiled and installed before pushing =="

            #Install package dependancies
            paru -Si $PACKAGE_NAME \
                | awk -F"[:<=>]" "/^(Depends On|Make Deps)/{print \$2}" \
                | tr -s " " "\n" \
                | grep -v "^$" \
                | xargs paru -S --needed --norebuild --noconfirm || true

            if [ $? -eq 0 ]; then
                echo "== Package dependencies installed successfully =="
            else
                echo "== FAIL Package dependency installation failed (this should not cause issues as makepkg will try again but won't have access to AUR) =="
                FAILURE=1
            fi

            # Build package
            echo "Building package..."
            makepkg -s --noconfirm
            if [ $? -eq 0 ]; then
                echo "== Package ${PACKAGE_NAME} built successfully =="
            else
                echo "== FAIL makepkg build of ${PACKAGE_NAME} failed (skipping commit) =="
                FAILURE=1
            fi            

            # Install the package
            echo "== Installing package =="
            sudo pacman -U --noconfirm ./${PACKAGE_NAME}*.pkg.tar.zst
            if [ $? -eq 0 ]; then
                echo "== Package ${PACKAGE_NAME} installed successfully =="
                # Create a new release
                # Authenticate using the GitHub token
                #echo "=== Auth to GH ==="
                #echo "${GH_TOKEN}" | gh auth login --with-token
                echo "=== Push compiled binary to releases ==="
                gh release create "${RELEASE_TAG}" ./${PACKAGE_NAME}*.pkg.tar.zst --title "${RELEASE_NAME}" --notes "${RELEASE_BODY}" -R "${GITHUB_REPOSITORY}"
            else
                echo "== FAIL install of ${PACKAGE_NAME} failed (skipping commit) =="
                FAILURE=1
            fi            
        fi

        if [ $FAILURE = 0 ]; then
            git fetch
            git commit -m "${COMMIT_MESSAGE}"
            git push origin master
            if [ $? -eq 0 ]; then
                echo "== ${PACKAGE_NAME} submitted to AUR successfully =="
            else
                echo "== FAILED ${PACKAGE_NAME} submission to AUR =="
                FAILURE = 1
            fi 
        fi

    fi
fi
if [ $FAILURE -eq 0 ]; then
    echo "==== ${PACKAGE_NAME} processed without detected errors ===="
else
    echo "**** ${PACKAGE_NAME} has had some errors while processing, check logs for more details ****"
fi
echo "==== Build and release process for ${PACKAGE_NAME} exiting ===="
