#!/usr/bin/env python3

import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional

# --- Constants and Configuration ---
BUILDER_USER = "builder" # User for build operations
BUILDER_HOME = Path(os.getenv("BUILDER_HOME", f"/home/{BUILDER_USER}"))
NVCHECKER_RUN_DIR = Path(os.getenv("NVCHECKER_RUN_DIR", str(BUILDER_HOME / "nvchecker-run")))
PACKAGE_BUILD_BASE_DIR = Path(os.getenv("PACKAGE_BUILD_BASE_DIR", str(BUILDER_HOME / "pkg_builds")))

GITHUB_WORKSPACE = Path(os.getenv("GITHUB_WORKSPACE", ""))
PKGBUILD_ROOT = os.getenv("PKGBUILD_ROOT", "")
PKGBUILD_ROOT_PATH_STR = os.path.normpath(str(GITHUB_WORKSPACE / PKGBUILD_ROOT.lstrip("/")))

ARTIFACTS_DIR = Path(os.getenv("ARTIFACTS_DIR", str(GITHUB_WORKSPACE / "artifacts")))

# This is the output from aur_package_updater_cli.py
UPDATER_CLI_OUTPUT_JSON_PATH = NVCHECKER_RUN_DIR / "updater_cli_output.json"
# This is the JSON consumed by buildscript2.py
PACKAGE_DETAILS_JSON_PATH = NVCHECKER_RUN_DIR / "package_details.json"

KEYFILE_PATH = NVCHECKER_RUN_DIR / "keyfile.toml"

GIT_COMMIT_USER_NAME = os.getenv("GIT_USER_NAME", "GitHub Actions CI")
GIT_COMMIT_USER_EMAIL = os.getenv("GIT_USER_EMAIL", "actions@github.com")

# Secrets and contextual env vars
SECRET_GHUK_VALUE = os.getenv("SECRET_GHUK_VALUE")
AUR_MAINTAINER_NAME = os.getenv("AUR_MAINTAINER_NAME")
GH_TOKEN = os.getenv("GH_TOKEN")
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY")
GITHUB_RUN_ID = os.getenv("GITHUB_RUN_ID")
GITHUB_STEP_SUMMARY_FILE = os.getenv("GITHUB_STEP_SUMMARY")

# --- Logging Helpers (GitHub Actions format) ---
def _log_gha(level: str, title: str, message: str):
    # Replace problematic characters for GHA logging
    sanitized_message = message.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
    sanitized_title = title.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
    print(f"::{level} title={sanitized_title}::{sanitized_message}")

def log_notice(title: str, message: str):
    _log_gha("notice", title, message)

def log_error(title: str, message: str):
    _log_gha("error", title, message)

def log_warning(title: str, message: str):
    _log_gha("warning", title, message)

def log_debug(message: str):
    # GitHub debug logs don't have titles
    sanitized_message = message.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
    print(f"::debug::{sanitized_message}")

def start_group(title: str):
    print(f"::group::{title}")

def end_group():
    print(f"::endgroup::")

# --- Subprocess Helper ---
def run_command(
    cmd: List[str],
    check: bool = True,
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
    capture_output: bool = True,
    print_command: bool = True,
    input_data: Optional[str] = None
) -> subprocess.CompletedProcess:
    if print_command:
        # For sensitive commands, caller can set print_command=False
        log_debug(f"Running command: {shlex.join(cmd)}")
    
    process_env = os.environ.copy()
    if env:
        process_env.update(env)

    try:
        result = subprocess.run(
            cmd,
            check=check,
            text=True,
            capture_output=capture_output,
            cwd=cwd,
            env=process_env,
            input=input_data
        )
        return result
    except subprocess.CalledProcessError as e:
        log_error("CMD_FAIL", f"Command '{shlex.join(cmd)}' failed with {e.returncode}")
        if e.stdout: # log STDOUT and STDERR from failed command
            log_debug(f"CMD_FAIL_STDOUT:\n{e.stdout.strip()}")
        if e.stderr:
            log_debug(f"CMD_FAIL_STDERR:\n{e.stderr.strip()}")
        if check:
            raise
        return e 
    except FileNotFoundError as e:
        log_error("CMD_NOT_FOUND", f"Command not found: {cmd[0]}. Ensure it's in PATH or installed.")
        if check:
            raise
        return subprocess.CompletedProcess(cmd, returncode=127, stdout="", stderr=str(e))


