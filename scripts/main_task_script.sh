#!/usr/bin/env bash
set -euo pipefail
echo "::notice title=SCRIPT_START::Arch Package Update Task started."

# --- Helper Functions ---
_log_notice() { echo "::notice title=$1::$2"; }
_log_error() { echo "::error title=$1::$2"; }
_log_warning() { echo "::warning title=$1::$2"; }
_log_debug() { echo "::debug::$1"; }
_start_group() { echo "::group::$1"; }
_end_group() { echo "::endgroup::"; }
_log_notice "HELPER_DEF" "Helper functions defined."

# --- Configuration & Constants ---
_log_notice "VARS_CONSTANTS" "Defining constants..."
BUILDER_HOME="/home/builder"
NVCHECKER_RUN_DIR="${BUILDER_HOME}/nvchecker-run"
ARTIFACTS_DIR="${GITHUB_WORKSPACE}/artifacts"
PACKAGE_DETAILS_JSON_PATH="${NVCHECKER_RUN_DIR}/package_details.json"
_log_notice "VARS_CONSTANTS" "BUILDER_HOME=${BUILDER_HOME}, NVCHECKER_RUN_DIR=${NVCHECKER_RUN_DIR}, PACKAGE_DETAILS_JSON_PATH=${PACKAGE_DETAILS_JSON_PATH}"

# --- Function Definitions ---
# (setup_environment, generate_nvchecker_config, run_compare_aur_local_versions, run_version_checks, get_package_updates_list, extract_path_components, extract_pkgbuild_details, process_single_package_details)
# ... (Keep these functions exactly as they were in the last *working* version that produced the good JSON)
setup_environment() {
  _start_group "Setup Environment"
  _log_notice "SETUP_ENV" "Configuring environment in ${NVCHECKER_RUN_DIR}..."
  if ! sudo -u builder mkdir -p "${NVCHECKER_RUN_DIR}"; then
    _log_error "SETUP_FAIL" "mkdir NVCHECKER_RUN_DIR failed."
    _end_group
    return 1
  fi
  if ! mkdir -p "${ARTIFACTS_DIR}"; then
    _log_error "SETUP_FAIL" "mkdir ARTIFACTS_DIR failed."
    _end_group
    return 1
  fi
  if ! chown builder:builder "${ARTIFACTS_DIR}"; then
    _log_error "SETUP_FAIL" "chown builder:builder ARTIFACTS_DIR failed."
    _end_group
    return 1
  fi
  cd "${NVCHECKER_RUN_DIR}"
  _log_debug "Now in $(pwd)."
  local all_ok=true
  for script_to_copy in "buildscript2.py" "compare_aur_local_versions.py"; do # Used buildscript2.py
    local script_source_path
    script_source_path=$(find "${GITHUB_WORKSPACE}/scripts/" -name "${script_to_copy}" -type f -print -quit 2>/dev/null)
    if [ -n "${script_source_path}" ] && [ -f "${script_source_path}" ]; then
      if sudo -u builder cp "${script_source_path}" "./${script_to_copy}" && sudo -u builder chmod +x "./${script_to_copy}"; then
        _log_debug "Copied & chmodded ${script_to_copy}."
      else
        _log_error "SETUP_FAIL" "cp/chmod failed for ${script_to_copy}."
        all_ok=false
      fi
    else
      _log_error "SETUP_FAIL" "${script_to_copy} not found in ${GITHUB_WORKSPACE}/scripts/."
      all_ok=false
    fi
  done
  if ! ${all_ok}; then
    _log_error "SETUP_ENV_FINAL_FAIL" "Script setup failed."
    _end_group
    return 1
  fi
  _log_notice "SETUP_ENV" "Environment setup SUCCEEDED."
  _end_group
  return 0
}

