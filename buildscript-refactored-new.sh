#!/bin/bash

# Strict mode configuration
set -euo pipefail

# Script configuration
readonly DEBUG=${DEBUG:-false}
[[ "$DEBUG" == "true" ]] && set -x

# Function to display usage information
show_usage() {
    cat << EOF
Usage: $(basename "$0") <args>

Required arguments:
    GITHUB_REPO      - GitHub repository name
    GITHUB_TOKEN     - GitHub authentication token
    GITHUB_WORKSPACE - GitHub workspace directory
    BUILD_MODE       - Build mode (0/1)
    PACKAGE_NAME     - Name of the package
    PKGBUILD_PATH    - Path to PKGBUILD file
    COMMIT_MESSAGE   - Git commit message

Environment variables:
    DEBUG            - Enable debug mode (true/false)
EOF
    exit 1
}

# Function to log debug messages
log_debug() {
    echo "[debug] $*"
}

# Function to log errors
log_error() {
    echo "[ERROR] $*" >&2
}

# Function to log arrays
log_array() {
    local array_name="$1"
    shift
    local array=("$@")

    log_debug "Logging array: $array_name"
    for index in "${!array[@]}"; do
        log_debug "[$index]: ${array[$index]}"
    done
}

# Function to authenticate with GitHub
setup_github_auth() {
    local token="$1"
    
    log_debug "=== Authenticating with GitHub ==="
    echo "$token" | gh auth login --with-token
    
    if ! gh auth status &>/dev/null; then
        log_error "GitHub CLI authentication failed"
        exit 1
    fi
    log_debug "GitHub CLI authenticated successfully"
}

# Function to setup build environment
setup_build_environment() {
    local package_name="$1"
    local build_dir="/tmp/build-${package_name}"
    
    rm -rf "$build_dir"
    mkdir -p "$build_dir"
    cd "$build_dir"
    
    return "$build_dir"
}

# Function to clone and setup AUR repository
setup_aur_repo() {
    local package_name="$1"
    local aur_repo="ssh://aur@aur.archlinux.org/${package_name}.git"
    
    log_debug "Cloning AUR repository for ${package_name}..."
    git clone "$aur_repo" .
}

# Function to copy package files
copy_package_files() {
    local workspace="$1"
    local pkgbuild_path="$2"
    local tracked_files=()
    
    # Copy PKGBUILD
    if [[ -f "${workspace}/${pkgbuild_path}/PKGBUILD" ]]; then
        cp "${workspace}/${pkgbuild_path}/PKGBUILD" .
        tracked_files+=("PKGBUILD")
    fi
    
    # Copy nvchecker config
    if [[ -f "${workspace}/${pkgbuild_path}/.nvchecker.toml" ]]; then
        cp "${workspace}/${pkgbuild_path}/.nvchecker.toml" .
        tracked_files+=(".nvchecker.toml")
    fi
    
    echo "${tracked_files[@]}"
}

# Function to read PKGBUILD variables
read_pkgbuild_info() {
    local -n sources=$1
    local -n depends=$2
    local -n makedepends=$3
    local -n pgpkeys=$4
    local -n packages=$5
    
    readarray -t sources < <(bash -c 'source PKGBUILD; printf "%s\n" "${source[@]}"' | grep .)
    readarray -t depends < <(bash -c 'source PKGBUILD; printf "%s\n" "${depends[@]}"' | grep .)
    readarray -t makedepends < <(bash -c 'source PKGBUILD; printf "%s\n" "${makedepends[@]}"' | grep .)
    readarray -t pgpkeys < <(bash -c 'source PKGBUILD; printf "%s\n" "${validpgpkeys[@]}"' | grep .)
    readarray -t packages < <(bash -c 'source PKGBUILD; printf "%s\n" "${pkgname[@]}"' | grep .)
}

