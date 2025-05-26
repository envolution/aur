#!/usr/bin/env python3

import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import stat # For permission checking
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional

# --- Constants and Configuration ---
BUILDER_USER = "builder" 
BUILDER_HOME = Path(os.getenv("BUILDER_HOME", f"/home/{BUILDER_USER}"))
NVCHECKER_RUN_DIR = Path(os.getenv("NVCHECKER_RUN_DIR", str(BUILDER_HOME / "nvchecker-run")))
PACKAGE_BUILD_BASE_DIR = Path(os.getenv("PACKAGE_BUILD_BASE_DIR", str(BUILDER_HOME / "pkg_builds")))

GITHUB_WORKSPACE = Path(os.getenv("GITHUB_WORKSPACE", "/github/workspace"))
ARTIFACTS_DIR = Path(os.getenv("ARTIFACTS_DIR", str(GITHUB_WORKSPACE / "artifacts")))

UPDATER_CLI_OUTPUT_JSON_PATH = NVCHECKER_RUN_DIR / "updater_cli_output.json"
PACKAGE_DETAILS_JSON_PATH = NVCHECKER_RUN_DIR / "package_details.json"
KEYFILE_PATH = NVCHECKER_RUN_DIR / "keyfile.toml"

GIT_COMMIT_USER_NAME = os.getenv("GIT_USER_NAME", "GitHub Actions CI")
GIT_COMMIT_USER_EMAIL = os.getenv("GIT_USER_EMAIL", "actions@github.com")

SECRET_GHUK_VALUE = os.getenv("SECRET_GHUK_VALUE")
AUR_MAINTAINER_NAME = os.getenv("AUR_MAINTAINER_NAME")
GH_TOKEN = os.getenv("GH_TOKEN")
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY")
GITHUB_RUN_ID = os.getenv("GITHUB_RUN_ID") # Corrected from GITHUB_RUNID
GITHUB_STEP_SUMMARY_FILE = os.getenv("GITHUB_STEP_SUMMARY")
PKGBUILD_ROOT_PATH_STR = os.getenv("PKGBUILD_ROOT")

# --- Logging Helpers (GitHub Actions format) ---
def _log_gha(level: str, title: str, message: str):
    sanitized_message = str(message).replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
    sanitized_title = str(title).replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
    print(f"::{level} title={sanitized_title}::{sanitized_message}")

def log_notice(title: str, message: str): _log_gha("notice", title, message)
def log_error(title: str, message: str): _log_gha("error", title, message)
def log_warning(title: str, message: str): _log_gha("warning", title, message)
def log_debug(message: str):
    sanitized_message = str(message).replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
    print(f"::debug::{sanitized_message}")
def start_group(title: str): print(f"::group::{title}")
def end_group(): print(f"::endgroup::")

# --- Subprocess Helper ---
def run_command(
    cmd: List[str], check: bool = True, cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None, capture_output: bool = True,
    print_command: bool = True, input_data: Optional[str] = None
) -> subprocess.CompletedProcess:
    if print_command: log_debug(f"Running command: {shlex.join(cmd)}")
    process_env = os.environ.copy();
    if env: process_env.update(env)
    try:
        result = subprocess.run(
            cmd, check=check, text=True, capture_output=capture_output,
            cwd=cwd, env=process_env, input=input_data
        )
        return result
    except subprocess.CalledProcessError as e:
        log_error("CMD_FAIL", f"Command '{shlex.join(cmd)}' failed with {e.returncode}")
        if e.stdout: log_debug(f"CMD_FAIL_STDOUT:\n{e.stdout.strip()}")
        if e.stderr: log_debug(f"CMD_FAIL_STDERR:\n{e.stderr.strip()}")
        if check: raise
        return e 
    except FileNotFoundError as e:
        log_error("CMD_NOT_FOUND", f"Cmd not found: {cmd[0]}. Ensure in PATH/installed.")
        if check: raise
        return subprocess.CompletedProcess(cmd, returncode=127, stdout="", stderr=str(e))

