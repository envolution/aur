#!/bin/bash
set -euo pipefail

# Constants
BUILDER_HOME="/home/builder"
NVCHECKER_DIR="${BUILDER_HOME}/nvchecker"
DEPENDS_JSON_PATH="${NVCHECKER_DIR}/depends.json"

# Helper Functions
log_debug() {
    echo "[debug] $1"
}

log_error() {
    echo "[error] $1"
}

setup_environment() {
    sudo -u builder mkdir -p "${NVCHECKER_DIR}"
    cd "${NVCHECKER_DIR}"
    # Copy required Python scripts if they exist
    for script in "github_version.py" "compare_aur_local_versions.py" "buildscript.py"; do
        if [ -f "${GITHUB_WORKSPACE}/${script}" ]; then
            cp "${GITHUB_WORKSPACE}/${script}" "${NVCHECKER_DIR}"
        fi
    done
}

generate_nvchecker_config() {
    local config_file="new.toml"
    
    # Create base config
    cat > "${config_file}" << EOF
[__config__]
oldver='aur.json'
newver='local.json'

EOF
    
    # Append all .nvchecker.toml files
    find "${GITHUB_WORKSPACE}" -name ".nvchecker.toml" -exec sh -c 'cat {} && echo' \; >> "${config_file}"
    
    log_debug "Generated ${config_file}:"
    cat "${config_file}"
}

run_version_checks() {
    local original_home=$HOME
    HOME="${BUILDER_HOME}"
    
    # Run nvchecker and generate comparison
    nvchecker -c new.toml --logger json > newver.json
    nvcmp -c new.toml
    
    HOME="${original_home}"
}

get_package_updates() {
    local UPGRADES=()
    local NVCUPDATES=()
    
    # Get upgrades from changes.json
    mapfile -t UPGRADES < <(jq -r 'to_entries | map(select(.value.status == "upgrade") | .key) | .[]' changes.json)
    log_debug "List of packages with UPGRADES scheduled from REPO -> AUR:"
    printf '%s\n' "${UPGRADES[@]:-}"
    
    # Get updates from nvchecker
    readarray -t NVCUPDATES < <(nvcmp -c new.toml -q)
    log_debug "List of packages in with UPGRADES scheduled from NVCHECKER:"
    printf '%s\n' "${NVCUPDATES[@]:-}"
    
    # Combine and deduplicate updates
    # Use printf to handle empty arrays properly
    UPDATES=($(printf '%s\n' "${UPGRADES[@]:-}" "${NVCUPDATES[@]:-}" | sort -u))
    declare -g UPDATES
}

extract_components() {
    local path="$1"
    IFS='/' read -r -a components <<< "${path}"
    echo "${components[@]}"
}

extract_dependencies() {
    local dir="$1"
    (
        source "${GITHUB_WORKSPACE}/${dir}/PKGBUILD"
        declare -p depends makedepends checkdepends 2>/dev/null || true
    )
}

process_package_dependencies() {
    local package="$1"
    local update_dir="$2"
    local deps_output
    
    # Extract dependencies
    deps_output=$(extract_dependencies "${update_dir}")
    
    # Reset and evaluate dependency variables
    unset depends makedepends checkdepends
    eval "$deps_output"
    
    # Convert arrays to JSON, handling empty arrays
    local depends_json
    depends_json=$(jq -n \
        --arg pkg "$package" \
        --arg deps "$(printf '%s\n' "${depends[@]:-}" | jq -R -s -c 'split("\n")[:-1]')" \
        --arg mkdeps "$(printf '%s\n' "${makedepends[@]:-}" | jq -R -s -c 'split("\n")[:-1]')" \
        --arg chkdeps "$(printf '%s\n' "${checkdepends[@]:-}" | jq -R -s -c 'split("\n")[:-1]')" \
        '{($pkg): {depends: ($deps|fromjson), makedepends: ($mkdeps|fromjson), checkdepends: ($chkdeps|fromjson)}}')
    
    echo "$depends_json"
}

build_package() {
    local package="$1"
    local build="$2"
    local pkgbuild="$3"
    local depjson=$DEPENDS_JSON_PATH
    
    log_debug "Building package: ${package}"
    
    sudo -u builder python buildscript.py \
        --github-repo "${{ github.repository }}" \
        --github-token "$GH_TOKEN" \
        --github-workspace "$GITHUB_WORKSPACE" \
        --package-name "$package" \
        --depends-json "$depjson" \
        --pkgbuild-path "${pkgbuild}" \
        --commit-message "Auto update: ${package}" \
        --build-mode "$build" \
        --debug
}

main() {
    setup_environment
    
    # Run initial version comparison
    python "${NVCHECKER_DIR}/compare_aur_local_versions.py" \
        --maintainer "${AUR_MAINTAINER_NAME}" \
        --repo-root "${GITHUB_WORKSPACE}"
    
    generate_nvchecker_config
    run_version_checks
    get_package_updates
    echo "1"
    # Process dependencies for all updates
    local final_depends_json="{}"
    echo "2"
    for update in "${UPDATES[@]}"; do
        local subdirs
        subdirs=($(find "${GITHUB_WORKSPACE}" -maxdepth 3 -type d -name "${update}" -exec realpath --relative-to="${GITHUB_WORKSPACE}" {} +))
        if [ ${#subdirs[@]} -ne 1 ]; then
            log_debug "Skipping '${update}': found ${#subdirs[@]} matching directories"
            continue
        fi
        
        local update_dir="${subdirs[0]}"
        local components
        components=($(extract_components "${update_dir}"))
        local maintain="${components[0]:-}"
        local build="${components[1]:-}"
        local package="${components[2]:-}"
        
        if [ -z "$package" ] || [ -z "$build" ] || [ -z "$maintain" ]; then
            log_error "Invalid directory structure for ${update_dir}"
            continue
        fi
        
        # Process dependencies and merge with existing JSON
        local pkg_deps_json
        pkg_deps_json=$(process_package_dependencies "$update" "$update_dir")
        final_depends_json=$(echo "$final_depends_json" "$pkg_deps_json" | jq -s 'add')
    done
    echo "3"
    # Save dependencies
    echo "${final_depends_json}" | jq . > "${DEPENDS_JSON_PATH}"
    
    # Build packages
    for update in "${UPDATES[@]}"; do
        local subdirs
        local components
        subdirs=($(find "${GITHUB_WORKSPACE}" -maxdepth 3 -type d -name "${update}" -exec realpath --relative-to="${GITHUB_WORKSPACE}" {} +))
        local update_dir="${subdirs[0]}"
        components=($(extract_components "${update_dir}"))
        local maintain="${components[0]:-}"
        local build="${components[1]:-}"
        local package="${components[2]:-}"                  
        log_debug "maintain: $maintain update: $update build: $build"
        # TODO: we should have maintain passed here somehow
        if ! build_package "$update" "$build" "$GITHUB_WORKSPACE/$maintain/$build/$update"; then
            log_error "Build failed for ${update}"
            continue
        fi
    done
    
    log_debug "All updates processed successfully."
}

# Execute main function
main "$@"        