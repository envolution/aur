#!/bin/bash

set -euo pipefail

# Global variables
declare GITHUB_REPOSITORY=""
declare GH_TOKEN=""
declare GITHUB_WORKSPACE=""
declare BUILD=""
declare PACKAGE_NAME=""
declare PKGBUILD_PATH=""
declare COMMIT_MESSAGE=""
declare -r RELEASE_BODY="To install, run: sudo pacman -U PACKAGENAME.pkg.tar.zst"
declare -r BUILD_DIR=""
declare -a TRACKED_FILES=()
declare FAILURE=0
declare INITIAL=0
declare NEW_VERSION=""

log_debug() {
    echo "[debug] $*"
}

log_error() {
    echo "[debug] !!!== $* ==" >&2
}

log_array() {
    local array_name="$1"
    shift
    local array=("$@")

    log_debug "Logging array: $array_name"
    for index in "${!array[@]}"; do
        log_debug "[$index]: ${array[$index]}"
    done
}

validate_arguments() {
    if [ "$#" -ne 7 ]; then
        log_debug "Usage: $0 <GITHUB_REPO> <GITHUB_TOKEN> <GITHUB_WORKSPACE> <BUILD:0:1> <PACKAGE_NAME> <PKGBUILD_PATH> <COMMIT_MESSAGE>"
        exit 1
    fi

    GITHUB_REPOSITORY="$1"
    GH_TOKEN="$2"
    GITHUB_WORKSPACE="$3"
    BUILD="$4"
    PACKAGE_NAME="$5"
    PKGBUILD_PATH="$6"
    COMMIT_MESSAGE="$7"
    BUILD_DIR="/tmp/build-${PACKAGE_NAME}"
}

setup_github_auth() {
    log_debug "=== Auth to GH ==="
    echo "${GH_TOKEN}" | gh auth login --with-token
    if ! gh auth status &>/dev/null; then
        log_error "GitHub CLI is not authenticated"
        exit 1
    fi
    log_debug "GitHub CLI is authenticated"
}

setup_build_directory() {
    rm -rf "${BUILD_DIR}"
    mkdir -p "${BUILD_DIR}"
    cd "${BUILD_DIR}"
    
    local aur_repo="ssh://aur@aur.archlinux.org/${PACKAGE_NAME}.git"
    log_debug "Cloning AUR repository for ${PACKAGE_NAME}..."
    git clone "$aur_repo" .
}

copy_package_files() {
    # Copy PKGBUILD
    if [ -f "${GITHUB_WORKSPACE}/${PKGBUILD_PATH}/PKGBUILD" ]; then
        log_debug "Copying PKGBUILD from ${PKGBUILD_PATH}"
        cp "${GITHUB_WORKSPACE}/${PKGBUILD_PATH}/PKGBUILD" .
        TRACKED_FILES+=("PKGBUILD")
    fi

    # Copy nvchecker config
    if [ -f "${GITHUB_WORKSPACE}/${PKGBUILD_PATH}/.nvchecker.toml" ]; then
        log_debug "Copying .nvchecker.toml from ${PKGBUILD_PATH}"
        cp "${GITHUB_WORKSPACE}/${PKGBUILD_PATH}/.nvchecker.toml" .
        TRACKED_FILES+=(".nvchecker.toml")
    fi
}