# --- Path Debugging Helper ---
def debug_path_permissions(path_to_debug_str: str, user_context: str, as_user: Optional[str] = None):
    start_group(f"Debug Path Access: {path_to_debug_str} (context: {user_context}{f', as {as_user}' if as_user else ''})")
    path_to_debug = Path(path_to_debug_str)
    log_debug(f"[{user_context}] Checking path: {path_to_debug}")

    path_exists_cmd = ["test", "-e", str(path_to_debug)]
    path_isdir_cmd = ["test", "-d", str(path_to_debug)]
    path_isfile_cmd = ["test", "-f", str(path_to_debug)]
    path_readable_cmd = ["test", "-r", str(path_to_debug)]
    path_executable_cmd = ["test", "-x", str(path_to_debug)] # For dirs, means listable

    if as_user:
        path_exists_cmd = ["sudo", "-u", as_user] + path_exists_cmd
        path_isdir_cmd = ["sudo", "-u", as_user] + path_isdir_cmd
        path_isfile_cmd = ["sudo", "-u", as_user] + path_isfile_cmd
        path_readable_cmd = ["sudo", "-u", as_user] + path_readable_cmd
        path_executable_cmd = ["sudo", "-u", as_user] + path_executable_cmd
    
    exists_result = run_command(path_exists_cmd, check=False, print_command=True)
    if exists_result.returncode != 0:
        log_warning(f"[{user_context}{f' as {as_user}' if as_user else ''}] Path does NOT exist or is inaccessible: {path_to_debug}")
        end_group()
        return
    log_debug(f"[{user_context}{f' as {as_user}' if as_user else ''}] Path exists: {path_to_debug}")

    isdir_result = run_command(path_isdir_cmd, check=False, print_command=True)
    log_debug(f"[{user_context}{f' as {as_user}' if as_user else ''}] Is directory: {isdir_result.returncode == 0}")
    
    isfile_result = run_command(path_isfile_cmd, check=False, print_command=True)
    log_debug(f"[{user_context}{f' as {as_user}' if as_user else ''}] Is file: {isfile_result.returncode == 0}")

    readable_result = run_command(path_readable_cmd, check=False, print_command=True)
    log_debug(f"[{user_context}{f' as {as_user}' if as_user else ''}] Is readable: {readable_result.returncode == 0}")

    if isdir_result.returncode == 0 : # Only check executable if it's a directory
        executable_result = run_command(path_executable_cmd, check=False, print_command=True)
        log_debug(f"[{user_context}{f' as {as_user}' if as_user else ''}] Is executable/listable (if dir): {executable_result.returncode == 0}")

    # Try 'ls'
    ls_cmd_list = ["ls", "-lha", str(path_to_debug)]
    if as_user:
        ls_cmd_list = ["sudo", "-u", as_user] + ls_cmd_list
    
    log_debug(f"[{user_context}{f' as {as_user}' if as_user else ''}] Attempting '{' '.join(ls_cmd_list)}' (first few lines):")
    try:
        ls_result = run_command(ls_cmd_list, check=False, capture_output=True, print_command=True)
        if ls_result.returncode == 0:
            log_debug(f"[{user_context}{f' as {as_user}' if as_user else ''}] 'ls' successful. Output (first 10 lines):")
            lines = ls_result.stdout.splitlines()
            for i, line in enumerate(lines[:10]): log_debug(f"  {line}")
            if not lines: log_debug("  (ls output was empty)")
        else:
            log_warning(f"[{user_context}{f' as {as_user}' if as_user else ''}] '{' '.join(ls_cmd_list)}' FAILED with code {ls_result.returncode}.")
            if ls_result.stdout: log_debug(f"  LS STDOUT: {ls_result.stdout.strip()}")
            if ls_result.stderr: log_debug(f"  LS STDERR: {ls_result.stderr.strip()}")
    except Exception as e:
        log_warning(f"[{user_context}{f' as {as_user}' if as_user else ''}] Exception running 'ls': {e}")
    end_group()

