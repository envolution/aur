#!/bin/bash
set -euo pipefail

# Ensure the script receives exactly 7 arguments
if [ "$#" -ne 7 ]; then
    echo "Usage: $0 <GITHUB_REPO> <GITHUB_TOKEN> <GITHUB_WORKSPACE> <BUILD:0:1> <PACKAGE_NAME> <PKGBUILD_PATH> <COMMIT_MESSAGE>"
    exit 1
fi

log_array() {
    local array_name="$1"
    shift
    local array=("$@")

    echo "Logging array: $array_name"
    for index in "${!array[@]}"; do
        echo "[$index]: ${array[$index]}"
    done
}

# Define variables
GITHUB_REPOSITORY="$1"
GH_TOKEN="$2"
GITHUB_WORKSPACE="$3"
BUILD=$4
PACKAGE_NAME="$5"
PKGBUILD_PATH="$6"
COMMIT_MESSAGE="$7"
RELEASE_TAG="v$(date +%Y%m)"
RELEASE_NAME="Release of packages compiled for ARCH x86_64 on $RELEASE_TAG"
RELEASE_BODY="To install, run: sudo pacman -U PACKAGENAME.pkg.tar.zst"
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

# Collect PKGBUILD from our repo
if [ -f "${GITHUB_WORKSPACE}/${PKGBUILD_PATH}/PKGBUILD" ]; then
    echo "Copying PKGBUILD and others from ${PKGBUILD_PATH}"
    cp "${GITHUB_WORKSPACE}/${PKGBUILD_PATH}/PKGBUILD" .
fi

#get the source array directly from our PKGBUILD.  We want to get all of our sources from this.
readarray -t SOURCES < <(bash -c 'source PKGBUILD; printf "%s\n" "${source[@]}"')
readarray -t DEPENDS < <(bash -c 'source PKGBUILD; printf "%s\n" "${depends[@]}"')
readarray -t MAKEDEPENDS < <(bash -c 'source PKGBUILD; printf "%s\n" "${makedepends[@]}"')

TRACKED_FILES=("PKGBUILD" ".SRCINFO")

if [[ ${#SOURCES[@]} -gt 1 ]]; then
    echo "== There is more than one source in PKGBUILD =="
    # Iterate over the array and copy our source files one at a time.  Avoid URLs
    for item in "${SOURCES[@]}"; do
        if [[ "$item" != *[/:]* ]]; then
        echo "\"$item\" identified possible file"
            if [ -f ${GITHUB_WORKSPACE}/${PKGBUILD_PATH}/${item} ]; then
                cp ${GITHUB_WORKSPACE}/${PKGBUILD_PATH}/${item} . && \
                    TRACKED_FILES+=($item) #add this file for git push
            fi
        else
            echo "${item} is an invalid file (probably a url)"
        fi
    done
else
    echo "== PKGBUILD source array looks like just one item =="
fi

# Check for a version file
if [ -f "./version.sh" ]; then
    mv "version.sh" ./_PKGBUILD_version.sh
    chmod 700 ./_PKGBUILD_version.sh
    NEW_VERSION=$(./_PKGBUILD_version.sh)
    echo "== Detected ${NEW_VERSION} from upstream, PKGBUILD updating... =="
    sed -i "s|pkgver=.*|pkgver=${NEW_VERSION}|" PKGBUILD
    rm _PKGBUILD_version.sh
    cat PKGBUILD
fi

# Update source files
echo "== Updating package checksums =="
updpkgsums

# Generate .SRCINFO
echo "== Generating .SRCINFO =="
makepkg --printsrcinfo > .SRCINFO


# Check for changes and commit
echo "== Checking for changes to commit =="
echo "== staging current files to compare against remote for changes =="
log_array "TRACKED_FILES" "${TRACKED_FILES[@]}"
git add "${TRACKED_FILES[@]}"

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
            if [[ ${#DEPENDS[@]} -gt 0 ]]; then
                paru -S --needed --norebuild --noconfirm ${SOURCES[@]}
            fi
            if [ $? -eq 0 ]; then
                echo "== Package dependencies installed successfully =="
            else
                echo "== FAIL Package dependency installation failed (this should not cause issues as makepkg will try again but won't have access to AUR) =="
                FAILURE=1
            fi
            if [[ ${#MAKEDEPENDS[@]} -gt 0 ]]; then
                paru -S --needed --norebuild --noconfirm ${MAKEDEPENDS[@]}
            fi
##                | xargs paru -S --needed --norebuild --noconfirm || true

            if [ $? -eq 0 ]; then
                echo "== Package make dependencies installed successfully =="
            else
                echo "== FAIL Package make dependency installation failed (this should not cause issues as makepkg will try again but won't have access to AUR) =="
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
                echo "=== Auth to GH ==="
                echo "${GH_TOKEN}" | gh auth login --with-token
                echo "=== Push compiled binary to releases ==="
                gh release create "${PACKAGE_NAME}" --title "Binary installers for ${PACKAGE_NAME}" --notes "${RELEASE_BODY}" -R "${GITHUB_REPOSITORY}" \
                    || echo "== Assuming tag ${PACKAGE_NAME} exists as we can't create one =="
                gh release upload "${PACKAGE_NAME}" ./${PACKAGE_NAME}*.pkg.tar.zst --clobber -R "${GITHUB_REPOSITORY}"
            else
                echo "== FAIL install of ${PACKAGE_NAME} failed (skipping commit) =="
                FAILURE=1
            fi
        fi

        if [ $FAILURE = 0 ]; then
            echo '----'
            ls -latr
            echo '----'

            git fetch
            # Stage tracked files that have changes
            echo "== ATTEMPTING TO TRACK CHANGES FOR FILES =="
            log_array "TRACKED_FILES" "${TRACKED_FILES[@]}"
            git add "${TRACKED_FILES[@]}"
            #git add PKGBUILD .SRCINFO

            git commit -m "${COMMIT_MESSAGE}"
            git push origin master
            if [ $? -eq 0 ]; then
                echo "== ${PACKAGE_NAME} submitted to AUR successfully =="
                # We update our local PKGBUILD now since we've confirmed an update to remote AUR
                gh api -X PUT /repos/${GITHUB_REPOSITORY}/contents/${PACKAGE_NAME}/PKGBUILD \
                    -f message="Updated file" \
                    -f content="$(base64 < PKGBUILD)" \
                    --jq '.commit.sha' \
                    -f sha="$(gh api repos/${GITHUB_REPOSITORY}/contents/${PACKAGE_NAME}/PKGBUILD --jq '.sha')"
                echo "== local PKGBUILD updated =="
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