# Function to process PGP keys
process_pgp_keys() {
    local -a pgpkeys=("$@")
    
    if [[ ${#pgpkeys[@]} -gt 0 ]]; then
        gpg --receive-keys "${pgpkeys[@]}" &&
            log_debug "== Adopted package PGP keys =="
    else
        log_debug "== No PGP keys in PKGBUILD =="
    fi
}

# Function to handle package version updates
update_package_version() {
    local pkgbuild_path="$1"
    local workspace="$2"
    
    if [[ -f "${workspace}/${pkgbuild_path}/.nvchecker.toml" ]]; then
        local new_version
        new_version=$(nvchecker -c .nvchecker.toml --logger json | jq -r 'select(.logger_name == "nvchecker.core") | .version')
        
        if [[ -z "$new_version" ]]; then
            log_error "nvchecker configuration exists but returned errors"
            exit 1
        fi
        
        log_debug "== UPDATE DETECTED ${new_version} from upstream, updating PKGBUILD..."
        sed -i "s|pkgver=.*|pkgver=${new_version}|" PKGBUILD
        echo "$new_version"
    fi
}

# Function to build package
build_package() {
    local package_name="$1"
    local -a depends=("${@:2}")
    
    if ! makepkg -s --noconfirm; then
        log_error "Package build failed for ${package_name}"
        return 1
    fi
    
    log_debug "Package ${package_name} built successfully"
    return 0
}

# Function to install package
install_package() {
    local package_name="$1"
    shift
    local -a packages=("$@")
    
    sudo rm -f "${package_name}"*debug*pkg.tar.zst || true
    
    if ! sudo pacman --noconfirm -U *.pkg.tar.zst; then
        log_error "Package installation failed for ${package_name}"
        return 1
    fi
    
    if [[ "${#packages[@]}" -gt 1 ]]; then
        log_debug "Multi-package detected, skipping removal"
    else
        log_debug "Removing installed package"
        sudo pacman --noconfirm -R "$(expac --timefmt=%s '%l\t%n' | sort | cut -f2 | xargs -r pacman -Q | cut -f1 -d' ' | tail -n 1)"
    fi
    
    return 0
}

# Function to create GitHub release
create_github_release() {
    local package_name="$1"
    local github_repo="$2"
    local release_body="To install, run: sudo pacman -U PACKAGENAME.pkg.tar.zst"
    shift 2
    local -a packages=("$@")
    
    gh release create "$package_name" \
        --title "Binary installers for ${package_name}" \
        --notes "$release_body" \
        -R "$github_repo" || log_debug "Release tag ${package_name} already exists"
    
    for pkg in "${packages[@]}"; do
        gh release upload "$package_name" ./"${pkg}"*.pkg.tar.zst --clobber -R "$github_repo"
    done
}

# Main function
main() {
    # Validate arguments
    [[ "$#" -ne 7 ]] && show_usage
    
    # Parse arguments
    local github_repo="$1"
    local github_token="$2"
    local github_workspace="$3"
    local build_mode="$4"
    local package_name="$5"
    local pkgbuild_path="$6"
    local commit_message="$7"
    
    local tracked_files=()
    local failure=0
    local is_initial=0
    
    # Setup
    setup_github_auth "$github_token"
    setup_build_environment "$package_name"
    setup_aur_repo "$package_name"
    
    # Process package files
    tracked_files=($(copy_package_files "$github_workspace" "$pkgbuild_path"))
    
    # Read PKGBUILD information
    declare -a sources depends makedepends pgpkeys packages
    read_pkgbuild_info sources depends makedepends pgpkeys packages
    
    # Process package information
    process_pgp_keys "${pgpkeys[@]}"
    local new_version
    new_version=$(update_package_version "$pkgbuild_path" "$github_workspace")
    
    # Update package
    updpkgsums
    makepkg --printsrcinfo > .SRCINFO
    tracked_files+=(".SRCINFO")
    
    # Handle git operations
    if [[ "$build_mode" == "build" ]] || [[ "$build_mode" == "test" ]]; then
        if ! build_package "$package_name" "${depends[@]}" "${makedepends[@]}"; then
            failure=1
        elif ! install_package "$package_name" "${packages[@]}"; then
            failure=1
        elif [[ "$build_mode" != "test" ]]; then
            create_github_release "$package_name" "$github_repo" "${packages[@]}"
        fi
    fi
    
    # Exit with appropriate status
    if [[ $failure -eq 0 ]]; then
        log_debug "==== ${package_name} processed successfully ===="
        exit 0
    else
        log_error "${package_name} encountered errors during processing"
        exit 1
    fi
}

# Execute main function
main "$@"