# --- Core Functions ---
def setup_environment() -> bool:
    start_group("Setup Environment")
    log_notice("SETUP_ENV", f"Configuring env in {NVCHECKER_RUN_DIR}, {PACKAGE_BUILD_BASE_DIR}, {ARTIFACTS_DIR}...")
    dirs_to_create = [NVCHECKER_RUN_DIR, PACKAGE_BUILD_BASE_DIR] # builder will own these
    for d_path in dirs_to_create:
        try: run_command(["sudo", "-u", BUILDER_USER, "mkdir", "-p", str(d_path)])
        except: log_error("SETUP_FAIL", f"mkdir for {d_path} as {BUILDER_USER} failed."); end_group(); return False
    try: ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e: log_error("SETUP_FAIL", f"mkdir ARTIFACTS_DIR failed: {e}"); end_group(); return False
    try: run_command(["sudo", "chown", f"{BUILDER_USER}:{BUILDER_USER}", str(ARTIFACTS_DIR)], check=False)
    except: log_warning("SETUP_CHOWN_WARN", f"chown {ARTIFACTS_DIR} to {BUILDER_USER} failed.")

    scripts_to_copy = ["buildscript2.py", "aur_package_updater_cli.py"]
    scripts_source_base = GITHUB_WORKSPACE / "scripts" 
    for script_name in scripts_to_copy:
        source_path = scripts_source_base / script_name
        if not source_path.is_file(): source_path = GITHUB_WORKSPACE / script_name 
        dest_path = NVCHECKER_RUN_DIR / script_name
        if not source_path.is_file():
            log_error("SETUP_FAIL", f"{script_name} not found."); end_group(); return False
        try:
            shutil.copy(source_path, dest_path)
            run_command(["sudo", "chown", f"{BUILDER_USER}:{BUILDER_USER}", str(dest_path)])
            run_command(["sudo", "-u", BUILDER_USER, "chmod", "+x", str(dest_path)])
            log_debug(f"Copied, chowned, chmodded {script_name} to {dest_path}")
        except Exception as e:
            log_error("SETUP_FAIL", f"Failed to copy/chown/chmod {script_name}: {e}"); end_group(); return False
    log_notice("SETUP_ENV", "Environment setup SUCCEEDED."); end_group(); return True

def create_nvchecker_keyfile() -> bool:
    start_group("Create NVChecker Keyfile for Updater CLI")
    if not SECRET_GHUK_VALUE:
        log_warning("KEYFILE_SKIP", "SECRET_GHUK_VALUE not set. Skipping keyfile."); end_group(); return True 
    keyfile_content = f"[keys]\ngithub = '{SECRET_GHUK_VALUE}'\n"
    temp_keyfile_path = NVCHECKER_RUN_DIR / "temp_keyfile.toml"
    try:
        with open(temp_keyfile_path, "w") as f: f.write(keyfile_content)
        run_command(["sudo", "-u", BUILDER_USER, "mv", str(temp_keyfile_path), str(KEYFILE_PATH)])
        log_notice("KEYFILE_OK", f"{KEYFILE_PATH} created, owned by {BUILDER_USER}.")
    except Exception as e:
        log_error("KEYFILE_FAIL", f"Failed to create/move keyfile.toml: {e}")
        if temp_keyfile_path.exists(): temp_keyfile_path.unlink(missing_ok=True)
        end_group(); return False
    end_group(); return True