generate_nvchecker_config() {
  _start_group "Generate NVChecker Configuration"
  _log_notice "NV_CONF" "Generating nvchecker config files..."
  local cfg="new.toml"
  local keyf="keyfile.toml"
  echo "[__config__]" >"${cfg}"
  echo "oldver = 'aur.json'" >>"${cfg}"
  echo "newver = 'local.json'" >>"${cfg}"
  echo "[keys]" >"${keyf}"
  echo "github = '${SECRET_GHUK_VALUE}'" >>"${keyf}"
  local individual_configs=()
  mapfile -t individual_configs < <(find "${GITHUB_WORKSPACE}" -path "*/maintain/build/*/.nvchecker.toml" -type f -print)
  if [ ${#individual_configs[@]} -gt 0 ]; then
    _log_debug "Appending ${#individual_configs[@]} individual .nvchecker.toml files."
    for cf in "${individual_configs[@]}"; do
      cat "${cf}" >>"${cfg}"
      echo "" >>"${cfg}"
    done
  else _log_warning "NV_CONF" "No individual .nvchecker.toml files found."; fi
  if ! chown builder:builder "${cfg}" "${keyf}"; then
    _log_error "NV_CONF_FAIL" "chown for ${cfg}/${keyf} FAILED. Exit: $?."
    _end_group
    return 1
  fi
  _log_notice "NV_CONF" "NVChecker configuration generated successfully."
  _end_group
  return 0
}

run_compare_aur_local_versions() {
  _start_group "Compare AUR vs Local Versions (Generates aur.json)"
  _log_notice "COMPARE_AUR" "Running compare_aur_local_versions.py..."
  local script="./compare_aur_local_versions.py"
  local outfile="aur.json"
  if [ ! -f "${script}" ]; then
    _log_error "COMPARE_AUR_FAIL" "${script} not found in $(pwd)!"
    _end_group
    return 1
  fi
  _log_debug "Executing: sudo -E -u builder python3 ${script} (args follow)"
  local stderr_log
  stderr_log=$(mktemp)
  if sudo -E -u builder python3 "${script}" --maintainer "${AUR_MAINTAINER_NAME}" --repo-root "${GITHUB_WORKSPACE}" 2>"${stderr_log}"; then
    if [ -s "${outfile}" ]; then
      _log_notice "COMPARE_AUR" "${outfile} generated (Size: $(wc -c <"${outfile}") bytes)."
      _log_debug "aur.json Head: $(head -n 5 "${outfile}")"
      if ! sudo -u builder chown builder:builder aur.json local.json changes.json combined.json 2>/dev/null; then _log_warning "COMPARE_AUR" "Could not chown one or more output files to builder."; fi
    else
      _log_warning "COMPARE_AUR" "${outfile} was expected but is empty/not found."
      if [ -s "${stderr_log}" ]; then _log_warning "COMPARE_AUR_PY_STDERR" "Stderr(exit 0): $(cat "${stderr_log}")"; fi
    fi
  else
    _log_error "COMPARE_AUR_FAIL" "Python script FAILED (Exit: $?). Stderr: $(cat "${stderr_log}")"
    rm "${stderr_log}"
    _end_group
    return 1
  fi
  rm "${stderr_log}"
  _log_notice "COMPARE_AUR" "compare_aur_local_versions.py SUCCEEDED."
  _end_group
  return 0
}

run_version_checks() {
  _start_group "Run Version Checks (nvchecker, nvcmp)"
  _log_notice "VER_CHECKS" "Running nvchecker & nvcmp..."
  if [ ! -f "new.toml" ] || [ ! -f "keyfile.toml" ] || [ ! -f "aur.json" ]; then
    _log_error "VER_CHECKS_FAIL" "Missing input files."
    _end_group
    return 1
  fi
  if sudo -E -u builder nvchecker -c new.toml -k keyfile.toml --logger json >local.json; then
    _log_notice "VER_CHECKS" "nvchecker OK. local.json size: $(wc -c <local.json) bytes."
    _log_debug "local.json Head: $(head -n 5 local.json)"
  else
    _log_error "VER_CHECKS_FAIL" "nvchecker FAILED (Exit: $?)."
    _end_group
    return 1
  fi
  if sudo -E -u builder nvcmp -c new.toml >changes.json; then
    _log_notice "VER_CHECKS" "nvcmp OK. changes.json size: $(wc -c <changes.json) bytes."
    _log_debug "changes.json Head: $(head -n 5 changes.json)"
  else
    _log_error "VER_CHECKS_FAIL" "nvcmp FAILED (Exit: $?)."
    _end_group
    return 1
  fi
  _log_notice "VER_CHECKS" "Version checks SUCCEEDED."
  _end_group
  return 0
}

get_package_updates_list() {
  _start_group "Get Package Updates List"
  _log_notice "GET_UPDATES" "Determining packages to update using nvcmp -q only..."
  if [ ! -f "new.toml" ]; then
    _log_error "GET_UPDATES_FAIL" "new.toml not found for nvcmp -q!"
    _end_group
    return 1
  fi
  local updates_from_nvcmp_q=()
  mapfile -t updates_from_nvcmp_q < <(sudo -E -u builder nvcmp -c new.toml -q 2>/dev/null || true)
  _log_debug "Found ${#updates_from_nvcmp_q[@]} updates from nvcmp -q."
  declare -ga UPDATES
  local temp_file_nvcmp_q
  temp_file_nvcmp_q=$(mktemp)
  if [ ${#updates_from_nvcmp_q[@]} -gt 0 ]; then printf '%s\n' "${updates_from_nvcmp_q[@]}" >"${temp_file_nvcmp_q}"; else >"${temp_file_nvcmp_q}"; fi
  mapfile -t UPDATES < <(sort -u "${temp_file_nvcmp_q}" | grep .)
  rm "${temp_file_nvcmp_q}"
  if [ ${#UPDATES[@]} -eq 0 ]; then
    _log_notice "GET_UPDATES" "No packages require updates based on nvcmp -q."
  else
    _log_notice "GET_UPDATES" "Found ${#UPDATES[@]} package(s) to update (from nvcmp -q):"
    printf '  ::notice title=Update Candidate (nvcmp -q)::%s\n' "${UPDATES[@]}"
  fi
  _log_notice "GET_UPDATES" "Package update list generation SUCCEEDED."
  _end_group
  return 0
}

extract_path_components() {
  echo "::debug::extract_path_components input: $1" >&2
  local path_to_parse="$1"
  local relative_path="${path_to_parse#${GITHUB_WORKSPACE}/}"
  relative_path="${relative_path%/}"
  relative_path="${relative_path#/}"
  IFS='/' read -r -a components_array <<<"${relative_path}"
  echo "::debug::extract_path_components output: ${components_array[*]}" >&2
  echo "${components_array[@]}"
}

extract_pkgbuild_details() {
  echo "::debug::extract_pkgbuild_details input (absolute PKGBUILD dir): $1" >&2
  local pkgbuild_dir_abs="$1"
  if [ ! -f "${pkgbuild_dir_abs}/PKGBUILD" ]; then
    echo "::error title=EXTRACT_FAIL::PKGBUILD missing in ${pkgbuild_dir_abs}" >&2
    return 1
  fi
  (
    CARCH="x86_64"
    PKGDEST="/tmp/pkgdest"
    SRCDEST="/tmp/srcdest"
    SRCPKGDEST="/tmp/srcpkgdest"
    source "${pkgbuild_dir_abs}/PKGBUILD"
    declare -p depends makedepends checkdepends source 2>/dev/null || true
  ) ||
    {
      echo "::error title=EXTRACT_FAIL::Sourcing/declaring PKGBUILD vars failed for ${pkgbuild_dir_abs}." >&2
      return 1
    }
}

process_single_package_details() {
  echo "::debug::process_single_package_details: pkg='$1', rel_dir='$2'" >&2
  local package_name="$1"
  local pkgbuild_dir_rel_to_workspace="$2"
  local declarations
  if ! declarations=$(extract_pkgbuild_details "${GITHUB_WORKSPACE}/${pkgbuild_dir_rel_to_workspace}"); then
    echo "::error title=PROCESS_FAIL::extract_pkgbuild_details failed for ${package_name}." >&2
    jq -n --arg pkg_name_arg "$package_name" '{($pkg_name_arg): {"error": "failed to extract PKGBUILD details"}}'
    return 1
  fi
  echo "::debug::Bash declarations for ${package_name} to be eval'd: ${declarations}" >&2
  unset depends makedepends checkdepends source
  eval "$declarations"
  local dep_j mak_j chk_j src_j
  if declare -p depends &>/dev/null && [ ${#depends[@]} -gt 0 ]; then dep_j=$(printf '%s\n' "${depends[@]}" | jq -R -s -c 'split("\n")[:-1]'); else dep_j="[]"; fi
  if declare -p makedepends &>/dev/null && [ ${#makedepends[@]} -gt 0 ]; then mak_j=$(printf '%s\n' "${makedepends[@]}" | jq -R -s -c 'split("\n")[:-1]'); else mak_j="[]"; fi
  if declare -p checkdepends &>/dev/null && [ ${#checkdepends[@]} -gt 0 ]; then chk_j=$(printf '%s\n' "${checkdepends[@]}" | jq -R -s -c 'split("\n")[:-1]'); else chk_j="[]"; fi
  if declare -p source &>/dev/null && [ ${#source[@]} -gt 0 ]; then src_j=$(printf '%s\n' "${source[@]}" | jq -R -s -c 'split("\n")[:-1]'); else src_j="[]"; fi
  echo "::debug::JSON arrays for ${package_name}: depends=${dep_j}, makedepends=${mak_j}, checkdepends=${chk_j}, sources=${src_j}" >&2
  jq -n --arg pkg "$package_name" --argjson d "$dep_j" --argjson m "$mak_j" --argjson c "$chk_j" --argjson s "$src_j" \
    '{($pkg): {depends: $d, makedepends: $m, checkdepends: $c, sources: $s}}'
}

# --- Function to call buildscript2.py ---
execute_build_script_py() {
  local package_name_arg="$1" # Renamed to avoid conflict with any global 'package_name'
  local build_type_arg="$2"
  local pkgbuild_path_rel_arg="$3"

  _start_group "Build Script Execution: ${package_name_arg}"
  # Current PWD should be NVCHECKER_RUN_DIR
  local package_specific_artifact_dir="${ARTIFACTS_DIR}/${package_name_arg}"
  if ! sudo -u builder mkdir -p "${package_specific_artifact_dir}"; then
    _log_error "BUILD_SCRIPT_MKDIR_FAIL" "Failed to create artifact subdir ${package_specific_artifact_dir} for ${package_name_arg}."
    _end_group
    return 1 # Critical failure for this package build
  fi

  _log_notice "BUILD_SCRIPT_PY_EXEC" "Starting buildscript2.py for package: ${package_name_arg} (Type: ${build_type_arg}, PKGBUILD Path: ${pkgbuild_path_rel_arg})"

  local build_script_executable_path="./buildscript2.py" # It's in NVCHECKER_RUN_DIR (current dir)
  local captured_build_output_json                       # Variable to store stdout
  local build_script_actual_exit_code=0

  # Temporarily redirect stderr to a file to capture it without 'set -x' pollution, then display if error
  local build_stderr_log
  build_stderr_log=$(mktemp)

  # Use a subshell for the command substitution to isolate its potential failure from `set -e` on the `if` line itself
  # if `sudo ... python3 ...` exits non-zero, command substitution yields empty string, and $? is set.
  if ! captured_build_output_json=$(sudo -E -u builder \
    python3 "${build_script_executable_path}" \
    --github-repo "${GITHUB_REPOSITORY}" \
    --github-token "${GH_TOKEN}" \
    --github-workspace "${GITHUB_WORKSPACE}" \
    --package-name "${package_name_arg}" \
    --depends-json "${PACKAGE_DETAILS_JSON_PATH}" \
    --pkgbuild-path "${pkgbuild_path_rel_arg}" \
    --commit-message "CI: Auto update ${package_name_arg}" \
    --build-mode "${build_type_arg}" \
    --artifacts-dir "${package_specific_artifact_dir}" \
    --debug 2>"${build_stderr_log}"); then
    build_script_actual_exit_code=$? # Capture exit code of the sudo command
    _log_error "BUILD_SCRIPT_PY_FAIL" "buildscript2.py for '${package_name_arg}' FAILED with exit code ${build_script_actual_exit_code}."
  else
    _log_debug "buildscript2.py for '${package_name_arg}' apparently succeeded (exit code 0). STDOUT (JSON): ${captured_build_output_json}"
  fi

  # Log stderr from buildscript2.py regardless of exit code, if not empty
  if [ -s "${build_stderr_log}" ]; then
    _log_warning "BUILD_SCRIPT_PY_STDERR" "Stderr from buildscript2.py for ${package_name_arg}:"
    cat "${build_stderr_log}" | while IFS= read -r line; do _log_warning "PY_STDERR" "  $line"; done
  fi
  rm "${build_stderr_log}"

  # Parse JSON output from buildscript2.py for summary
  local bs_version bs_success bs_changes_detected bs_error_msg # 'local' is fine in function
  bs_version=$(echo "${captured_build_output_json}" | jq -r '.version // "N/A"')
  bs_success=$(echo "${captured_build_output_json}" | jq -r '.success')
  bs_changes_detected=$(echo "${captured_build_output_json}" | jq -r '.changes_detected')
  bs_error_msg=$(echo "${captured_build_output_json}" | jq -r '.error_message // ""')

  if [[ -z "$bs_success" || "$bs_success" == "null" ]]; then
    # This can happen if captured_build_output_json is empty (e.g. script failed before printing JSON)
    _log_error "BUILD_SCRIPT_JSON_ERR" "Failed to parse JSON from buildscript2.py for ${package_name_arg} or output invalid. Assuming failure."
    bs_success="false"
    bs_error_msg="${bs_error_msg:-buildscript2.py produced invalid/no JSON. Script exit code was ${build_script_actual_exit_code}.}"
  fi

  local status_md="❌ Failure"
  if [ "$bs_success" = "true" ]; then status_md="✅ Success"; fi
  if [ "$bs_success" = "false" ] && [ -n "$bs_error_msg" ] && [ "$bs_error_msg" != "null" ]; then
    status_md="${status_md}: <small>$(echo "$bs_error_msg" | sed 's/|/\\|/g; s/\n/<br>/g')</small>"
  fi
  local changes_md="➖ No"
  if [ "$bs_changes_detected" = "true" ]; then changes_md="✔️ Yes"; fi
  local aur_link_md="[AUR](https://aur.archlinux.org/packages/${package_name_arg})"
  local log_link_md="See 'build-artifacts-${GITHUB_RUNID}' artifact (<tt>${package_name_arg}/</tt> subdir)"
  if ! compgen -G "${package_specific_artifact_dir}/${package_name_arg}-*.log" >/dev/null; then log_link_md="N/A"; fi

  echo "| **${package_name_arg}** | ${bs_version} | ${status_md} | ${changes_md} | ${aur_link_md} | ${log_link_md} |" >>"$GITHUB_STEP_SUMMARY"
  _end_group
  [ "$bs_success" = "true" ] # Return true if buildscript2.py reported success
}

# --- Main Execution Flow ---
_log_notice "MAIN_EXEC" "Starting main execution flow..."

echo "## Arch Package Build Summary" >"$GITHUB_STEP_SUMMARY"
echo "| Package | Version | Status | Changes | AUR Link | Build Logs |" >>"$GITHUB_STEP_SUMMARY"
echo "|---|---|---|---|---|---|" >>"$GITHUB_STEP_SUMMARY"

if ! setup_environment; then
  _log_error "MAIN_FAIL" "setup_environment FAILED."
  exit 1
fi
if ! generate_nvchecker_config; then
  _log_error "MAIN_FAIL" "generate_nvchecker_config FAILED."
  exit 1
fi
if ! run_compare_aur_local_versions; then
  _log_error "MAIN_FAIL" "run_compare_aur_local_versions FAILED."
  exit 1
fi
if ! run_version_checks; then
  _log_error "MAIN_FAIL" "run_version_checks FAILED."
  exit 1
fi
if ! get_package_updates_list; then
  _log_error "MAIN_FAIL" "get_package_updates_list FAILED."
  exit 1
fi

_log_notice "MAIN_EXEC" "Pre-build data generation completed. UPDATES count: ${#UPDATES[@]}"
if [ ${#UPDATES[@]} -eq 0 ]; then
  _log_notice "MAIN_EXEC_NO_UPDATES" "No updates to process. Exiting."
  echo "| *No updates found* | - | - | - | - | - |" >>"$GITHUB_STEP_SUMMARY"
  exit 0
fi

_start_group "Process PKGBUILD Details for Updated Packages"
_log_notice "JSON_GEN" "Processing PKGBUILD details for ${#UPDATES[@]} updated package(s)..."
combined_pkg_details_json="{}"
for package_to_process in "${UPDATES[@]}"; do
  _start_group "Processing Details: [${package_to_process}]"
  if [ -z "${package_to_process}" ]; then
    _log_warning "JSON_GEN_PKG_EMPTY" "Empty pkg name. Skipping."
    _end_group
    continue
  fi
  pkg_build_dirs_found=()
  mapfile -t pkg_build_dirs_found < <(find "${GITHUB_WORKSPACE}" -path "*/maintain/build/${package_to_process}" -type d -exec test -f "{}/PKGBUILD" \; -print)
  if [ ${#pkg_build_dirs_found[@]} -ne 1 ]; then
    _log_warning "JSON_GEN_PKG_FIND_ERR" "PKGBUILD dir for '${package_to_process}' error (Found: ${#pkg_build_dirs_found[@]})."
    _end_group
    continue
  fi
  package_dir_abs="${pkg_build_dirs_found[0]}"
  package_dir_rel_to_workspace="${package_dir_abs#${GITHUB_WORKSPACE}/}"
  single_package_json_fragment=""
  process_details_succeeded=true
  if ! single_package_json_fragment=$(process_single_package_details "$package_to_process" "$package_dir_rel_to_workspace"); then
    _log_warning "JSON_GEN_PKG_PROCESS_FAIL" "process_single_package_details issues for ${package_to_process}."
    process_details_succeeded=false
  fi
  _log_debug "JSON fragment for ${package_to_process}: ${single_package_json_fragment}"
  temp_merged_json=""
  if temp_merged_json=$(jq -n --argjson current "$combined_pkg_details_json" --argjson fragment "$single_package_json_fragment" '$current + $fragment'); then
    combined_pkg_details_json="$temp_merged_json"
    if ! $process_details_succeeded; then _log_warning "JSON_GEN_PKG_MERGED_ERR" "Merged error JSON for ${package_to_process}."; fi
  else _log_error "JSON_GEN_PKG_MERGE_FAIL" "jq merge FAILED for ${package_to_process}."; fi
  _end_group
done
_log_notice "JSON_GEN_LOOP_END" "Finished loop for PKGBUILD details."
echo "${combined_pkg_details_json}" | jq . >"${PACKAGE_DETAILS_JSON_PATH}"
if ! chown builder:builder "${PACKAGE_DETAILS_JSON_PATH}"; then _log_warning "JSON_GEN_CHOWN_FAIL" "Failed to chown ${PACKAGE_DETAILS_JSON_PATH}."; fi
_log_notice "JSON_GEN_SUMMARY" "${PACKAGE_DETAILS_JSON_PATH} (Size: $(wc -c <"${PACKAGE_DETAILS_JSON_PATH}") bytes) created."
_log_debug "Full content of ${PACKAGE_DETAILS_JSON_PATH}:"
cat "${PACKAGE_DETAILS_JSON_PATH}" | while IFS= read -r line; do _log_debug "  $line"; done
_end_group

# Build packages
_start_group "Build Packages"
_log_notice "BUILD_LOOP" "Starting build loop for ${#UPDATES[@]} package(s)..."
overall_build_success=true
for package_to_build in "${UPDATES[@]}"; do
  if [ -z "${package_to_build}" ]; then
    _log_warning "BUILD_LOOP_SKIP_EMPTY" "Skipping empty package name."
    continue
  fi

  pkg_build_dirs_found_for_build=()
  mapfile -t pkg_build_dirs_found_for_build < <(find "${GITHUB_WORKSPACE}" -path "*/maintain/build/${package_to_build}" -type d -exec test -f "{}/PKGBUILD" \; -print)
  if [ ${#pkg_build_dirs_found_for_build[@]} -ne 1 ]; then
    _log_error "BUILD_LOOP_FIND_ERR" "PKGBUILD dir error for build: '${package_to_build}' (Found: ${#pkg_build_dirs_found_for_build[@]})."
    echo "| **${package_to_build}** | N/A | ❌ Error: PKGBUILD dir not found for build | - | - | N/A |" >>"$GITHUB_STEP_SUMMARY"
    overall_build_success=false
    continue
  fi
  package_dir_abs_for_build="${pkg_build_dirs_found_for_build[0]}"
  package_dir_rel_to_workspace_for_build="${package_dir_abs_for_build#${GITHUB_WORKSPACE}/}"

  path_components_for_build=()
  path_components_for_build=($(extract_path_components "${package_dir_rel_to_workspace_for_build}"))
  build_mode_from_dir_for_build="nobuild"
  if [ "${#path_components_for_build[@]}" -ge 2 ] && ([[ "${path_components_for_build[1]}" == "build" || "${path_components_for_build[1]}" == "test" ]]); then
    build_mode_from_dir_for_build="${path_components_for_build[1]}"
  else _log_warning "BUILD_LOOP_MODE_WARN" "Cannot determine build_mode for ${package_to_build} from path ${package_dir_rel_to_workspace_for_build}. Defaulting to 'nobuild'."; fi
  _log_debug "Package: ${package_to_build}, Determined build_mode: ${build_mode_from_dir_for_build}"

  if ! execute_build_script_py "$package_to_build" "$build_mode_from_dir_for_build" "$package_dir_rel_to_workspace_for_build"; then
    _log_error "BUILD_LOOP_SCRIPT_FAIL" "Build script execution FAILED for ${package_to_build}."
    overall_build_success=false
  fi
done
_log_notice "BUILD_LOOP" "Finished build loop."
_end_group

if ! $overall_build_success; then
  _log_error "MAIN_EXEC_BUILD_FAIL" "One or more packages failed to build. See summary and individual build logs."
  exit 1
fi
_log_notice "MAIN_EXEC" "All tasks completed successfully."
