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
# New: Base directory for buildscript2.py to create its temporary package build directories
PACKAGE_BUILD_BASE_DIR="${BUILDER_HOME}/pkg_builds"
ARTIFACTS_DIR="${GITHUB_WORKSPACE}/artifacts"
PACKAGE_DETAILS_JSON_PATH="${NVCHECKER_RUN_DIR}/package_details.json"
_log_notice "VARS_CONSTANTS" "BUILDER_HOME=${BUILDER_HOME}, NVCHECKER_RUN_DIR=${NVCHECKER_RUN_DIR}, PACKAGE_BUILD_BASE_DIR=${PACKAGE_BUILD_BASE_DIR}, PACKAGE_DETAILS_JSON_PATH=${PACKAGE_DETAILS_JSON_PATH}"

# Git author information (can be overridden by secrets/env variables)
GIT_COMMIT_USER_NAME="${GIT_USER_NAME:-GitHub Actions CI}"
GIT_COMMIT_USER_EMAIL="${GIT_USER_EMAIL:-actions@github.com}"

# --- Function Definitions ---
# (setup_environment, generate_nvchecker_config, run_compare_aur_local_versions, run_version_checks, get_package_updates_list, extract_path_components, extract_pkgbuild_details, process_single_package_details)
setup_environment() {
  _start_group "Setup Environment"
  _log_notice "SETUP_ENV" "Configuring environment in ${NVCHECKER_RUN_DIR} and ${PACKAGE_BUILD_BASE_DIR}..."

  # Create and set permissions for NVCHECKER_RUN_DIR
  if ! sudo -u builder mkdir -p "${NVCHECKER_RUN_DIR}"; then
    _log_error "SETUP_FAIL" "mkdir NVCHECKER_RUN_DIR failed."
    _end_group
    return 1
  fi
  # New: Create and set permissions for PACKAGE_BUILD_BASE_DIR
  if ! sudo -u builder mkdir -p "${PACKAGE_BUILD_BASE_DIR}"; then
    _log_error "SETUP_FAIL" "mkdir PACKAGE_BUILD_BASE_DIR failed."
    _end_group
    return 1
  fi
  if ! sudo chown -R builder:builder "${NVCHECKER_RUN_DIR}" "${PACKAGE_BUILD_BASE_DIR}"; then
    _log_error "SETUP_FAIL" "chown builder:builder for NVCHECKER_RUN_DIR or PACKAGE_BUILD_BASE_DIR failed."
    _end_group
    return 1
  fi

  # Create and set permissions for ARTIFACTS_DIR (owned by GHA runner user, then chown to builder)
  if ! mkdir -p "${ARTIFACTS_DIR}"; then
    _log_error "SETUP_FAIL" "mkdir ARTIFACTS_DIR failed."
    _end_group
    return 1
  fi
  if ! chown builder:builder "${ARTIFACTS_DIR}"; then # This chown might be problematic if GHA runner needs to write here later directly
    _log_warning "SETUP_CHOWN_WARN" "chown ARTIFACTS_DIR to builder failed. This might be ok."
    # Continue, as builder might only need to write *into* subdirs created by buildscript2.py
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
  # NVCHECKER_RUN_DIR is current working directory and owned by builder.
  # Files new.toml, keyfile.toml will be created here.
  _log_notice "NV_CONF" "Generating nvchecker config files in $(pwd)..."
  local cfg="new.toml"
  local keyf="keyfile.toml"

  # These files are created by the user running this script (e.g., root or runner user)
  # in the NVCHECKER_RUN_DIR.
  echo "[__config__]" >"${cfg}"
  echo "oldver = 'aur.json'" >>"${cfg}"
  echo "newver = 'local.json'" >>"${cfg}"
  echo "[keys]" >"${keyf}"
  echo "github = '${SECRET_GHUK_VALUE}'" >>"${keyf}" # SECRET_GHUK_VALUE must be set in env

  local individual_configs=()
  # find runs as the current script user. GITHUB_WORKSPACE should be readable.
  mapfile -t individual_configs < <(find "${GITHUB_WORKSPACE}" -path "*/maintain/build/*/.nvchecker.toml" -type f -print)

  if [ ${#individual_configs[@]} -gt 0 ]; then
    _log_debug "Appending ${#individual_configs[@]} individual .nvchecker.toml files."
    for cf_item_loop_var in "${individual_configs[@]}"; do # Renamed loop variable
      # cat runs as current script user, appending to $cfg (owned by current script user)
      cat "${cf_item_loop_var}" >>"${cfg}"
      echo "" >>"${cfg}"
    done
  else
    _log_warning "NV_CONF" "No individual .nvchecker.toml files found."
  fi

  # Chown these files (currently owned by runner_user) to 'builder:builder'.
  # This chown must be done by a privileged user (root via sudo).
  _log_debug "Attempting to chown ${cfg} and ${keyf} to builder:builder using privileged sudo..."
  if ! sudo chown builder:builder "${cfg}" "${keyf}"; then # MODIFIED LINE
    _log_error "NV_CONF_FAIL" "sudo chown builder:builder for ${cfg}/${keyf} FAILED. Exit: $?."
    _end_group
    return 1
  fi
  _log_debug "Successfully chowned ${cfg} and ${keyf} to builder:builder."

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
  _log_debug "Executing: sudo -E -u builder HOME=${BUILDER_HOME} python3 ${script} (args follow)"
  local stderr_log
  stderr_log=$(mktemp)
  # Ensure HOME is set for the builder user context
  if sudo -E -u builder HOME="${BUILDER_HOME}" python3 "${script}" --maintainer "${AUR_MAINTAINER_NAME}" --repo-root "${GITHUB_WORKSPACE}" 2>"${stderr_log}"; then
    if [ -s "${outfile}" ]; then
      _log_notice "COMPARE_AUR" "${outfile} generated (Size: $(wc -c <"${outfile}") bytes)."
      _log_debug "aur.json Head: $(head -n 5 "${outfile}")"
      # Chown output files to builder. These are in NVCHECKER_RUN_DIR.
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
    _log_error "VER_CHECKS_FAIL" "Missing input files (new.toml, keyfile.toml, or aur.json)."
    _end_group
    return 1
  fi
  # Run as builder, HOME set. nvchecker/nvcmp run from NVCHECKER_RUN_DIR.
  if sudo -E -u builder HOME="${BUILDER_HOME}" nvchecker -c new.toml -k keyfile.toml --logger json >local.json; then
    _log_notice "VER_CHECKS" "nvchecker OK. local.json size: $(wc -c <local.json) bytes."
    _log_debug "local.json Head: $(head -n 5 local.json)"
  else
    _log_error "VER_CHECKS_FAIL" "nvchecker FAILED (Exit: $?)."
    _end_group
    return 1
  fi
  if sudo -E -u builder HOME="${BUILDER_HOME}" nvcmp -c new.toml >changes.json; then
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
  # Run as builder, HOME set.
  mapfile -t updates_from_nvcmp_q < <(sudo -E -u builder HOME="${BUILDER_HOME}" nvcmp -c new.toml -q 2>/dev/null || true)
  _log_debug "Found ${#updates_from_nvcmp_q[@]} updates from nvcmp -q."
  declare -ga UPDATES # Global array
  local temp_file_nvcmp_q
  temp_file_nvcmp_q=$(mktemp)
  if [ ${#updates_from_nvcmp_q[@]} -gt 0 ]; then printf '%s\n' "${updates_from_nvcmp_q[@]}" >"${temp_file_nvcmp_q}"; else >"${temp_file_nvcmp_q}"; fi
  mapfile -t UPDATES < <(sort -u "${temp_file_nvcmp_q}" | grep .) # grep . to remove empty lines
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
  _log_debug "extract_path_components input: $1"
  local path_to_parse="$1"
  local relative_path="${path_to_parse#${GITHUB_WORKSPACE}/}"
  relative_path="${relative_path%/}"
  relative_path="${relative_path#/}"
  IFS='/' read -r -a components_array <<<"${relative_path}"
  _log_debug "extract_path_components output: ${components_array[*]}"
  echo "${components_array[@]}"
}

extract_pkgbuild_details() {
  # Ensure internal echos go to stderr to not pollute $declarations
  _log_debug "extract_pkgbuild_details input (absolute PKGBUILD dir): $1" >&2
  local pkgbuild_dir_abs="$1"
  if [ ! -f "${pkgbuild_dir_abs}/PKGBUILD" ]; then
    _log_error "EXTRACT_FAIL" "PKGBUILD missing in ${pkgbuild_dir_abs}" >&2
    return 1
  fi

  local declarations
  # The subshell here captures stdout. All echos inside it meant for debugging MUST go to stderr.
  declarations=$(
    sudo -E -u builder HOME="${BUILDER_HOME}" bash -c '
      # This sub-bash script should only print `declare -p` lines to its stdout.
      # Debug messages within this sub-bash script should go to its stderr.
      # echo "Sub-bash script starting for PKGBUILD sourcing." >&2 # Example debug to stderr
      set -euo pipefail
      CARCH="x86_64"
      PKGDEST="/tmp/pkgdest" 
      SRCDEST="/tmp/srcdest"
      SRCPKGDEST="/tmp/srcpkgdest"
      
      # Source the PKGBUILD. Errors during source will cause sub-bash to exit non-zero due to set -e.
      source "'"${pkgbuild_dir_abs}/PKGBUILD"'"
      
      # Capture `declare -p` output for relevant arrays.
      # If an array is not set, `declare -p` for it will fail, set -e handles this.
      # To avoid failure for unset arrays, check if they are set first.
      declare_output=""
      if declare -p depends &>/dev/null; then declare_output+="$(declare -p depends);"; fi
      if declare -p makedepends &>/dev/null; then declare_output+="$(declare -p makedepends);"; fi
      if declare -p checkdepends &>/dev/null; then declare_output+="$(declare -p checkdepends);"; fi
      if declare -p source &>/dev/null; then declare_output+="$(declare -p source);"; fi
      
      # This echo is the *only* thing that should go to sub-bash stdout.
      echo "$declare_output"
    '
  )
  local sub_bash_exit_code=$? # Capture exit code of the sudo bash -c command

  if [ $sub_bash_exit_code -ne 0 ]; then
    _log_error "EXTRACT_FAIL" "Sourcing PKGBUILD or declaring vars failed for ${pkgbuild_dir_abs}. Sub-bash exit code: ${sub_bash_exit_code}." >&2
    # $declarations might contain partial output or error messages from the sub-bash stderr if not careful
    _log_debug "Content of \$declarations on failure: ${declarations}" >&2
    return 1
  fi
  if [ -z "$declarations" ]; then
    # This can happen if all arrays (depends, makedepends, etc.) are unset/empty in the PKGBUILD.
    # This is not necessarily an error for extraction itself, but process_single_package_details will produce empty arrays.
    _log_warning "EXTRACT_WARN" "No relevant array variables (depends, makedepends, checkdepends, source) found or declared in ${pkgbuild_dir_abs}/PKGBUILD." >&2
    # Return success, but $declarations is empty. process_single_package_details will handle empty eval.
  fi

  echo "$declarations" # This is the stdout of extract_pkgbuild_details, goes to the calling function.
}
# In main.txt
process_single_package_details() {
  # This debug line correctly goes to stderr
  _log_debug "process_single_package_details: pkg='$1', rel_dir='$2'" >&2
  local package_name="$1"
  local pkgbuild_dir_rel_to_workspace="$2"
  local declarations
  if ! declarations=$(extract_pkgbuild_details "${GITHUB_WORKSPACE}/${pkgbuild_dir_rel_to_workspace}"); then
    # This warning correctly goes to stderr
    _log_warning "PROCESS_FAIL" "extract_pkgbuild_details failed for ${package_name}." >&2
    # This jq output is the stdout of this function branch, which is correct.
    jq -n --arg pkg_name_arg "$package_name" '{($pkg_name_arg): {"error": "failed to extract PKGBUILD details"}}'
    return 1
  fi

  # MODIFIED LINE: Ensure this debug output goes to stderr
  _log_debug "Bash declarations for ${package_name} to be eval'd: ${declarations}" >&2

  unset depends makedepends checkdepends source # Ensure clean slate
  eval "$declarations"                          # This evals the 'declare -p' output. Errors here will be caught by set -e.

  local dep_j mak_j chk_j src_j
  # Safely create JSON arrays, handling cases where arrays might be unset or empty after eval
  dep_j=$(declare -p depends &>/dev/null && [ ${#depends[@]} -gt 0 ] && printf '%s\n' "${depends[@]}" | jq -R -s -c 'split("\n")[:-1]' || echo "[]")
  mak_j=$(declare -p makedepends &>/dev/null && [ ${#makedepends[@]} -gt 0 ] && printf '%s\n' "${makedepends[@]}" | jq -R -s -c 'split("\n")[:-1]' || echo "[]")
  chk_j=$(declare -p checkdepends &>/dev/null && [ ${#checkdepends[@]} -gt 0 ] && printf '%s\n' "${checkdepends[@]}" | jq -R -s -c 'split("\n")[:-1]' || echo "[]")
  src_j=$(declare -p source &>/dev/null && [ ${#source[@]} -gt 0 ] && printf '%s\n' "${source[@]}" | jq -R -s -c 'split("\n")[:-1]' || echo "[]")

  # MODIFIED LINE: Ensure this debug output also goes to stderr
  _log_debug "JSON arrays for ${package_name}: depends=${dep_j}, makedepends=${mak_j}, checkdepends=${chk_j}, sources=${src_j}" >&2

  # This final jq command is the *intended stdout* of this function, which will be captured.
  jq -n --arg pkg "$package_name" --argjson d "$dep_j" --argjson m "$mak_j" --argjson c "$chk_j" --argjson s "$src_j" \
    '{($pkg): {depends: $d, makedepends: $m, checkdepends: $c, sources: $s}}'
}

# --- Function to call buildscript2.py ---
execute_build_script_py() {
  local package_name_arg="$1"
  local build_type_arg="$2"
  local pkgbuild_path_rel_arg="$3"

  _start_group "Build Script Execution: ${package_name_arg}"
  local package_specific_artifact_dir="${ARTIFACTS_DIR}/${package_name_arg}"
  # Create artifact subdir as builder
  if ! sudo -u builder HOME="${BUILDER_HOME}" mkdir -p "${package_specific_artifact_dir}"; then
    _log_error "BUILD_SCRIPT_MKDIR_FAIL" "Failed to create artifact subdir ${package_specific_artifact_dir} for ${package_name_arg}."
    _end_group
    return 1
  fi

  _log_notice "BUILD_SCRIPT_PY_EXEC" "Starting buildscript2.py for package: ${package_name_arg} (Type: ${build_type_arg}, PKGBUILD Path: ${pkgbuild_path_rel_arg})"

  # Configure git for the builder user (important for buildscript2.py's git operations)
  _log_notice "GIT_CONFIG" "Configuring git for builder user (${BUILDER_HOME})..."
  if ! sudo -E -u builder HOME="${BUILDER_HOME}" git config --global user.name "${GIT_COMMIT_USER_NAME}"; then
    _log_warning "GIT_CONFIG_FAIL" "Failed to set git user.name for builder."
    # Continue, but buildscript might fail later if it needs to commit
  fi
  if ! sudo -E -u builder HOME="${BUILDER_HOME}" git config --global user.email "${GIT_COMMIT_USER_EMAIL}"; then
    _log_warning "GIT_CONFIG_FAIL" "Failed to set git user.email for builder."
  fi
  # Display git config for builder user for verification
  _log_debug "Builder git config user.name: $(sudo -E -u builder HOME="${BUILDER_HOME}" git config --global user.name || echo 'not set')"
  _log_debug "Builder git config user.email: $(sudo -E -u builder HOME="${BUILDER_HOME}" git config --global user.email || echo 'not set')"

  local build_script_executable_path="./buildscript2.py"
  local captured_build_output_json=""
  local python_cmd_exit_status=0

  local build_stderr_log
  build_stderr_log=$(mktemp)

  # Execute python script as builder, ensuring HOME is set correctly.
  # Pass PACKAGE_BUILD_BASE_DIR for buildscript2.py to use.
  if ! captured_build_output_json=$(sudo -E -u builder \
    HOME="${BUILDER_HOME}" \
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
    --base-build-dir "${PACKAGE_BUILD_BASE_DIR}" \
    --debug 2>"${build_stderr_log}"); then
    python_cmd_exit_status=$?
    _log_error "BUILD_SCRIPT_PY_FAIL" "buildscript2.py for '${package_name_arg}' FAILED. Command exit status: ${python_cmd_exit_status}."
  else
    python_cmd_exit_status=0
    _log_debug "buildscript2.py for '${package_name_arg}' command apparently succeeded. Command exit status: ${python_cmd_exit_status}."
  fi

  _log_debug "Raw output from buildscript2.py (stdout): '${captured_build_output_json}'"

  if [ -s "${build_stderr_log}" ]; then
    _log_warning "BUILD_SCRIPT_PY_STDERR" "Stderr from buildscript2.py for ${package_name_arg}:"
    while IFS= read -r line; do _log_warning "PY_STDERR_LINE" "  $line"; done <"${build_stderr_log}"
  else
    _log_debug "Stderr from buildscript2.py for ${package_name_arg} was empty."
  fi
  rm "${build_stderr_log}"

  local bs_version bs_success bs_changes_detected bs_error_msg

  if [ -z "${captured_build_output_json}" ] && [ "${python_cmd_exit_status}" -ne 0 ]; then
    _log_warning "BUILD_SCRIPT_JSON_EMPTY" "stdout from buildscript2.py was empty for ${package_name_arg}, and script exited non-zero."
    # In this case, jq parsing will fail, and bs_success will not be 'true'.
  elif [ -z "${captured_build_output_json}" ]; then
    _log_warning "BUILD_SCRIPT_JSON_EMPTY" "stdout from buildscript2.py was empty for ${package_name_arg}, but script exited zero. This is unexpected if JSON output is required."
  fi

  bs_version=$(echo "${captured_build_output_json}" | jq -r '.version // "N/A"')
  jq_exit_status_version=$?
  bs_success=$(echo "${captured_build_output_json}" | jq -r '.success')
  jq_exit_status_success=$?
  bs_changes_detected=$(echo "${captured_build_output_json}" | jq -r '.changes_detected')
  jq_exit_status_changes=$?
  bs_error_msg=$(echo "${captured_build_output_json}" | jq -r '.error_message // ""')
  jq_exit_status_error_msg=$?

  # Consolidate jq parsing failure checks
  if [ "$jq_exit_status_version" -ne 0 ] || [ "$jq_exit_status_success" -ne 0 ] ||
    [ "$jq_exit_status_changes" -ne 0 ] || [ "$jq_exit_status_error_msg" -ne 0 ]; then
    _log_warning "BUILD_SCRIPT_JQ_FAIL" "One or more jq commands failed to parse output from buildscript2.py for ${package_name_arg}. Raw output was: '${captured_build_output_json}'"
    # If jq failed, we cannot trust bs_success, so force it to "false" unless python_cmd_exit_status is 0 AND json was truly empty but meant success (unlikely)
    if [ -n "${captured_build_output_json}" ]; then # If there was output, but jq failed, it's definitely a JSON issue
      bs_success="false"
      if [ -z "${bs_error_msg}" ] || [ "${bs_error_msg}" == "null" ]; then # Provide a jq-specific error
        bs_error_msg="Failed to parse JSON output from buildscript2.py. Python command exit status: ${python_cmd_exit_status}."
      fi
    fi
  fi

  # Final check for success: python script must exit 0 AND report "success": true in JSON
  if [ "${python_cmd_exit_status}" -ne 0 ] || [ "$bs_success" != "true" ]; then
    if [ "$bs_success" != "true" ] && [ "$bs_success" != "false" ] && [ -n "${captured_build_output_json}" ]; then
      _log_error "BUILD_SCRIPT_JSON_ERR" "Parsed 'success' field from buildscript2.py was not 'true' or 'false' ('${bs_success}'). Assuming failure."
    elif [ "${python_cmd_exit_status}" -ne 0 ] && [ "$bs_success" != "true" ]; then
      _log_warning "BUILD_SCRIPT_STATUS_NOTE" "Python command failed (status: ${python_cmd_exit_status}) or reported success was not 'true' (is: '${bs_success}')."
    fi

    bs_success="false"

    if [ -z "${bs_error_msg}" ] || [ "${bs_error_msg}" == "null" ]; then
      bs_error_msg="buildscript2.py command failed (exit status ${python_cmd_exit_status}) or did not report success in JSON."
    fi
  fi

  local status_md="❌ Failure"
  if [ "$bs_success" = "true" ]; then status_md="✅ Success"; fi

  if [ "$bs_success" = "false" ] && [ -n "$bs_error_msg" ] && [ "$bs_error_msg" != "null" ]; then
    sanitized_error_msg=$(echo "$bs_error_msg" | sed 's/|/\\|/g; s/\n/<br>/g')
    status_md="${status_md}: <small>${sanitized_error_msg}</small>"
  fi

  local changes_md="➖ No"
  if [ "$bs_changes_detected" = "true" ]; then changes_md="✔️ Yes"; fi
  local aur_link_md="[AUR](https://aur.archlinux.org/packages/${package_name_arg})"
  local log_link_md="See 'build-artifacts-${GITHUB_RUN_ID}' artifact (<tt>${package_name_arg}/</tt> subdir)"
  # Check if any log file exists in the specific artifact directory for this package
  if ! compgen -G "${package_specific_artifact_dir}/${package_name_arg}*.log" >/dev/null &&
    ! compgen -G "${package_specific_artifact_dir}/*.log" >/dev/null; then
    log_link_md="N/A"
  fi

  echo "| **${package_name_arg}** | ${bs_version} | ${status_md} | ${changes_md} | ${aur_link_md} | ${log_link_md} |" >>"$GITHUB_STEP_SUMMARY"
  _end_group
  [ "$bs_success" = "true" ]
}

# --- Main Execution Flow ---
_log_notice "MAIN_EXEC" "Starting main execution flow..."

echo "## Arch Package Build Summary" >"$GITHUB_STEP_SUMMARY"
echo "| Package | Version | Status | Changes | AUR Link | Build Logs |" >>"$GITHUB_STEP_SUMMARY"
echo "|---|---|---|---|---|---|" >>"$GITHUB_STEP_SUMMARY"

if ! setup_environment; then
  _log_error "MAIN_FAIL" "setup_environment FAILED."
  echo "| **SETUP** | N/A | ❌ Failure: Environment setup | - | - | Check Action Logs |" >>"$GITHUB_STEP_SUMMARY"
  exit 1
fi
if ! generate_nvchecker_config; then
  _log_error "MAIN_FAIL" "generate_nvchecker_config FAILED."
  echo "| **SETUP** | N/A | ❌ Failure: NVChecker config | - | - | Check Action Logs |" >>"$GITHUB_STEP_SUMMARY"
  exit 1
fi
# Ensure GITHUB_WORKSPACE and related dirs are accessible by builder where needed
# The scripts are in NVCHECKER_RUN_DIR owned by builder.
# GITHUB_WORKSPACE is the source repo, should be readable by builder.
if ! run_compare_aur_local_versions; then
  _log_error "MAIN_FAIL" "run_compare_aur_local_versions FAILED."
  echo "| **SETUP** | N/A | ❌ Failure: AUR/Local Compare | - | - | Check Action Logs |" >>"$GITHUB_STEP_SUMMARY"
  exit 1
fi
if ! run_version_checks; then
  _log_error "MAIN_FAIL" "run_version_checks FAILED."
  echo "| **SETUP** | N/A | ❌ Failure: Version Checks | - | - | Check Action Logs |" >>"$GITHUB_STEP_SUMMARY"
  exit 1
fi
if ! get_package_updates_list; then
  _log_error "MAIN_FAIL" "get_package_updates_list FAILED."
  echo "| **SETUP** | N/A | ❌ Failure: Get Updates List | - | - | Check Action Logs |" >>"$GITHUB_STEP_SUMMARY"
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
all_pkgbuild_details_extracted_successfully=true
for package_to_process in "${UPDATES[@]}"; do
  _start_group "Processing Details: [${package_to_process}]"
  if [ -z "${package_to_process}" ]; then
    _log_warning "JSON_GEN_PKG_EMPTY" "Empty pkg name. Skipping."
    _end_group
    continue
  fi
  pkg_build_dirs_found=()
  # Find should search from GITHUB_WORKSPACE, ensure paths are correct
  mapfile -t pkg_build_dirs_found < <(find "${GITHUB_WORKSPACE}" -path "*/maintain/build/${package_to_process}" -type d -print -quit)

  if [ ${#pkg_build_dirs_found[@]} -ne 1 ]; then # Exactly one PKGBUILD dir per package name
    _log_warning "JSON_GEN_PKG_FIND_ERR" "PKGBUILD dir for '${package_to_process}' error (Found: ${#pkg_build_dirs_found[@]}). Searched in ${GITHUB_WORKSPACE}/maintain/build/${package_to_process}"
    # Add error entry to JSON for this package
    error_json_fragment=$(jq -n --arg pkg_name_arg "$package_to_process" '{($pkg_name_arg): {"error": "PKGBUILD directory not uniquely found"}}')
    combined_pkg_details_json=$(jq -n --argjson current "$combined_pkg_details_json" --argjson fragment "$error_json_fragment" '$current + $fragment')
    all_pkgbuild_details_extracted_successfully=false
    _end_group
    continue
  fi

  package_dir_abs="${pkg_build_dirs_found[0]}"
  # Ensure PKGBUILD file itself exists
  if [ ! -f "${package_dir_abs}/PKGBUILD" ]; then
    _log_warning "JSON_GEN_PKG_NO_PKGBUILD" "PKGBUILD file missing in found directory ${package_dir_abs} for '${package_to_process}'."
    error_json_fragment=$(jq -n --arg pkg_name_arg "$package_to_process" '{($pkg_name_arg): {"error": "PKGBUILD file not found in package directory"}}')
    combined_pkg_details_json=$(jq -n --argjson current "$combined_pkg_details_json" --argjson fragment "$error_json_fragment" '$current + $fragment')
    all_pkgbuild_details_extracted_successfully=false
    _end_group
    continue
  fi

  package_dir_rel_to_workspace="${package_dir_abs#${GITHUB_WORKSPACE}/}"
  single_package_json_fragment=""
  process_details_succeeded_for_pkg=true
  if ! single_package_json_fragment=$(process_single_package_details "$package_to_process" "$package_dir_rel_to_workspace"); then
    _log_warning "JSON_GEN_PKG_PROCESS_FAIL" "process_single_package_details had issues for ${package_to_process}."
    # process_single_package_details already outputs an error JSON fragment on failure.
    process_details_succeeded_for_pkg=false
    all_pkgbuild_details_extracted_successfully=false # Mark overall failure
  fi
  _log_debug "JSON fragment for ${package_to_process}: ${single_package_json_fragment}"
  temp_merged_json=""
  if temp_merged_json=$(jq -n --argjson current "$combined_pkg_details_json" --argjson fragment "$single_package_json_fragment" '$current + $fragment'); then
    combined_pkg_details_json="$temp_merged_json"
    if ! $process_details_succeeded_for_pkg; then _log_warning "JSON_GEN_PKG_MERGED_ERR" "Merged error JSON for ${package_to_process}."; fi
  else
    _log_error "JSON_GEN_PKG_MERGE_FAIL" "jq merge FAILED for ${package_to_process}. Fragment was: ${single_package_json_fragment}"
    all_pkgbuild_details_extracted_successfully=false
  fi
  _end_group
done
_log_notice "JSON_GEN_LOOP_END" "Finished loop for PKGBUILD details."
# Write the JSON to file, ensuring it's owned by builder (as it's in NVCHECKER_RUN_DIR)
echo "${combined_pkg_details_json}" | jq . >temp_package_details.json # Temp file first
if ! sudo -u builder mv temp_package_details.json "${PACKAGE_DETAILS_JSON_PATH}"; then
  _log_error "JSON_GEN_MV_FAIL" "Failed to move temp_package_details.json to ${PACKAGE_DETAILS_JSON_PATH} as builder."
  # No chown needed if mv as builder worked. If not, try chown if file was created by root.
  if [ -f temp_package_details.json ] && [ ! -f "${PACKAGE_DETAILS_JSON_PATH}" ]; then # if move failed, but temp exists
    _log_warning "JSON_GEN_MV_RETRY" "Attempting to copy and chown as fallback."
    cp temp_package_details.json "${PACKAGE_DETAILS_JSON_PATH}"
    if ! sudo -u builder chown builder:builder "${PACKAGE_DETAILS_JSON_PATH}"; then _log_warning "JSON_GEN_CHOWN_FAIL" "Failed to chown ${PACKAGE_DETAILS_JSON_PATH}."; fi
  fi
fi
if [ -f temp_package_details.json ]; then rm temp_package_details.json; fi

if ! $all_pkgbuild_details_extracted_successfully; then
  _log_error "JSON_GEN_FAIL_OVERALL" "One or more packages failed PKGBUILD detail extraction. Check logs. Contents of ${PACKAGE_DETAILS_JSON_PATH}:"
  cat "${PACKAGE_DETAILS_JSON_PATH}" | while IFS= read -r line; do _log_debug "  $line"; done
  # Decide if this is a fatal error for the whole process. For now, continue to attempt builds with available data.
  # exit 1 # Optionally exit if any PKGBUILD detail extraction fails.
fi

_log_notice "JSON_GEN_SUMMARY" "${PACKAGE_DETAILS_JSON_PATH} (Size: $(wc -c <"${PACKAGE_DETAILS_JSON_PATH}" 2>/dev/null || echo 0) bytes) created/updated."
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

  # Check if this package had an error during PKGBUILD detail extraction
  # The jq command checks if the package name exists as a key and if that key has an "error" sub-key.
  if jq -e --arg pkg_name "$package_to_build" '.[$pkg_name] | has("error")' "${PACKAGE_DETAILS_JSON_PATH}" >/dev/null; then
    _log_error "BUILD_LOOP_SKIP_ERR_PKG" "Skipping build for '${package_to_build}' due to errors in PKGBUILD detail extraction (see above)."
    package_error_msg=$(jq -r --arg pkg_name "$package_to_build" '.[$pkg_name].error // "Unknown error during detail extraction."' "${PACKAGE_DETAILS_JSON_PATH}")
    echo "| **${package_to_build}** | N/A | ❌ Error: PKGBUILD details error: ${package_error_msg} | - | - | N/A |" >>"$GITHUB_STEP_SUMMARY"
    overall_build_success=false
    continue
  fi

  pkg_build_dirs_found_for_build=()
  mapfile -t pkg_build_dirs_found_for_build < <(find "${GITHUB_WORKSPACE}" -path "*/maintain/build/${package_to_build}" -type d -print -quit)
  if [ ${#pkg_build_dirs_found_for_build[@]} -ne 1 ]; then
    _log_error "BUILD_LOOP_FIND_ERR" "PKGBUILD dir error for build: '${package_to_build}' (Found: ${#pkg_build_dirs_found_for_build[@]}). Should have been caught earlier."
    echo "| **${package_to_build}** | N/A | ❌ Error: PKGBUILD dir not found for build | - | - | N/A |" >>"$GITHUB_STEP_SUMMARY"
    overall_build_success=false
    continue
  fi
  package_dir_abs_for_build="${pkg_build_dirs_found_for_build[0]}"
  if [ ! -f "${package_dir_abs_for_build}/PKGBUILD" ]; then # Double check PKGBUILD file exists
    _log_error "BUILD_LOOP_NO_PKGBUILD" "PKGBUILD file missing in dir ${package_dir_abs_for_build} for build of '${package_to_build}'."
    echo "| **${package_to_build}** | N/A | ❌ Error: PKGBUILD file not found for build | - | - | N/A |" >>"$GITHUB_STEP_SUMMARY"
    overall_build_success=false
    continue
  fi
  package_dir_rel_to_workspace_for_build="${package_dir_abs_for_build#${GITHUB_WORKSPACE}/}"

  path_components_for_build=()
  path_components_for_build=($(extract_path_components "${package_dir_rel_to_workspace_for_build}"))
  build_mode_from_dir_for_build="nobuild" # Default build mode
  # Example path: category/build_type/package_name -> components_array[0]=category, [1]=build_type, [2]=package_name
  # Path relative to GITHUB_WORKSPACE: maintain/build/package_name -> components_array[0]=maintain, [1]=build, [2]=package_name
  # Path relative to GITHUB_WORKSPACE: maintain/test/package_name -> components_array[0]=maintain, [1]=test, [2]=package_name
  # We need to check the component that defines the build type.
  # If pkgbuild_path is like "maintain/build/apache-spark", components are (maintain build apache-spark)
  # We expect the build type to be the directory *containing* the package name directory, under "maintain".
  # So if pkgbuild_path_rel_arg is "maintain/build/pkgname", its parent dir name is "build"
  # Let's refine path component logic for build mode.
  # Assuming structure GITHUB_WORKSPACE/some_root_dir/actual_build_mode/package_name/PKGBUILD
  # And pkgbuild_path_rel_arg points to GITHUB_WORKSPACE/some_root_dir/actual_build_mode/package_name

  # Simplification: Use directory name containing PKGBUILD, if it's 'build' or 'test'
  # This needs pkgbuild_path_rel_arg to be like "category/build_mode_dir/pkg_name_dir"
  # Or, if pkgbuild_path_rel_arg is "maintain/build/pkgname", then extract "build"
  # Let's use the folder name one level above the package_name folder.
  # Example: "maintain/build/apache-spark" -> parent of "apache-spark" is "build".
  #          "experimental/test/foobar" -> parent of "foobar" is "test".

  # Get parent directory of pkgbuild_path_rel_arg
  parent_dir_of_pkg_path=$(dirname "${package_dir_rel_to_workspace_for_build}")
  # Get basename of that parent directory
  build_mode_candidate=$(basename "${parent_dir_of_pkg_path}")

  if [[ "${build_mode_candidate}" == "build" || "${build_mode_candidate}" == "test" ]]; then
    build_mode_from_dir_for_build="${build_mode_candidate}"
  else
    _log_warning "BUILD_LOOP_MODE_WARN" "Cannot determine build_mode for ${package_to_build} from path ${package_dir_rel_to_workspace_for_build} (parent dir basename: ${build_mode_candidate}). Defaulting to 'nobuild'."
  fi
  _log_debug "Package: ${package_to_build}, Determined build_mode: ${build_mode_from_dir_for_build}"

  if ! execute_build_script_py "$package_to_build" "$build_mode_from_dir_for_build" "$package_dir_rel_to_workspace_for_build"; then
    # Error message is already logged by execute_build_script_py and added to summary
    _log_error "BUILD_LOOP_SCRIPT_FAIL_MAIN" "Build script execution FAILED for ${package_to_build} (reported by execute_build_script_py)."
    overall_build_success=false
    # Summary line already added by execute_build_script_py
  fi
done
_log_notice "BUILD_LOOP" "Finished build loop."
_end_group

if ! $overall_build_success; then
  _log_error "MAIN_EXEC_BUILD_FAIL" "One or more packages failed to build or had errors. See summary and individual build logs."
  exit 1
fi
_log_notice "MAIN_EXEC" "All tasks completed successfully."