def run_aur_updater_cli(path_root_for_cli: str) -> Optional[List[Dict[str, Any]]]:
    start_group("Run AUR Package Updater CLI")
    script_path = NVCHECKER_RUN_DIR / "aur_package_updater_cli.py"
    if not AUR_MAINTAINER_NAME:
        log_error("AUR_UPDATER_FAIL", "AUR_MAINTAINER_NAME env var not set."); end_group(); return None

    cmd = [
        "sudo", "-E", "-u", BUILDER_USER, f"HOME={BUILDER_HOME}",
        "python3", str(script_path),
        "--maintainer", AUR_MAINTAINER_NAME,
        "--path-root", path_root_for_cli, 
        "--output-file", str(UPDATER_CLI_OUTPUT_JSON_PATH), 
    ]
    if KEYFILE_PATH.exists(): cmd.extend(["--key-toml", str(KEYFILE_PATH)])
    if os.getenv("RUNNER_DEBUG") == "1" or os.getenv("ACTIONS_STEP_DEBUG") == "true": cmd.append("--debug")

    try:
        proc_result = run_command(cmd, cwd=NVCHECKER_RUN_DIR, check=False) 
        if proc_result.stdout: log_debug(f"AUR Updater CLI STDOUT:\n{proc_result.stdout.strip()}")
        if proc_result.stderr: log_debug(f"AUR Updater CLI STDERR:\n{proc_result.stderr.strip()}")
        if proc_result.returncode != 0: log_error("AUR_UPDATER_NON_ZERO", f"aur_package_updater_cli.py exited {proc_result.returncode}.")

        if not UPDATER_CLI_OUTPUT_JSON_PATH.is_file():
            log_error("AUR_UPDATER_NO_FILE", f"{UPDATER_CLI_OUTPUT_JSON_PATH} NOT created."); end_group(); return None
        
        file_size = UPDATER_CLI_OUTPUT_JSON_PATH.stat().st_size
        if file_size < 5: 
            log_error("AUR_UPDATER_EMPTY_OUTPUT", f"{UPDATER_CLI_OUTPUT_JSON_PATH} too small (size: {file_size} bytes).")
            try:
                with open(UPDATER_CLI_OUTPUT_JSON_PATH, "r") as f_small: small_content = f_small.read()
                log_debug(f"Small file content: {small_content}")
            except Exception as read_err: log_debug(f"Could not read small file: {read_err}")
            if proc_result.returncode != 0: end_group(); return None

        with open(UPDATER_CLI_OUTPUT_JSON_PATH, "r") as f: update_data = json.load(f)
        if proc_result.returncode != 0 and update_data:
            log_warning("AUR_UPDATER_NON_ZERO_WITH_JSON", f"CLI exited {proc_result.returncode} but valid JSON parsed.")
        log_notice("AUR_UPDATER_OK", f"CLI ran. Output: {UPDATER_CLI_OUTPUT_JSON_PATH} (Size: {file_size} bytes).")
        
        artifact_path = ARTIFACTS_DIR / "updater_cli_output.json"
        try:
            run_command(["sudo", "-u", BUILDER_USER, "cp", str(UPDATER_CLI_OUTPUT_JSON_PATH), str(artifact_path)])
            log_notice("ARTIFACT_OK", f"Copied updater CLI output to artifacts: {artifact_path}")
        except Exception as e: log_warning("ARTIFACT_FAIL", f"Failed to copy output to artifacts: {e}")
        
        if isinstance(update_data, list) and update_data: log_debug(f"First Updater CLI element: {json.dumps(update_data[0], indent=2)}")
        else: log_debug(f"Updater CLI output empty/not list: {update_data}")
        end_group(); return update_data
        
    except json.JSONDecodeError as e:
        log_error("AUR_UPDATER_JSON_DECODE_FAIL", f"Failed to parse JSON: {e}. File size: {UPDATER_CLI_OUTPUT_JSON_PATH.stat().st_size if UPDATER_CLI_OUTPUT_JSON_PATH.exists() else 'N/A'}")
        if UPDATER_CLI_OUTPUT_JSON_PATH.exists():
            try:
                with open(UPDATER_CLI_OUTPUT_JSON_PATH, "r") as f_err: err_content = f_err.read()
                log_debug(f"Content of {UPDATER_CLI_OUTPUT_JSON_PATH} (failed parse):\n{err_content[:1000]}")
            except Exception as read_err: log_debug(f"Could not read errored file: {read_err}")
        end_group(); return None
    except Exception as e: 
        log_error("AUR_UPDATER_UNEXPECTED_ERROR", f"Unexpected error in CLI processing: {type(e).__name__} - {e}"); end_group(); return None