# --- Core Functions ---

def setup_environment() -> bool:
    start_group("Setup Environment")
    log_notice("SETUP_ENV", f"Configuring environment in {NVCHECKER_RUN_DIR}, {PACKAGE_BUILD_BASE_DIR}, {ARTIFACTS_DIR}...")

    dirs_to_create_as_builder = [NVCHECKER_RUN_DIR, PACKAGE_BUILD_BASE_DIR]
    for d_path in dirs_to_create_as_builder:
        try:
            run_command(["sudo", "-u", BUILDER_USER, "mkdir", "-p", str(d_path)])
        except subprocess.CalledProcessError:
            log_error("SETUP_FAIL", f"mkdir for {d_path} as {BUILDER_USER} failed.")
            end_group()
            return False
    
    try:
        ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        log_error("SETUP_FAIL", f"mkdir ARTIFACTS_DIR ({ARTIFACTS_DIR}) failed: {e}")
        end_group()
        return False
    
    try: # Attempt to chown artifacts dir to builder so it can write directly if needed
        run_command(["sudo", "chown", f"{BUILDER_USER}:{BUILDER_USER}", str(ARTIFACTS_DIR)], check=False)
    except subprocess.CalledProcessError:
         log_warning("SETUP_CHOWN_WARN", f"sudo chown {ARTIFACTS_DIR} to {BUILDER_USER} failed. This might be ok.")

    scripts_to_copy = ["buildscript2.py", "aur_package_updater_cli.py"]
    # Assuming scripts are in GITHUB_WORKSPACE/scripts/
    # If your scripts are in the root of GITHUB_WORKSPACE, adjust scripts_source_base
    scripts_source_base = GITHUB_WORKSPACE / "scripts" 

    for script_name in scripts_to_copy:
        source_path = scripts_source_base / script_name
        if not source_path.is_file(): # If not in /scripts, check GITHUB_WORKSPACE root
            source_path = GITHUB_WORKSPACE / script_name 
        
        dest_path = NVCHECKER_RUN_DIR / script_name
        if not source_path.is_file():
            log_error("SETUP_FAIL", f"{script_name} not found at {scripts_source_base / script_name} or {GITHUB_WORKSPACE / script_name}.")
            end_group()
            return False
        try:
            shutil.copy(source_path, dest_path)
            run_command(["sudo", "chown", f"{BUILDER_USER}:{BUILDER_USER}", str(dest_path)])
            run_command(["sudo", "-u", BUILDER_USER, "chmod", "+x", str(dest_path)])
            log_debug(f"Copied, chowned, and chmodded {script_name} to {dest_path}")
        except Exception as e:
            log_error("SETUP_FAIL", f"Failed to copy/chown/chmod {script_name}: {e}")
            end_group()
            return False

    log_notice("SETUP_ENV", "Environment setup SUCCEEDED.")
    end_group()
    return True

def create_nvchecker_keyfile() -> bool:
    start_group("Create NVChecker Keyfile for Updater CLI")
    if not SECRET_GHUK_VALUE:
        log_warning("KEYFILE_SKIP", "SECRET_GHUK_VALUE not set. Skipping keyfile.toml creation. Updater CLI might fail for GitHub sources.")
        end_group()
        return True 

    keyfile_content = f"[keys]\ngithub = '{SECRET_GHUK_VALUE}'\n"
    temp_keyfile_path = NVCHECKER_RUN_DIR / "temp_keyfile.toml" # Create as runner first

    try:
        with open(temp_keyfile_path, "w") as f:
            f.write(keyfile_content)
        
        run_command(["sudo", "-u", BUILDER_USER, "mv", str(temp_keyfile_path), str(KEYFILE_PATH)])
        log_notice("KEYFILE_OK", f"keyfile.toml created at {KEYFILE_PATH} and owned by {BUILDER_USER}.")
    except Exception as e:
        log_error("KEYFILE_FAIL", f"Failed to create or move keyfile.toml: {e}")
        if temp_keyfile_path.exists():
            temp_keyfile_path.unlink(missing_ok=True)
        end_group()
        return False
    
    end_group()
    return True