read_package_info() {
    readarray -t SOURCES < <(bash -c 'source PKGBUILD; printf "%s\n" "${source[@]}"' | grep .)
    readarray -t DEPENDS < <(bash -c 'source PKGBUILD; printf "%s\n" "${depends[@]}"' | grep .)
    readarray -t MAKEDEPENDS < <(bash -c 'source PKGBUILD; printf "%s\n" "${makedepends[@]}"' | grep .)
    readarray -t PGPKEYS < <(bash -c 'source PKGBUILD; printf "%s\n" "${validpgpkeys[@]}"' | grep .)

    # Log arrays
    [[ ${#SOURCES[@]} -gt 0 ]] && log_array "SOURCES" "${SOURCES[@]}" ||
        log_error "No sources in PKGBUILD, this is probably not intended"
    [[ ${#DEPENDS[@]} -gt 0 ]] && log_array "DEPENDS" "${DEPENDS[@]}" ||
        log_debug "== No depends in PKGBUILD =="
    [[ ${#MAKEDEPENDS[@]} -gt 0 ]] && log_array "MAKEDEPENDS" "${MAKEDEPENDS[@]}" ||
        log_debug "== No make depends in PKGBUILD =="
}

handle_pgp_keys() {
    if [[ ${#PGPKEYS[@]} -gt 0 ]]; then
        gpg --receive-keys "${PGPKEYS[@]}" &&
            log_debug "== Adopted package PGP keys ==" ||
            log_debug "== No PGP keys in PKGBUILD =="
    fi
}

copy_source_files() {
    if [[ ${#SOURCES[@]} -gt 1 ]]; then
        log_debug "== There is more than one source in PKGBUILD =="
        for item in "${SOURCES[@]}"; do
            if [[ "$item" != *[/:]* ]]; then
                log_debug "\"$item\" identified possible file"
                if [ -f "${GITHUB_WORKSPACE}/${PKGBUILD_PATH}/${item}" ]; then
                    cp "${GITHUB_WORKSPACE}/${PKGBUILD_PATH}/${item}" . &&
                        TRACKED_FILES+=("${item}")
                fi
            else
                log_debug "${item} is an invalid file (probably a url)"
            fi
        done
    else
        log_debug "== PKGBUILD source array looks like just one item =="
    fi
}

update_version() {
    if [ -f ".nvchecker.toml" ]; then
        NEW_VERSION=$(nvchecker -c .nvchecker.toml --logger json | jq -r 'select(.logger_name == "nvchecker.core") | .version')
        if [[ -z $NEW_VERSION ]]; then
            log_error ".nvchecker.toml exists, but it's giving errors"
            exit 1
        fi
        log_debug "== UPDATE DETECTED ${NEW_VERSION} from upstream, PKGBUILD updating... =="
        sed -i "s|pkgver=.*|pkgver=${NEW_VERSION}|" PKGBUILD
        cat PKGBUILD
    fi
}

build_package() {
    if [[ "$BUILD" != "build" && "$BUILD" != "test" ]]; then
        return 0
    fi

    install_dependencies
    
    log_debug "Building package..."
    if ! makepkg -s --noconfirm; then
        log_error "makepkg build of ${PACKAGE_NAME} failed (skipping commit)"
        return 1
    fi
    
    install_package
    return 0
}

install_dependencies() {
    if [[ ${#DEPENDS[@]} -gt 0 ]]; then
        if paru -S --needed --norebuild --noconfirm --mflags "--skipchecksums --skippgpcheck" "${DEPENDS[@]}"; then
            log_debug "== Package dependencies installed successfully =="
        else
            log_debug "== FAIL Package dependency installation failed =="
        fi
    fi

    if [[ ${#MAKEDEPENDS[@]} -gt 0 ]]; then
        if paru -S --needed --norebuild --noconfirm --mflags "--skipchecksums --skippgpcheck" "${MAKEDEPENDS[@]}"; then
            log_debug "== Package make dependencies installed successfully =="
        else
            log_debug "== FAIL Package make dependency installation failed =="
        fi
    fi
}

install_package() {
    log_debug "== Installing package '${PACKAGE_NAME}' and attempting to auto resolve any conflicts =="
    sudo rm -f "${PACKAGE_NAME}"*debug*pkg.tar.zst || true
    ls -latr

    if ! sudo pacman --noconfirm -U "${PACKAGE_NAME}"*.pkg.tar.zst; then
        log_error "install of ${PACKAGE_NAME} failed (skipping commit)"
        return 1
    fi

    log_debug "== Package ${PACKAGE_NAME} installed successfully, attempting to remove it =="
    sudo pacman --noconfirm -R "$(expac --timefmt=%s '%l\t%n' | sort | cut -f2 | xargs -r pacman -Q | cut -f1 -d' ' | tail -n 1)"

    if [[ "$BUILD" != "test" ]]; then
        create_github_release
    fi
    return 0
}

create_github_release() {
    log_debug "=== Push compiled binary to releases ==="
    gh release create "${PACKAGE_NAME}" --title "Binary installers for ${PACKAGE_NAME}" --notes "${RELEASE_BODY}" -R "${GITHUB_REPOSITORY}" ||
        log_debug "== Assuming tag ${PACKAGE_NAME} exists as we can't create one =="
    gh release upload "${PACKAGE_NAME}" ./"${PACKAGE_NAME}"*.pkg.tar.zst --clobber -R "${GITHUB_REPOSITORY}"
}

commit_changes() {
    git rm --cached .gitignore || true
    git add -f "${TRACKED_FILES[@]}"

    if [ -z "$(git rev-parse --verify HEAD 2>/dev/null)" ]; then
        log_debug "== Initial commit, committing selected files =="
        git commit -m "${COMMIT_MESSAGE}: ${NEW_VERSION:-}"
        git push
        INITIAL=1
        return 0
    fi

    if git diff-index --cached --quiet HEAD -- && [ $INITIAL -ne 1 ] && [[ ${PACKAGE_NAME} != *-git ]] && [ "$BUILD" != "test" ]; then
        log_debug "== No changes detected. Skipping commit and push =="
        return 0
    fi

    if [ "$BUILD" != "test" ]; then
        push_to_aur
        update_github_repo
    else
        log_debug "== Test for $PACKAGE_NAME concluded =="
    fi
}

push_to_aur() {
    git commit -m "${COMMIT_MESSAGE}: ${NEW_VERSION:-}"
    if ! git push origin master; then
        log_error "${PACKAGE_NAME} submission to AUR failed"
        FAILURE=1
        return 1
    fi
    log_debug "== ${PACKAGE_NAME} submitted to AUR successfully =="
    return 0
}

update_github_repo() {
    for file in "${TRACKED_FILES[@]}"; do
        if [[ ! -f "$file" ]]; then
            log_error "${file} is in our tracked files but doesn't appear to be a file"
            continue
        fi

        sha=$(gh api "/repos/${GITHUB_REPOSITORY}/contents/${PKGBUILD_PATH}/${file}" --jq '.sha' 2>/dev/null) || true
        if [[ -n "$sha" ]]; then
            filesha=$(git hash-object "$file")
            if [[ "$sha" != "$filesha" ]]; then
                update_github_file "$file" "$sha"
            else
                log_debug "$file with same sha on remote. Skipping..."
            fi
        else
            create_github_file "$file"
        fi
    done
}

update_github_file() {
    local file="$1"
    local sha="$2"
    
    log_debug "local ${file} != /contents/${PKGBUILD_PATH}/${file} replacing..."
    if gh api -X PUT "/repos/${GITHUB_REPOSITORY}/contents/${PKGBUILD_PATH}/${file}" \
        -f message="Auto updated ${file} in ${GITHUB_REPOSITORY} while syncing to AUR" \
        -f content="$(base64 <"${file}")" \
        --jq '.commit.sha' \
        -f sha="$sha"; then
        log_debug "==${file} pushed to ${GITHUB_REPOSITORY}/${PKGBUILD_PATH} successfully =="
    else
        log_error "FAILED on ${file} push to ${GITHUB_REPOSITORY}/${PKGBUILD_PATH}"
    fi
}

create_github_file() {
    local file="$1"
    
    if gh api -X PUT "/repos/${GITHUB_REPOSITORY}/contents/${PKGBUILD_PATH}${file}" \
        -f message="Added ${file} to ${GITHUB_REPOSITORY}" \
        -f content="$(base64 <"${file}")" \
        --jq '.commit.sha'; then
        log_debug "==${file} pushed to ${GITHUB_REPOSITORY}/${PKGBUILD_PATH} successfully =="
    else
        log_error "FAILED on ${file} push to ${GITHUB_REPOSITORY}/${PKGBUILD_PATH}"
    fi
}

main() {
    validate_arguments "$@"
    
    # Enable debugging if DEBUG environment variable is set
    [[ "${DEBUG:-}" == "true" ]] && set -x

    setup_github_auth
    setup_build_directory
    copy_package_files
    read_package_info
    handle_pgp_keys
    copy_source_files
    update_version
    
    # Update source files and generate .SRCINFO
    updpkgsums
    makepkg --printsrcinfo >.SRCINFO
    TRACKED_FILES+=(".SRCINFO")
    
    if ! build_package; then
        FAILURE=1
    else
        commit_changes
    fi

    if [ $FAILURE -eq 0 ]; then
        log_debug "==== ${PACKAGE_NAME} processed without detected errors ===="
    else
        log_debug "**** ${PACKAGE_NAME} has had some errors while processing, check logs for more details ****"
        exit 1
    fi
    log_debug "==== Build and release process for ${PACKAGE_NAME} exiting ===="
}

main "$@"