def get_packages_to_update(update_data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    start_group("Identify Packages for Update")
    pkgs_needing_update = []
    for pkg_info in update_data:
        pkgbase = pkg_info.get("pkgbase")
        if not pkgbase or not pkg_info.get("pkgfile"):
            log_warning("PKG_SKIP_INVALID", f"Skipping entry, missing pkgbase/pkgfile: {str(pkg_info)[:100]}"); continue
        if pkg_info.get("errors"):
            log_warning("PKG_HAS_ERRORS", f"Pkg {pkgbase} has updater errors: {pkg_info['errors']}. Not for auto-update."); continue
        if pkg_info.get("is_update"):
            log_notice("UPDATE_CANDIDATE", f"Pkg {pkgbase} needs update. New ver: {pkg_info.get('new_version_for_update')} from {pkg_info.get('update_source')}")
            pkgs_needing_update.append(pkg_info)
        else: log_debug(f"Pkg {pkgbase} no update by 'is_update' flag.")
    if not pkgs_needing_update: log_notice("NO_UPDATES", "No packages require updates.")
    else: log_notice("UPDATES_FOUND", f"Found {len(pkgs_needing_update)} package(s) for update.")
    end_group(); return pkgs_needing_update

def generate_package_details_for_buildscript(pkgs_info: List[Dict[str, Any]]) -> bool:
    start_group("Generate package_details.json for Build Script")
    all_details: Dict[str, Dict[str, List[str]]] = {}
    if not pkgs_info:
        log_notice("JSON_GEN_SKIP", "No packages for buildscript's JSON input.")
        try: # Write empty JSON if no packages
            with open(PACKAGE_DETAILS_JSON_PATH, "w") as f: json.dump({}, f) # Write as runner
            run_command(["sudo", "-u", BUILDER_USER, "mv", str(PACKAGE_DETAILS_JSON_PATH), str(PACKAGE_DETAILS_JSON_PATH)]) # Chown trick
        except Exception as e: log_error("JSON_GEN_EMPTY_FAIL", f"Failed to write empty {PACKAGE_DETAILS_JSON_PATH}: {e}"); end_group(); return False
        end_group(); return True

    for pkg_entry in pkgs_info:
        pkgbase = pkg_entry["pkgbase"]
        all_details[pkgbase] = {
            "depends": pkg_entry.get("depends", []),
            "makedepends": pkg_entry.get("makedepends", []),
            "checkdepends": pkg_entry.get("checkdepends", []),
            "sources": pkg_entry.get("sources", []), 
        }
        log_debug(f"Details for {pkgbase} for buildscript: deps={len(all_details[pkgbase]['depends'])}, makedeps={len(all_details[pkgbase]['makedepends'])}, checkdeps={len(all_details[pkgbase]['checkdepends'])}, sources={len(all_details[pkgbase]['sources'])}")
    
    temp_json_path = NVCHECKER_RUN_DIR / "temp_pkg_details_for_bs.json"
    try:
        with open(temp_json_path, "w") as f: json.dump(all_details, f, indent=2)
        run_command(["sudo", "-u", BUILDER_USER, "mv", str(temp_json_path), str(PACKAGE_DETAILS_JSON_PATH)])
        log_notice("JSON_GEN_OK", f"{PACKAGE_DETAILS_JSON_PATH} created, owned by {BUILDER_USER}.")
        log_debug(f"Full content of {PACKAGE_DETAILS_JSON_PATH}:\n{json.dumps(all_details, indent=2)}")
    except Exception as e:
        log_error("JSON_GEN_WRITE_FAIL", f"Failed to write/move {PACKAGE_DETAILS_JSON_PATH}: {e}")
        if temp_json_path.exists(): temp_json_path.unlink(missing_ok=True)
        end_group(); return False
    end_group(); return True

def determine_build_mode(pkgbuild_dir_rel: Path) -> str:
    parent_name = pkgbuild_dir_rel.parent.name
    if parent_name in ["build", "test"]: return parent_name
    log_warning("BUILD_MODE_UNKNOWN", f"Cannot determine build_mode from {pkgbuild_dir_rel} (parent: {parent_name}). Default 'nobuild'.")
    return "nobuild"

def execute_build_script_py(pkg_name: str, build_type: str, pkgbuild_path_rel_str: str) -> bool:
    start_group(f"Build Script Execution: {pkg_name}")
    pkg_artifact_dir = ARTIFACTS_DIR / pkg_name
    try: run_command(["sudo", "-u", BUILDER_USER, f"HOME={BUILDER_HOME}", "mkdir", "-p", str(pkg_artifact_dir)])
    except:
        log_error("BUILD_SCRIPT_MKDIR_FAIL", f"Failed to create artifact subdir {pkg_artifact_dir} for {pkg_name}.")
        if GITHUB_STEP_SUMMARY_FILE:
            with open(GITHUB_STEP_SUMMARY_FILE, "a", encoding="utf-8") as f:
                f.write(f"| **{pkg_name}** | N/A | ❌ Error: Artifact dir creation | - | - | N/A |\n")
        end_group(); return False

    log_notice("BUILD_SCRIPT_PY_EXEC", f"Starting buildscript2.py for {pkg_name} (Type: {build_type}, Path: {pkgbuild_path_rel_str})")
    git_cfgs = [
        ["sudo", "-E", "-u", BUILDER_USER, f"HOME={BUILDER_HOME}", "git", "config", "--global", "user.name", GIT_COMMIT_USER_NAME],
        ["sudo", "-E", "-u", BUILDER_USER, f"HOME={BUILDER_HOME}", "git", "config", "--global", "user.email", GIT_COMMIT_USER_EMAIL],
    ]
    for cfg_cmd in git_cfgs:
        try: run_command(cfg_cmd, print_command=False)
        except: log_warning("GIT_CONFIG_FAIL", f"Failed to set {cfg_cmd[-2]}. Build script might fail.")

    bs_exe = NVCHECKER_RUN_DIR / "buildscript2.py"
    bs_cmd = [
        "sudo", "-E", "-u", BUILDER_USER, f"HOME={BUILDER_HOME}", "python3", str(bs_exe),
        "--github-repo", GITHUB_REPOSITORY, "--github-token", GH_TOKEN,
        "--github-workspace", str(GITHUB_WORKSPACE), "--package-name", pkg_name,
        "--depends-json", str(PACKAGE_DETAILS_JSON_PATH), "--pkgbuild-path", pkgbuild_path_rel_str,
        "--commit-message", f"CI: Auto update {pkg_name}", "--build-mode", build_type,
        "--artifacts-dir", str(pkg_artifact_dir), "--base-build-dir", str(PACKAGE_BUILD_BASE_DIR),
    ]
    if os.getenv("RUNNER_DEBUG") == "1" or os.getenv("ACTIONS_STEP_DEBUG") == "true": bs_cmd.append("--debug")

    bs_json_str, bs_ok, bs_ver, bs_chg_str, bs_err = "", False, "N/A", "➖ No", ""
    try:
        bs_proc_res = run_command(bs_cmd, cwd=NVCHECKER_RUN_DIR)
        bs_json_str = bs_proc_res.stdout.strip()
        if not bs_json_str:
            if bs_proc_res.returncode == 0: raise ValueError("buildscript2.py exited 0 but no JSON output.")
            else: raise ValueError(f"buildscript2.py exited {bs_proc_res.returncode} and no JSON output.")
        bs_data = json.loads(bs_json_str)
        bs_ok, bs_ver = bs_data.get("success", False), bs_data.get("version", "N/A")
        if bs_data.get("changes_detected", False): bs_chg_str = "✔️ Yes"
        bs_err = bs_data.get("error_message", "")
        if bs_proc_res.returncode != 0 and bs_ok:
            log_warning("BUILD_SCRIPT_EXIT_MISMATCH", f"buildscript2.py exited {bs_proc_res.returncode} but JSON reported success.")
        elif bs_proc_res.returncode == 0 and not bs_ok and not bs_err:
            bs_err = "buildscript2.py exited 0 but JSON reported !success w/o error_message."
            log_warning("BUILD_SCRIPT_SUCCESS_MISMATCH", bs_err)
        if bs_proc_res.returncode != 0 and not bs_err:
            bs_err = f"buildscript2.py cmd failed (exit {bs_proc_res.returncode}) no specific JSON error."
            bs_ok = False
    except (json.JSONDecodeError, ValueError) as e:
        bs_err = f"Failed parse/validate JSON from buildscript2.py: {e}. Raw: '{bs_json_str[:200]}...'"; bs_ok = False
        log_error("BUILD_SCRIPT_PY_FAIL_JSON", bs_err)
    except Exception as e:
        bs_err = f"Unexpected error running/parsing buildscript2.py: {e}"; bs_ok = False
        log_error("BUILD_SCRIPT_PY_FAIL_UNEX", bs_err)

    status_md = "✅ Success" if bs_ok else (f"❌ Failure: <small>{bs_err.replace('|', '\\|').replace(chr(10), '<br>')}</small>" if bs_err else "❌ Failure")
    aur_link = f"[AUR](https://aur.archlinux.org/packages/{pkg_name})"
    log_link = "N/A"
    if pkg_artifact_dir.exists() and list(pkg_artifact_dir.glob(f"{pkg_name}*.log")) + list(pkg_artifact_dir.glob("*.log")):
        log_link = f"See 'build-artifacts-{GITHUB_RUN_ID}' (<tt>{pkg_name}/</tt> subdir)"
    if GITHUB_STEP_SUMMARY_FILE:
        with open(GITHUB_STEP_SUMMARY_FILE, "a", encoding="utf-8") as f:
            f.write(f"| **{pkg_name}** | {bs_ver} | {status_md} | {bs_chg_str} | {aur_link} | {log_link} |\n")
    end_group(); return bs_ok

def main():
    log_notice("SCRIPT_START", "Arch Package Update Task started (Python v2).")
    if GITHUB_STEP_SUMMARY_FILE:
        with open(GITHUB_STEP_SUMMARY_FILE, "w", encoding="utf-8") as f:
            f.write("## Arch Package Build Summary\n| Package | Version | Status | Changes | AUR Link | Build Logs |\n|---|---|---|---|---|---|\n")
    else: log_warning("NO_SUMMARY_FILE", "GITHUB_STEP_SUMMARY env var not set. Summary will not be written.")

    crit_vars = {"AUR_MAINTAINER_NAME": AUR_MAINTAINER_NAME, "GH_TOKEN": GH_TOKEN,
                 "GITHUB_REPOSITORY": GITHUB_REPOSITORY, "SECRET_GHUK_VALUE": SECRET_GHUK_VALUE}
    missing = [k for k, v in crit_vars.items() if not v]
    if missing:
        msg = f"Missing critical env vars: {', '.join(missing)}. Exiting."
        log_error("CRITICAL_ENV_MISSING", msg)
        if GITHUB_STEP_SUMMARY_FILE:
             with open(GITHUB_STEP_SUMMARY_FILE, "a", encoding="utf-8") as f: f.write(f"| **SETUP** | N/A | ❌ Failure: {msg} | - | - | Check Logs |\n")
        sys.exit(1)

    if not setup_environment():
        log_error("MAIN_FAIL", "setup_environment FAILED.")
        if GITHUB_STEP_SUMMARY_FILE:
             with open(GITHUB_STEP_SUMMARY_FILE, "a", encoding="utf-8") as f: f.write("| **SETUP** | N/A | ❌ Failure: Env setup | - | - | Check Logs |\n")
        sys.exit(1)

    # Determine path_root_for_cli logic
    path_root_for_cli_actual = PKGBUILD_ROOT_PATH_STR if PKGBUILD_ROOT_PATH_STR else str(GITHUB_WORKSPACE)
    log_notice("PATH_ROOT_CONFIG", f"Using --path-root='{path_root_for_cli_actual}' for Updater CLI (derived from PKGBUILD_ROOT: '{PKGBUILD_ROOT_PATH_STR}')")

    # Pre-CLI Debugging of the path_root_for_cli_actual
    log_notice("PRE_CLI_DEBUG", f"Debugging PKGBUILD_ROOT ('{path_root_for_cli_actual}') access before Updater CLI.")
    debug_path_permissions(path_root_for_cli_actual, "runner") # As runner user
    debug_path_permissions(path_root_for_cli_actual, BUILDER_USER, as_user=BUILDER_USER) # As builder user

    # Example of debugging a specific known package subdirectory and PKGBUILD file
    # This should be adapted if "lobe-chat" is not always the package to check
    # For general use, this specific subdir check might be too specific or commented out.
    # known_pkg_name_for_debug = "lobe-chat" 
    # known_pkg_dir_for_debug = Path(path_root_for_cli_actual) / known_pkg_name_for_debug
    # log_notice("PRE_CLI_DEBUG_SUBDIR", f"Debugging specific subdir: {known_pkg_dir_for_debug}")
    # debug_path_permissions(str(known_pkg_dir_for_debug), "runner")
    # debug_path_permissions(str(known_pkg_dir_for_debug), BUILDER_USER, as_user=BUILDER_USER)
    # known_pkgbuild_file_for_debug = known_pkg_dir_for_debug / "PKGBUILD"
    # debug_path_permissions(str(known_pkgbuild_file_for_debug), "runner")
    # debug_path_permissions(str(known_pkgbuild_file_for_debug), BUILDER_USER, as_user=BUILDER_USER)

    if not create_nvchecker_keyfile():
        log_warning("MAIN_WARN", "create_nvchecker_keyfile had issues. Updater CLI might have limited functionality.")

    updater_data = run_aur_updater_cli(path_root_for_cli_actual)
    if updater_data is None: 
        log_error("MAIN_FAIL", "run_aur_updater_cli FAILED to produce data.")
        if GITHUB_STEP_SUMMARY_FILE:
             with open(GITHUB_STEP_SUMMARY_FILE, "a", encoding="utf-8") as f: f.write("| **SETUP** | N/A | ❌ Failure: AUR Updater CLI | - | - | Check Logs |\n")
        sys.exit(1)

    pkgs_to_build = get_packages_to_update(updater_data)
    if not pkgs_to_build:
        log_notice("MAIN_NO_UPDATES", "No packages to update. Exiting successfully.")
        if GITHUB_STEP_SUMMARY_FILE:
            with open(GITHUB_STEP_SUMMARY_FILE, "a", encoding="utf-8") as f: f.write("| *No updates found* | - | - | - | - | - |\n")
        sys.exit(0)

    if not generate_package_details_for_buildscript(pkgs_to_build):
        log_error("MAIN_FAIL", "generate_package_details_for_buildscript FAILED.")
        if GITHUB_STEP_SUMMARY_FILE:
             with open(GITHUB_STEP_SUMMARY_FILE, "a", encoding="utf-8") as f: f.write("| **SETUP** | N/A | ❌ Failure: Input for buildscript2.py | - | - | Check Logs |\n")
        sys.exit(1)
    
    start_group("Build Packages Loop")
    overall_build_ok = True
    for pkg_info in pkgs_to_build:
        pkgbase = pkg_info["pkgbase"]
        pkgbuild_file_abs_str = pkg_info["pkgfile"]
        pkgbuild_dir_abs = Path(pkgbuild_file_abs_str).parent
        try: pkgbuild_dir_rel = pkgbuild_dir_abs.relative_to(GITHUB_WORKSPACE)
        except ValueError:
            err = f"PKGBUILD path {pkgbuild_dir_abs} for {pkgbase} not relative to GITHUB_WORKSPACE {GITHUB_WORKSPACE}. Skipping."
            log_error("BUILD_LOOP_PATH_ERR", err)
            if GITHUB_STEP_SUMMARY_FILE:
                 with open(GITHUB_STEP_SUMMARY_FILE, "a", encoding="utf-8") as f: f.write(f"| **{pkgbase}** | N/A | ❌ Error: Invalid PKGBUILD path | - | - | N/A |\n")
            overall_build_ok = False; continue
        build_mode = determine_build_mode(pkgbuild_dir_rel)
        if not execute_build_script_py(pkgbase, build_mode, str(pkgbuild_dir_rel)):
            log_error("BUILD_LOOP_PKG_FAIL", f"Build script FAILED for {pkgbase}.")
            overall_build_ok = False
    end_group()

    if not overall_build_ok:
        log_error("MAIN_EXEC_BUILD_FAIL", "One or more packages failed to build/had errors. See summary/logs."); sys.exit(1)
    log_notice("MAIN_EXEC_SUCCESS", "All tasks completed successfully."); sys.exit(0)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", stream=sys.stderr)
    try: main()
    except Exception as e:
        error_title = "UNHANDLED_FATAL_EXCEPTION"
        error_message = f"Critical unhandled exception in main script: {type(e).__name__} - {e}"
        try: log_error(error_title, error_message)
        except: print(f"::error title={error_title}::{error_message}", file=sys.stderr)
        if GITHUB_STEP_SUMMARY_FILE and Path(GITHUB_STEP_SUMMARY_FILE).exists():
             try:
                with open(GITHUB_STEP_SUMMARY_FILE, "a", encoding="utf-8") as f:
                    f.write(f"| **CRITICAL** | N/A | ❌ Failure: Unhandled script error: {e} | - | - | Check Logs |\n")
             except: pass
        sys.exit(2)