def run_aur_updater_cli() -> Optional[List[Dict[str, Any]]]:
    start_group("Run AUR Package Updater CLI")
    script_path = NVCHECKER_RUN_DIR / "aur_package_updater_cli.py"
    if not AUR_MAINTAINER_NAME:
        log_error("AUR_UPDATER_FAIL", "AUR_MAINTAINER_NAME environment variable is not set.")
        end_group()
        return None

    # Determine the path-root for the updater CLI
    path_root_for_cli = ""
    if PKGBUILD_ROOT_PATH_STR:
        path_root_for_cli = PKGBUILD_ROOT_PATH_STR
        log_notice("PATH_ROOT_INFO", f"Using PKGBUILD_ROOT='{path_root_for_cli}' for --path-root.")
    else:
        path_root_for_cli = str(GITHUB_WORKSPACE)
        log_notice("PATH_ROOT_INFO", f"PKGBUILD_ROOT env var not set. Defaulting --path-root to GITHUB_WORKSPACE='{path_root_for_cli}'.")


    cmd = [
        "sudo", "-E", "-u", BUILDER_USER, f"HOME={BUILDER_HOME}",
        "python3", str(script_path),
        "--maintainer", AUR_MAINTAINER_NAME,
        "--path-root", path_root_for_cli, # USE THE DETERMINED PATH HERE
        "--output-file", str(UPDATER_CLI_OUTPUT_JSON_PATH), 
    ]
    if KEYFILE_PATH.exists():
         cmd.extend(["--key-toml", str(KEYFILE_PATH)])
    if os.getenv("RUNNER_DEBUG") == "1" or os.getenv("ACTIONS_STEP_DEBUG") == "true":
        cmd.append("--debug")

    try:
        # Run the command, don't check=True immediately, check its output first
        proc_result = run_command(cmd, cwd=NVCHECKER_RUN_DIR, check=False) 
        
        # Log stdout/stderr regardless of exit code for better debugging
        if proc_result.stdout:
            log_debug(f"AUR Updater CLI STDOUT:\n{proc_result.stdout.strip()}")
        if proc_result.stderr:
            log_debug(f"AUR Updater CLI STDERR:\n{proc_result.stderr.strip()}")

        if proc_result.returncode != 0:
            log_error("AUR_UPDATER_NON_ZERO", f"aur_package_updater_cli.py exited with code {proc_result.returncode}.")
            # Even if it exited non-zero, it might have written a partial or error JSON
            # We'll proceed to check the file, but this is a strong indicator of a problem.

        # Check file existence and size rigorously
        if not UPDATER_CLI_OUTPUT_JSON_PATH.is_file():
            log_error("AUR_UPDATER_NO_FILE", f"Output file {UPDATER_CLI_OUTPUT_JSON_PATH} was NOT created.")
            end_group()
            return None
        
        file_size = UPDATER_CLI_OUTPUT_JSON_PATH.stat().st_size
        if file_size < 5: # Arbitrary small number, e.g., for "{}" or "[]"
            log_error("AUR_UPDATER_EMPTY_OUTPUT", f"Output file {UPDATER_CLI_OUTPUT_JSON_PATH} is too small (size: {file_size} bytes). Content might be invalid or empty.")
            # Try to read and log content if small, it might be an error message
            try:
                with open(UPDATER_CLI_OUTPUT_JSON_PATH, "r") as f_small:
                    small_content = f_small.read()
                log_debug(f"Small file content of {UPDATER_CLI_OUTPUT_JSON_PATH}: {small_content}")
            except Exception as read_err:
                log_debug(f"Could not read small file content: {read_err}")
            
            # If the script also had a non-zero exit, it's definitely a failure.
            if proc_result.returncode != 0:
                end_group()
                return None
            # If exit code was 0 but file is too small, it's still problematic.
            # Depending on aur_package_updater_cli.py behavior, an empty JSON list "[]" might be valid if no packages found.
            # However, if we expect packages, this is an error.

        # Attempt to load JSON
        with open(UPDATER_CLI_OUTPUT_JSON_PATH, "r") as f:
            update_data = json.load(f)
        
        # If the script had a non-zero exit code but we managed to parse some JSON,
        # it's up to you if this data is trustworthy. For now, let's log and continue if JSON is valid.
        if proc_result.returncode != 0:
            log_warning("AUR_UPDATER_NON_ZERO_WITH_JSON", f"aur_package_updater_cli.py exited {proc_result.returncode} but valid JSON was parsed from output file.")

        log_notice("AUR_UPDATER_OK", f"aur_package_updater_cli.py ran. Output at {UPDATER_CLI_OUTPUT_JSON_PATH} (Size: {file_size} bytes).")
        
        # Artifact the updater CLI output
        artifact_updater_output_path = ARTIFACTS_DIR / "updater_cli_output.json"
        try:
            run_command(["sudo", "-u", BUILDER_USER, "cp", str(UPDATER_CLI_OUTPUT_JSON_PATH), str(artifact_updater_output_path)])
            log_notice("ARTIFACT_OK", f"Copied updater CLI output to artifacts: {artifact_updater_output_path}")
        except Exception as e:
            log_warning("ARTIFACT_FAIL", f"Failed to copy {UPDATER_CLI_OUTPUT_JSON_PATH} to artifacts: {e}")

        # log_debug(f"Updater CLI output head: {str(update_data)[:500]}") # This can be very verbose
        if isinstance(update_data, list) and update_data:
            log_debug(f"First element of Updater CLI output: {json.dumps(update_data[0], indent=2)}")
        elif isinstance(update_data, dict) and update_data:
             log_debug(f"Updater CLI output (dict): {json.dumps(update_data, indent=2)}")
        else:
            log_debug(f"Updater CLI output appears empty or not a list/dict: {update_data}")


        end_group()
        return update_data
        
    # except subprocess.CalledProcessError: # This path is less likely now with check=False
    #     log_error("AUR_UPDATER_FAIL_EXEC", "aur_package_updater_cli.py execution failed (Caught CalledProcessError).")
    #     end_group() # Should have been handled by run_command itself
    #     return None
    except json.JSONDecodeError as e:
        log_error("AUR_UPDATER_JSON_DECODE_FAIL", f"Failed to parse JSON from {UPDATER_CLI_OUTPUT_JSON_PATH}. Error: {e}. File size: {UPDATER_CLI_OUTPUT_JSON_PATH.stat().st_size if UPDATER_CLI_OUTPUT_JSON_PATH.exists() else 'N/A'}")
        # Attempt to log the problematic content
        if UPDATER_CLI_OUTPUT_JSON_PATH.exists():
            try:
                with open(UPDATER_CLI_OUTPUT_JSON_PATH, "r") as f_err:
                    err_content = f_err.read()
                log_debug(f"Content of {UPDATER_CLI_OUTPUT_JSON_PATH} that failed to parse:\n{err_content[:1000]}") # Log first 1KB
            except Exception as read_err:
                log_debug(f"Could not read content of {UPDATER_CLI_OUTPUT_JSON_PATH} on JSON error: {read_err}")
        end_group()
        return None
    except Exception as e: # Catch-all for other unexpected issues
        log_error("AUR_UPDATER_UNEXPECTED_ERROR", f"An unexpected error occurred during aur_package_updater_cli.py processing: {type(e).__name__} - {e}")
        end_group()
        return None

def get_packages_to_update(update_data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    start_group("Identify Packages for Update from Updater CLI Output")
    packages_needing_update = []
    for pkg_info in update_data:
        pkgbase = pkg_info.get("pkgbase")
        is_update = pkg_info.get("is_update", False)
        errors = pkg_info.get("errors", [])
        
        if not pkgbase: # Should be guaranteed by aur_package_updater_cli.py if entry exists
            log_warning("PKG_SKIP_NO_PKGBASE", f"Skipping entry due to missing pkgbase: {str(pkg_info)[:100]}")
            continue
        if not pkg_info.get("pkgfile"): # Ensure local PKGBUILD was processed
            log_warning("PKG_SKIP_NO_PKGBUILD_INFO", f"Package {pkgbase} has no 'pkgfile' info from updater. Skipping.")
            continue

        if errors:
            log_warning("PKG_HAS_ERRORS", f"Package {pkgbase} has errors from updater: {errors}. Not considering for auto-update.")
            continue
        
        if is_update:
            log_notice("UPDATE_CANDIDATE", f"Package {pkgbase} needs update. New version: {pkg_info.get('new_version_for_update')} from {pkg_info.get('update_source')}")
            packages_needing_update.append(pkg_info)
        else:
            log_debug(f"Package {pkgbase} does not require an update based on 'is_update' flag from updater CLI.")

    if not packages_needing_update:
        log_notice("NO_UPDATES", "No packages require updates based on updater CLI.")
    else:
        log_notice("UPDATES_FOUND", f"Found {len(packages_needing_update)} package(s) to update.")
    
    end_group()
    return packages_needing_update

def generate_package_details_for_buildscript(packages_info: List[Dict[str, Any]]) -> bool:
    start_group("Generate package_details.json for Build Script")
    all_pkg_details_for_buildscript: Dict[str, Dict[str, List[str]]] = {}
    
    if not packages_info:
        log_notice("JSON_GEN_SKIP", "No packages to process for buildscript's JSON input.")
        # Write an empty JSON object if no packages, so buildscript2.py doesn't fail on missing file
        try:
            with open(PACKAGE_DETAILS_JSON_PATH, "w") as f: # Write as runner first
                json.dump({}, f)
            run_command(["sudo", "-u", BUILDER_USER, "mv", str(PACKAGE_DETAILS_JSON_PATH), str(PACKAGE_DETAILS_JSON_PATH)]) # "mv to self" to chown
        except Exception as e:
            log_error("JSON_GEN_EMPTY_FAIL", f"Failed to write empty {PACKAGE_DETAILS_JSON_PATH}: {e}")
            end_group()
            return False
        end_group()
        return True


    for pkg_info_entry in packages_info:
        pkgbase = pkg_info_entry["pkgbase"] # Must exist
        
        # Extract dependencies directly from aur_package_updater_cli.py's output
        depends = pkg_info_entry.get("depends", [])
        makedepends = pkg_info_entry.get("makedepends", [])
        checkdepends = pkg_info_entry.get("checkdepends", [])
        
        # The `aur_package_updater_cli.py` schema provided does not include "sources".
        # `buildscript2.py` is designed to handle this: if "sources" is missing or empty
        # in `package_details.json`, it will only track default files (PKGBUILD, .SRCINFO, etc.).
        # If `aur_package_updater_cli.py` *were* to provide sources, they would be used here.
        sources_from_updater = pkg_info_entry.get("sources", []) # Add this if updater provides it

        all_pkg_details_for_buildscript[pkgbase] = {
            "depends": depends,
            "makedepends": makedepends,
            "checkdepends": checkdepends,
            "sources": sources_from_updater, 
        }
        log_debug(f"Details for {pkgbase} for buildscript: depends={len(depends)}, makedepends={len(makedepends)}, checkdepends={len(checkdepends)}, sources={len(sources_from_updater)}")

    temp_json_path = NVCHECKER_RUN_DIR / "temp_pkg_details_for_bs.json" # Write as runner
    try:
        with open(temp_json_path, "w") as f:
            json.dump(all_pkg_details_for_buildscript, f, indent=2)
        
        run_command(["sudo", "-u", BUILDER_USER, "mv", str(temp_json_path), str(PACKAGE_DETAILS_JSON_PATH)])
        log_notice("JSON_GEN_OK", f"{PACKAGE_DETAILS_JSON_PATH} (for buildscript) created and owned by {BUILDER_USER}.")
        log_debug(f"Full content of {PACKAGE_DETAILS_JSON_PATH}:\n{json.dumps(all_pkg_details_for_buildscript, indent=2)}")
    except Exception as e:
        log_error("JSON_GEN_WRITE_FAIL", f"Failed to write or move {PACKAGE_DETAILS_JSON_PATH}: {e}")
        if temp_json_path.exists():
            temp_json_path.unlink(missing_ok=True)
        end_group()
        return False
    
    end_group()
    return True

def determine_build_mode(pkgbuild_dir_rel_to_workspace: Path) -> str:
    parent_dir_name = pkgbuild_dir_rel_to_workspace.parent.name
    if parent_dir_name in ["build", "test"]:
        return parent_dir_name
    else:
        log_warning("BUILD_MODE_UNKNOWN", f"Cannot determine build_mode from path {pkgbuild_dir_rel_to_workspace} (parent dir: {parent_dir_name}). Defaulting to 'nobuild'.")
        return "nobuild"

def execute_build_script_py(
    package_name: str, # This is pkgbase
    build_type: str,
    pkgbuild_path_rel: str # Relative to GITHUB_WORKSPACE, points to the PKGBUILD *directory*
) -> bool:
    start_group(f"Build Script Execution: {package_name}")
    
    package_specific_artifact_dir = ARTIFACTS_DIR / package_name
    try:
        run_command(["sudo", "-u", BUILDER_USER, f"HOME={BUILDER_HOME}", "mkdir", "-p", str(package_specific_artifact_dir)])
    except subprocess.CalledProcessError:
        log_error("BUILD_SCRIPT_MKDIR_FAIL", f"Failed to create artifact subdir {package_specific_artifact_dir} for {package_name}.")
        if GITHUB_STEP_SUMMARY_FILE:
            with open(GITHUB_STEP_SUMMARY_FILE, "a", encoding="utf-8") as f:
                f.write(f"| **{package_name}** | N/A | ❌ Error: Artifact dir creation | - | - | N/A |\n")
        end_group()
        return False

    log_notice("BUILD_SCRIPT_PY_EXEC", f"Starting buildscript2.py for package: {package_name} (Type: {build_type}, PKGBUILD Path: {pkgbuild_path_rel})")

    git_config_cmds = [
        ["sudo", "-E", "-u", BUILDER_USER, f"HOME={BUILDER_HOME}", "git", "config", "--global", "user.name", GIT_COMMIT_USER_NAME],
        ["sudo", "-E", "-u", BUILDER_USER, f"HOME={BUILDER_HOME}", "git", "config", "--global", "user.email", GIT_COMMIT_USER_EMAIL],
    ]
    for git_cmd in git_config_cmds: # Corrected loop variable name
        try:
            run_command(git_cmd, print_command=False) # Don't log git config commands with user details
        except subprocess.CalledProcessError:
            log_warning("GIT_CONFIG_FAIL", f"Failed to set {git_cmd[-2]} for builder. Build script might fail.")

    build_script_executable = NVCHECKER_RUN_DIR / "buildscript2.py"
    cmd_build = [
        "sudo", "-E", "-u", BUILDER_USER, f"HOME={BUILDER_HOME}",
        "python3", str(build_script_executable),
        "--github-repo", GITHUB_REPOSITORY,
        "--github-token", GH_TOKEN, # Passed as env var for sudo -E, but also explicitly to script
        "--github-workspace", str(GITHUB_WORKSPACE),
        "--package-name", package_name,
        "--depends-json", str(PACKAGE_DETAILS_JSON_PATH),
        "--pkgbuild-path", pkgbuild_path_rel,
        "--commit-message", f"CI: Auto update {package_name}",
        "--build-mode", build_type,
        "--artifacts-dir", str(package_specific_artifact_dir),
        "--base-build-dir", str(PACKAGE_BUILD_BASE_DIR),
    ]
    if os.getenv("RUNNER_DEBUG") == "1" or os.getenv("ACTIONS_STEP_DEBUG") == "true":
        cmd_build.append("--debug")

    bs_result_json_str = ""
    bs_success = False
    bs_version = "N/A"
    bs_changes_detected_str = "➖ No" 
    bs_error_msg = ""

    try:
        build_proc_result = run_command(cmd_build, cwd=NVCHECKER_RUN_DIR)
        bs_result_json_str = build_proc_result.stdout.strip()
        
        if not bs_result_json_str:
            # If stdout is empty but exit code was 0, it might be an issue in buildscript2.py
            # or an unexpected success path that produces no JSON.
            if build_proc_result.returncode == 0:
                 raise ValueError("buildscript2.py exited 0 but produced no JSON output. This is unexpected.")
            else: # Non-zero exit, empty stdout
                 raise ValueError(f"buildscript2.py exited {build_proc_result.returncode} and produced no JSON output.")


        bs_data = json.loads(bs_result_json_str)
        bs_success = bs_data.get("success", False)
        bs_version = bs_data.get("version", "N/A")
        if bs_data.get("changes_detected", False):
            bs_changes_detected_str = "✔️ Yes"
        bs_error_msg = bs_data.get("error_message", "")

        if build_proc_result.returncode != 0 and bs_success:
            log_warning("BUILD_SCRIPT_EXIT_MISMATCH", f"buildscript2.py exited {build_proc_result.returncode} but JSON reported success=true.")
            # Potentially override bs_success = False here if strict adherence to exit code is required
        elif build_proc_result.returncode == 0 and not bs_success and not bs_error_msg:
            bs_error_msg = "buildscript2.py exited 0 but JSON reported success=false without explicit error_message."
            log_warning("BUILD_SCRIPT_SUCCESS_MISMATCH", bs_error_msg)
        
        # If the script failed (non-zero exit) and JSON parsing also failed or reported no error,
        # ensure a generic error based on exit code is present.
        if build_proc_result.returncode != 0 and not bs_error_msg:
            bs_error_msg = f"buildscript2.py command failed (exit {build_proc_result.returncode}) with no specific error in JSON."
            bs_success = False # Ensure success is false if exit code is non-zero

    except subprocess.CalledProcessError as e: # Should be caught by run_command's check=False, but as fallback
        bs_error_msg = f"buildscript2.py command execution failed (exit {e.returncode}). Stderr: {e.stderr[:200]}"
        log_error("BUILD_SCRIPT_PY_FAIL_CMD", bs_error_msg)
        bs_success = False
    except (json.JSONDecodeError, ValueError) as e: # ValueError for empty JSON or other issues
        bs_error_msg = f"Failed to parse/validate JSON from buildscript2.py: {e}. Raw output: '{bs_result_json_str[:200]}...'"
        log_error("BUILD_SCRIPT_PY_FAIL_JSON", bs_error_msg)
        bs_success = False
    except Exception as e:
        bs_error_msg = f"Unexpected error running/parsing buildscript2.py: {e}"
        log_error("BUILD_SCRIPT_PY_FAIL_UNEX", bs_error_msg)
        bs_success = False

    status_md = "✅ Success" if bs_success else "❌ Failure"
    if not bs_success and bs_error_msg:
        sanitized_error_msg = bs_error_msg.replace("|", "\\|").replace("\n", "<br>")
        status_md += f": <small>{sanitized_error_msg}</small>"
    
    aur_link_md = f"[AUR](https://aur.archlinux.org/packages/{package_name})"
    log_link_md = "N/A"
    if package_specific_artifact_dir.exists():
        found_logs = list(package_specific_artifact_dir.glob(f"{package_name}*.log")) + \
                     list(package_specific_artifact_dir.glob("*.log"))
        if found_logs:
             log_link_md = f"See 'build-artifacts-{GITHUB_RUN_ID}' (<tt>{package_name}/</tt> subdir)"

    if GITHUB_STEP_SUMMARY_FILE:
        with open(GITHUB_STEP_SUMMARY_FILE, "a", encoding="utf-8") as f:
            f.write(f"| **{package_name}** | {bs_version} | {status_md} | {bs_changes_detected_str} | {aur_link_md} | {log_link_md} |\n")
    
    end_group()
    return bs_success


def main():
    log_notice("SCRIPT_START", "Arch Package Update Task started (Python v2).")

    if GITHUB_STEP_SUMMARY_FILE:
        with open(GITHUB_STEP_SUMMARY_FILE, "w", encoding="utf-8") as f:
            f.write("## Arch Package Build Summary\n")
            f.write("| Package | Version | Status | Changes | AUR Link | Build Logs |\n")
            f.write("|---|---|---|---|---|---|\n")
    else:
        log_warning("NO_SUMMARY_FILE", "GITHUB_STEP_SUMMARY env var not set. Summary will not be written.")

    critical_env_vars = {
        "AUR_MAINTAINER_NAME": AUR_MAINTAINER_NAME, "GH_TOKEN": GH_TOKEN,
        "GITHUB_REPOSITORY": GITHUB_REPOSITORY, "SECRET_GHUK_VALUE": SECRET_GHUK_VALUE
    }
    missing_vars = [k for k, v in critical_env_vars.items() if not v]
    if missing_vars:
        msg = f"Missing critical environment variables: {', '.join(missing_vars)}. Exiting."
        log_error("CRITICAL_ENV_MISSING", msg)
        if GITHUB_STEP_SUMMARY_FILE:
             with open(GITHUB_STEP_SUMMARY_FILE, "a", encoding="utf-8") as f:
                f.write(f"| **SETUP** | N/A | ❌ Failure: {msg} | - | - | Check Action Logs |\n")
        sys.exit(1)

    if not setup_environment():
        log_error("MAIN_FAIL", "setup_environment FAILED.")
        if GITHUB_STEP_SUMMARY_FILE:
             with open(GITHUB_STEP_SUMMARY_FILE, "a", encoding="utf-8") as f:
                f.write("| **SETUP** | N/A | ❌ Failure: Environment setup | - | - | Check Action Logs |\n")
        sys.exit(1)

    if not create_nvchecker_keyfile():
        log_warning("MAIN_WARN", "create_nvchecker_keyfile had issues. Updater CLI might have limited functionality.")

    updater_cli_data = run_aur_updater_cli()
    if updater_cli_data is None: 
        log_error("MAIN_FAIL", "run_aur_updater_cli FAILED to produce data.")
        if GITHUB_STEP_SUMMARY_FILE:
             with open(GITHUB_STEP_SUMMARY_FILE, "a", encoding="utf-8") as f:
                f.write("| **SETUP** | N/A | ❌ Failure: AUR Updater CLI | - | - | Check Action Logs |\n")
        sys.exit(1)

    packages_identified_for_build = get_packages_to_update(updater_cli_data)
    if not packages_identified_for_build:
        log_notice("MAIN_NO_UPDATES", "No packages to update. Exiting successfully.")
        if GITHUB_STEP_SUMMARY_FILE:
            with open(GITHUB_STEP_SUMMARY_FILE, "a", encoding="utf-8") as f:
                f.write("| *No updates found* | - | - | - | - | - |\n")
        sys.exit(0)

    if not generate_package_details_for_buildscript(packages_identified_for_build):
        log_error("MAIN_FAIL", "generate_package_details_for_buildscript FAILED.")
        if GITHUB_STEP_SUMMARY_FILE:
             with open(GITHUB_STEP_SUMMARY_FILE, "a", encoding="utf-8") as f:
                f.write("| **SETUP** | N/A | ❌ Failure: Generating input for buildscript2.py | - | - | Check Action Logs |\n")
        sys.exit(1)
    
    start_group("Build Packages Loop")
    overall_build_success = True
    for pkg_info_from_updater in packages_identified_for_build:
        pkgbase = pkg_info_from_updater["pkgbase"] # Must exist
        pkgbuild_file_abs_str = pkg_info_from_updater["pkgfile"] # Must exist and be a string path
        
        pkgbuild_dir_abs = Path(pkgbuild_file_abs_str).parent
        
        try:
            pkgbuild_dir_rel = pkgbuild_dir_abs.relative_to(GITHUB_WORKSPACE)
        except ValueError:
            err_msg = f"PKGBUILD path {pkgbuild_dir_abs} for {pkgbase} is not relative to GITHUB_WORKSPACE {GITHUB_WORKSPACE}. Skipping."
            log_error("BUILD_LOOP_PATH_ERR", err_msg)
            if GITHUB_STEP_SUMMARY_FILE:
                 with open(GITHUB_STEP_SUMMARY_FILE, "a", encoding="utf-8") as f:
                    f.write(f"| **{pkgbase}** | N/A | ❌ Error: Invalid PKGBUILD path | - | - | N/A |\n")
            overall_build_success = False
            continue

        build_mode_for_pkg = determine_build_mode(pkgbuild_dir_rel)
        
        if not execute_build_script_py(pkgbase, build_mode_for_pkg, str(pkgbuild_dir_rel)):
            log_error("BUILD_LOOP_PKG_FAIL", f"Build script FAILED for {pkgbase}.")
            overall_build_success = False
            # Summary line added by execute_build_script_py

    end_group() # Build Packages Loop

    if not overall_build_success:
        log_error("MAIN_EXEC_BUILD_FAIL", "One or more packages failed to build or had errors. See summary and logs.")
        sys.exit(1)

    log_notice("MAIN_EXEC_SUCCESS", "All tasks completed successfully.")
    sys.exit(0)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", stream=sys.stderr)
    try:
        main()
    except Exception as e:
        # Ensure error is logged in GHA format if possible
        error_title = "UNHANDLED_FATAL_EXCEPTION"
        error_message = f"A critical unhandled exception occurred in the main script: {type(e).__name__} - {e}"
        try: # Try GHA logging
            log_error(error_title, error_message)
        except: # Fallback to basic print
            print(f"::error title={error_title}::{error_message}", file=sys.stderr)
            
        if GITHUB_STEP_SUMMARY_FILE and Path(GITHUB_STEP_SUMMARY_FILE).exists():
             try:
                with open(GITHUB_STEP_SUMMARY_FILE, "a", encoding="utf-8") as f:
                    f.write(f"| **CRITICAL** | N/A | ❌ Failure: Unhandled script error: {e} | - | - | Check Action Logs |\n")
             except: pass # Best effort for summary
        sys.exit(2)
