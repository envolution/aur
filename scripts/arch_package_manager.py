#!/usr/bin/env python3

import argparse
import base64
import dataclasses
import glob
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple, Union

# External dependencies (ensure installed in GHA via pacman)
import requests
from looseversion import LooseVersion

# --- Constants ---
BUILDER_USER = "builder"
BUILDER_HOME = Path(os.getenv("BUILDER_HOME_OVERRIDE", f"/home/{BUILDER_USER}")) # Allow override for local testing
GITHUB_WORKSPACE = Path(os.getenv("GITHUB_WORKSPACE", "/github/workspace")) # GHA default
NVCHECKER_RUN_TEMP_DIR_BASE = BUILDER_HOME / "nvchecker_run"
PACKAGE_BUILD_TEMP_DIR_BASE = BUILDER_HOME / "pkg_builds"
ARTIFACTS_OUTPUT_DIR_BASE = GITHUB_WORKSPACE / "artifacts" # Script will create subdirs per package

# --- GitHub Actions Logging Helpers ---
def _log_gha(level: str, title: str, message: str, file: Optional[str] = None, line: Optional[str] = None, end_line: Optional[str] = None, col: Optional[str] = None, end_column: Optional[str] = None):
    """More comprehensive GHA logger."""
    sanitized_message = str(message).replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
    sanitized_title = str(title).replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
    
    props = [f"title={sanitized_title}"]
    if file: props.append(f"file={file}")
    if line: props.append(f"line={line}")
    if end_line: props.append(f"endLine={end_line}")
    if col: props.append(f"col={col}")
    if end_column: props.append(f"endColumn={end_column}")
    
    print(f"::{level} {','.join(props)}::{sanitized_message}")

def log_notice(title: str, message: str, **kwargs): _log_gha("notice", title, message, **kwargs)
def log_error(title: str, message: str, **kwargs): _log_gha("error", title, message, **kwargs)
def log_warning(title: str, message: str, **kwargs): _log_gha("warning", title, message, **kwargs)
def log_debug(message: str, **kwargs):
    if os.getenv("ACTIONS_STEP_DEBUG", "false").lower() == "true":
        sanitized_message = str(message).replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
        print(f"::debug::{sanitized_message}")
def start_group(title: str): print(f"::group::{title}")
def end_group(): print(f"::endgroup::")


# --- Configuration Dataclass ---
@dataclasses.dataclass
class Config:
    aur_maintainer_name: str
    github_repo: str # Format: "owner/repo"
    pkgbuild_files_root_in_workspace: Path # Absolute path to PKGBUILD root in GHA workspace
    git_commit_user_name: str
    git_commit_user_email: str
    
    gh_token: str
    # aur_ssh_key_path: Optional[Path] # Managed by GHA workflow setup now

    # Derived/Internal Paths
    nvchecker_run_dir: Path = NVCHECKER_RUN_TEMP_DIR_BASE
    package_build_base_dir: Path = PACKAGE_BUILD_TEMP_DIR_BASE
    artifacts_dir_base: Path = ARTIFACTS_OUTPUT_DIR_BASE # Package-specific subdirs will be created

    # Output files for status/input
    package_status_report_path: Path = NVCHECKER_RUN_TEMP_DIR_BASE / "package_status_report.json"
    package_build_inputs_path: Path = NVCHECKER_RUN_TEMP_DIR_BASE / "package_build_inputs.json"
    nvchecker_keyfile_path: Path = NVCHECKER_RUN_TEMP_DIR_BASE / "nv_keyfile.toml"

    debug_mode: bool = os.getenv("ACTIONS_STEP_DEBUG", "false").lower() == "true"
    github_run_id: Optional[str] = os.getenv("GITHUB_RUN_ID_FOR_ARTIFACTS")
    github_step_summary_file: Optional[Path] = Path(os.getenv("GITHUB_STEP_SUMMARY_FILE_PATH")) if os.getenv("GITHUB_STEP_SUMMARY_FILE_PATH") else None

# --- Data Structures ---
@dataclasses.dataclass
class PKGBUILDInfo:
    pkgbase: Optional[str] = None
    pkgname: Optional[str] = None # Typically first from pkgname=() or same as pkgbase
    pkgver: Optional[str] = None
    pkgrel: Optional[str] = None
    depends: List[str] = dataclasses.field(default_factory=list)
    makedepends: List[str] = dataclasses.field(default_factory=list)
    checkdepends: List[str] = dataclasses.field(default_factory=list)
    sources: List[str] = dataclasses.field(default_factory=list)
    pkgfile_abs_path: Optional[Path] = None
    error: Optional[str] = None

@dataclasses.dataclass
class PackageOverallStatus:
    pkgbase: str
    pkgname_display: str # Best available name for display

    local_pkgbuild_info: Optional[PKGBUILDInfo] = None
    
    aur_version_str: Optional[str] = None # "pkgver-pkgrel"
    aur_pkgver: Optional[str] = None
    aur_pkgrel: Optional[str] = None
    aur_actual_pkgname: Optional[str] = None # The Name field from AUR for this PkgBase

    nvchecker_new_version: Optional[str] = None # Upstream version from nvchecker
    nvchecker_event: Optional[str] = None
    nvchecker_raw_log: Optional[Dict[str, Any]] = None

    comparison_errors: List[str] = dataclasses.field(default_factory=list)
    is_update_candidate: bool = True # Can this package be considered for update?
    needs_update: bool = False
    update_source_type: Optional[str] = None # "aur" or "nvchecker"
    version_for_update: Optional[str] = None # Full version string (pkgver or pkgver-pkgrel)
    local_is_ahead: bool = False
    comparison_log: Dict[str, str] = dataclasses.field(default_factory=dict)

    # For build process
    pkgbuild_dir_rel_to_workspace: Optional[Path] = None # Path like "category/packagename"

@dataclasses.dataclass
class BuildOpResult:
    package_name: str
    success: bool = False # Default to False, explicitly set to True on full success
    target_version_for_build: Optional[str] = None # The version we aimed to build (e.g., "1.2.3-1")
    final_pkgbuild_version_in_clone: Optional[str] = None # Version read from PKGBUILD in clone *before* AUR commit
    built_package_archive_files: List[Path] = dataclasses.field(default_factory=list) # Absolute paths to *.pkg.tar.zst files
    
    # Flags for specific successful operations
    setup_env_ok: bool = False
    dependencies_installed_ok: bool = False
    pkgbuild_versioned_ok: bool = False
    makepkg_ran_ok: bool = False # If makepkg itself was invoked and succeeded
    local_install_ok: bool = False
    
    changes_made_to_aur_clone_files: bool = False # If PKGBUILD, .SRCINFO etc. were modified
    git_commit_to_aur_ok: bool = False
    git_push_to_aur_ok: bool = False
    github_release_ok: bool = False
    source_repo_sync_ok: bool = False # Overall status of syncing back
    files_synced_to_source_repo: List[str] = dataclasses.field(default_factory=list) # List of files synced

    error_message: Optional[str] = None
    log_artifact_subdir: Optional[Path] = None # Relative to ARTIFACTS_OUTPUT_DIR_BASE
    # Store the unique build directory path for cleanup and reference
    package_specific_build_dir_abs: Optional[Path] = None
    aur_clone_dir_abs: Optional[Path] = None


# --- Command Runner ---
class CommandRunner:
    def __init__(self, logger: logging.Logger, default_user: Optional[str] = None, default_home: Optional[Path] = None):
        self.logger = logger
        self.default_user = default_user
        self.default_home = default_home

    def run(self, cmd_list: List[str], check: bool = True, cwd: Optional[Path] = None,
            env_extra: Optional[Dict[str, str]] = None, capture_output: bool = True,
            print_command: bool = True, input_data: Optional[str] = None,
            run_as_user: Optional[str] = None, user_home_dir: Optional[Path] = None
            ) -> subprocess.CompletedProcess:
        
        user_to_run = run_as_user if run_as_user is not None else self.default_user
        home_for_run = user_home_dir if user_home_dir is not None else self.default_home

        final_cmd = list(cmd_list)
        current_env = os.environ.copy()

        if user_to_run:
            sudo_prefix = ["sudo", "-E", "-u", user_to_run]
            if home_for_run:
                sudo_prefix.append(f"HOME={str(home_for_run)}")
            final_cmd = sudo_prefix + final_cmd
            # Some env vars might need to be explicitly passed for sudo -E if not in sudoers secure_path or env_keep
            # For gh, git, nvchecker, HOME is often key. Python path might also be.
            # The GHA workflow PATH should be fine for root, builder needs its own.
            if user_to_run == BUILDER_USER: # Ensure builder has a sane PATH
                current_env["PATH"] = f"/usr/local/bin:/usr/bin:/bin:{home_for_run / '.local/bin' if home_for_run else ''}"


        if env_extra:
            current_env.update(env_extra)
        
        if print_command:
            log_debug(f"Running command: {shlex.join(final_cmd)} (CWD: {cwd or '.'})")

        try:
            result = subprocess.run(
                final_cmd, check=check, text=True, capture_output=capture_output,
                cwd=cwd, env=current_env, input=input_data, timeout=300 # 5 min timeout
            )
            if result.stdout and print_command and capture_output: log_debug(f"CMD STDOUT: {result.stdout.strip()[:500]}")
            if result.stderr and print_command and capture_output: log_debug(f"CMD STDERR: {result.stderr.strip()[:500]}")
            return result
        except subprocess.CalledProcessError as e:
            log_error("CMD_FAIL", f"Command '{shlex.join(e.cmd)}' failed with RC {e.returncode}")
            if e.stdout: log_debug(f"FAIL STDOUT: {e.stdout.strip()}")
            if e.stderr: log_debug(f"FAIL STDERR: {e.stderr.strip()}")
            if check: raise
            return e
        except subprocess.TimeoutExpired as e:
            log_error("CMD_TIMEOUT", f"Command '{shlex.join(final_cmd)}' timed out.")
            if check: raise
            # Synthesize a CompletedProcess for timeout
            return subprocess.CompletedProcess(args=final_cmd, returncode=124, stdout=e.stdout or "", stderr=e.stderr or "TimeoutExpired")
        except FileNotFoundError as e:
            log_error("CMD_NOT_FOUND", f"Command not found: {final_cmd[0]}. Ensure it's installed and in PATH.")
            if check: raise
            return subprocess.CompletedProcess(args=final_cmd, returncode=127, stdout="", stderr=str(e))
        except Exception as e:
            log_error("CMD_UNEXPECTED_ERROR", f"Unexpected error running '{shlex.join(final_cmd)}': {type(e).__name__} - {e}")
            if check: raise
            return subprocess.CompletedProcess(args=final_cmd, returncode=1, stdout="", stderr=str(e))

# --- PKGBUILD Parser (Sources variables from PKGBUILD files) ---
class PKGBUILDParser:
    def __init__(self, runner: CommandRunner, logger: logging.Logger):
        self.runner = runner # Uses root usually to read files, then builder to source? Or just builder?
                            # Safest: Copy to a temp dir owned by builder, then source.
                            # For now: Assumes files are readable by user running the sourcing.
        self.logger = logger

    def _parse_variable_section(self, lines: List[str], start_marker: str, end_marker: str, is_array: bool) -> Union[Optional[str], List[str]]:
        try:
            start_idx = lines.index(start_marker)
            end_idx = lines.index(end_marker)
            values = [line.strip() for line in lines[start_idx + 1:end_idx] if line.strip()]
            if is_array:
                return values
            return values[0] if values else None
        except ValueError: # Marker not found
            return [] if is_array else None

    def _source_and_extract_pkgbuild_vars(self, pkgbuild_file_path: Path) -> PKGBUILDInfo:
        """Sources a single PKGBUILD using a bash script and extracts variables."""
        info = PKGBUILDInfo(pkgfile_abs_path=pkgbuild_file_path)
        
        # Bash script to source PKGBUILD and echo variables
        # Ensure PKGBUILD path is escaped for shell
        escaped_pkgbuild_path = shlex.quote(str(pkgbuild_file_path))
        bash_script = f"""
        set -e
        PKGBUILD_DIR=$(dirname {escaped_pkgbuild_path})
        cd "$PKGBUILD_DIR" # Source from its own directory context

        # Unset common variables to avoid interference from environment
        unset pkgbase pkgname pkgver pkgrel epoch depends makedepends checkdepends source
        
        . {escaped_pkgbuild_path} # Source the PKGBUILD

        echo "PKGBASE_START"; echo "${{pkgbase:-}}"; echo "PKGBASE_END"
        echo "PKGNAME_START"; echo "${{pkgname[0]:-${{pkgbase:-}}}}"; echo "PKGNAME_END" # Default to first pkgname or pkgbase
        echo "PKGVER_START";  echo "${{pkgver:-}}"; echo "PKGVER_END"
        echo "PKGREL_START";  echo "${{pkgrel:-}}"; echo "PKGREL_END"
        
        echo "DEPENDS_START";      if [ "${{#depends[@]}}" -gt 0 ]; then printf '%s\\n' "${{depends[@]}}"; fi; echo "DEPENDS_END"
        echo "MAKEDEPENDS_START";  if [ "${{#makedepends[@]}}" -gt 0 ]; then printf '%s\\n' "${{makedepends[@]}}"; fi; echo "MAKEDEPENDS_END"
        echo "CHECKDEPENDS_START"; if [ "${{#checkdepends[@]}}" -gt 0 ]; then printf '%s\\n' "${{checkdepends[@]}}"; fi; echo "CHECKDEPENDS_END"
        echo "SOURCES_START";      if [ "${{#source[@]}}" -gt 0 ]; then printf '%s\\n' "${{source[@]}}"; fi; echo "SOURCES_END"
        """
        try:
            # Run bash script as builder user for safety, if PKGBUILDs are from workspace
            # Files must be readable by builder user.
            result = self.runner.run(
                ['bash', '-c', bash_script],
                check=False, # Parse output even on error to capture stderr
                run_as_user=BUILDER_USER, # Important: source PKGBUILDs as non-root
                user_home_dir=BUILDER_HOME
            )

            if result.returncode != 0:
                info.error = f"Bash script failed (RC {result.returncode}): {result.stderr[:200]}"
                self.logger.warning(f"Failed to source {pkgbuild_file_path}: {info.error}")
                return info

            output_lines = result.stdout.splitlines()
            
            info.pkgbase = self._parse_variable_section(output_lines, "PKGBASE_START", "PKGBASE_END", False)
            info.pkgname = self._parse_variable_section(output_lines, "PKGNAME_START", "PKGNAME_END", False)
            info.pkgver = self._parse_variable_section(output_lines, "PKGVER_START", "PKGVER_END", False)
            info.pkgrel = self._parse_variable_section(output_lines, "PKGREL_START", "PKGREL_END", False)
            info.depends = self._parse_variable_section(output_lines, "DEPENDS_START", "DEPENDS_END", True)
            info.makedepends = self._parse_variable_section(output_lines, "MAKEDEPENDS_START", "MAKEDEPENDS_END", True)
            info.checkdepends = self._parse_variable_section(output_lines, "CHECKDEPENDS_START", "CHECKDEPENDS_END", True)
            info.sources = self._parse_variable_section(output_lines, "SOURCES_START", "SOURCES_END", True)

            if not info.pkgbase: # Critical if pkgbase is missing
                 info.error = "pkgbase could not be extracted."
                 self.logger.warning(f"No pkgbase from {pkgbuild_file_path}")
            
        except Exception as e:
            info.error = f"Exception sourcing PKGBUILD: {type(e).__name__} - {e}"
            self.logger.error(f"Error processing {pkgbuild_file_path}: {info.error}", exc_info=True)
        
        return info

    def fetch_all_local_pkgbuild_data(self, pkgbuild_root_dir: Path) -> Dict[str, PKGBUILDInfo]:
        """Finds all PKGBUILD files and parses them."""
        start_group("Parsing Local PKGBUILD Files")
        self.logger.info(f"Searching for PKGBUILDs in {pkgbuild_root_dir}...")
        
        pkgbuild_files = list(pkgbuild_root_dir.glob("**/PKGBUILD"))
        self.logger.info(f"Found {len(pkgbuild_files)} PKGBUILD file(s).")

        results_by_pkgbase: Dict[str, PKGBUILDInfo] = {}
        if not pkgbuild_files:
            self.logger.warning("No PKGBUILD files found.")
            end_group()
            return results_by_pkgbase

        # Parallel processing
        # Note: runner is passed to the class, not per method call, if it's always the same.
        # Here, _source_and_extract_pkgbuild_vars is a method, so it has self.
        num_workers = os.cpu_count() or 1
        with ProcessPoolExecutor(max_workers=num_workers) as executor:
            future_to_path = {executor.submit(self._source_and_extract_pkgbuild_vars, path): path for path in pkgbuild_files}
            for future in as_completed(future_to_path):
                pkg_info = future.result()
                if pkg_info.error or not pkg_info.pkgbase:
                    self.logger.warning(f"Skipping {pkg_info.pkgfile_abs_path} due to error: {pkg_info.error or 'No pkgbase'}")
                    continue
                
                if pkg_info.pkgbase in results_by_pkgbase:
                    self.logger.warning(f"Duplicate pkgbase '{pkg_info.pkgbase}' found. Overwriting entry from {results_by_pkgbase[pkg_info.pkgbase].pkgfile_abs_path} with {pkg_info.pkgfile_abs_path}")
                results_by_pkgbase[pkg_info.pkgbase] = pkg_info
                self.logger.debug(f"Parsed local: {pkg_info.pkgbase} (v{pkg_info.pkgver}-{pkg_info.pkgrel}) from {pkg_info.pkgfile_abs_path.name}")
        
        self.logger.info(f"Successfully parsed data for {len(results_by_pkgbase)} unique pkgbase(s).")
        end_group()
        return results_by_pkgbase

# --- AUR Info Fetcher ---
class AURInfoFetcher:
    def __init__(self, logger: logging.Logger):
        self.logger = logger

    def fetch_data_for_maintainer(self, maintainer: str) -> Dict[str, Dict[str, str]]:
        """Fetches package data from AUR for a given maintainer."""
        start_group(f"Fetching AUR Data for Maintainer: {maintainer}")
        aur_data_by_pkgbase: Dict[str, Dict[str, str]] = {}
        url = f"https://aur.archlinux.org/rpc/v5/search/{maintainer}?by=maintainer"
        self.logger.info(f"Querying AUR: {url}")
        try:
            response = requests.get(url, timeout=20)
            response.raise_for_status()
            data = response.json()

            if data.get("type") == "error":
                self.logger.error(f"AUR API error: {data.get('error')}")
                end_group(); return aur_data_by_pkgbase
            if data.get("resultcount", 0) == 0:
                self.logger.info(f"No packages found on AUR for maintainer '{maintainer}'.")
                end_group(); return aur_data_by_pkgbase

            for result in data.get("results", []):
                pkgbase = result.get("PackageBase")
                aur_name = result.get("Name")
                full_ver = result.get("Version")
                if not pkgbase or not full_ver: continue

                # Strip epoch if present "epoch:pkgver-pkgrel"
                ver_no_epoch = full_ver.split(':', 1)[-1]
                parts = ver_no_epoch.rsplit('-', 1)
                base_v, rel_v = parts[0], (parts[1] if len(parts) > 1 and parts[1].isdigit() else "0")
                
                if pkgbase not in aur_data_by_pkgbase: # Prioritize first entry if multiple pkgnames share a pkgbase
                    aur_data_by_pkgbase[pkgbase] = {
                        "aur_pkgver": base_v,
                        "aur_pkgrel": rel_v,
                        "aur_actual_pkgname": aur_name, # The actual Name from AUR (can differ from PkgBase for -git etc.)
                        "aur_version_str": f"{base_v}-{rel_v}" if rel_v != "0" else base_v
                    }
                    self.logger.debug(f"  AUR: {pkgbase} (Name: {aur_name}) -> v{base_v}-{rel_v}")
            
            self.logger.info(f"Fetched info for {len(aur_data_by_pkgbase)} unique PkgBase(s) from AUR.")

        except requests.Timeout:
            self.logger.error("AUR query timed out.")
        except requests.RequestException as e:
            self.logger.error(f"AUR query failed: {e}")
        except json.JSONDecodeError as e:
            self.logger.error(f"Failed to parse AUR JSON response: {e}")
        
        end_group()
        return aur_data_by_pkgbase

# --- NVChecker Runner ---
class NVCheckerRunner:
    def __init__(self, runner: CommandRunner, logger: logging.Logger, config: Config):
        self.runner = runner
        self.logger = logger
        self.config = config # For paths like nvchecker_run_dir, keyfile_path

    def _generate_nvchecker_keyfile(self) -> Optional[Path]:
        """Creates nvchecker keyfile from GH_TOKEN if available."""
        if not self.config.gh_token:
            log_debug("No GH_TOKEN provided, nvchecker will run without GitHub keys.")
            return None
        
        keyfile_content = f"[keys]\ngithub = '{self.config.gh_token}'\n"
        try:
            self.config.nvchecker_keyfile_path.parent.mkdir(parents=True, exist_ok=True)
            # Write as current user, then chown if necessary for builder, or just ensure builder can read.
            # Since runner runs as builder, this should be fine.
            with open(self.config.nvchecker_keyfile_path, "w") as f:
                f.write(keyfile_content)
            # Ensure builder owns this file
            self.runner.run(["chown", f"{BUILDER_USER}:{BUILDER_USER}", str(self.config.nvchecker_keyfile_path)], run_as_user="root", check=False) # chown as root
            self.logger.info(f"NVChecker keyfile created at {self.config.nvchecker_keyfile_path} using GH_TOKEN.")
            return self.config.nvchecker_keyfile_path
        except Exception as e:
            self.logger.error(f"Failed to create NVChecker keyfile: {e}")
            return None

    def run_global_nvchecker(self, pkgbuild_root_dir: Path, oldver_data_for_nvchecker: Dict[str, Dict[str, str]]) -> Dict[str, Dict[str, Any]]:
        """Runs nvchecker across all .nvchecker.toml files found."""
        start_group("Running Global NVChecker")
        results_by_pkgbase: Dict[str, Dict[str, Any]] = {}
        
        toml_files = list(pkgbuild_root_dir.glob("**/.*nvchecker.toml")) # Catches .nvchecker.toml and .nvchecker.ini etc.
        if not toml_files:
            self.logger.info("No .nvchecker.toml files found. Skipping global NVChecker run.")
            end_group(); return results_by_pkgbase
        
        self.logger.info(f"Found {len(toml_files)} .nvchecker.toml files.")

        keyfile_to_use = self._generate_nvchecker_keyfile() # Generates if token exists

        # NVChecker needs to run in a temp dir where it can write oldver/newver json
        # and the concatenated toml.
        # self.config.nvchecker_run_dir is already builder-owned.
        
        # Create inputs in the designated nvchecker_run_dir
        all_nv_tomls_path = self.config.nvchecker_run_dir / "all_project_nv.toml"
        oldver_json_path = self.config.nvchecker_run_dir / "oldver.json"
        newver_json_path = self.config.nvchecker_run_dir / "newver.json" # nvchecker reads this if it exists, writes to it

        try:
            # Concatenate all .toml files
            content = ["[__config__]\n", f"oldver = '{oldver_json_path.name}'\n", f"newver = '{newver_json_path.name}'\n\n"]
            for tf_path in toml_files:
                try:
                    # Ensure builder can read these toml files from workspace
                    rel_path_from_workspace = tf_path.relative_to(GITHUB_WORKSPACE)
                    content.extend([f"# Source: {rel_path_from_workspace}\n", tf_path.read_text(), "\n\n"])
                except Exception as e:
                    self.logger.warning(f"Error reading {tf_path}: {e}. Skipping.")
                    continue
            all_nv_tomls_path.write_text("".join(content))

            # Write oldver.json
            with open(oldver_json_path, "w") as f:
                json.dump({"version": 2, "data": oldver_data_for_nvchecker}, f)
            log_debug(f"Oldver JSON for NVChecker: {json.dumps(oldver_data_for_nvchecker, indent=2, sort_keys=True)[:500]}")

            # Ensure newver.json is empty if it exists, or nvchecker will use its contents
            if newver_json_path.exists(): newver_json_path.unlink()
            newver_json_path.write_text("{}")


            cmd = ['nvchecker', '-c', all_nv_tomls_path.name, '--logger=json']
            if keyfile_to_use and keyfile_to_use.is_file():
                cmd.extend(['-k', keyfile_to_use.name]) # Use relative name as running in that CWD

            self.logger.info(f"Running NVChecker with concatenated TOML... (CWD: {self.config.nvchecker_run_dir})")
            
            # Run nvchecker as builder user from its run_dir
            proc = self.runner.run(cmd, cwd=self.config.nvchecker_run_dir, check=False, run_as_user=BUILDER_USER, user_home_dir=BUILDER_HOME)
            
            if proc.returncode != 0:
                self.logger.error(f"NVChecker exited with code {proc.returncode}.")
            if proc.stderr:
                self.logger.warning(f"NVChecker STDERR:\n{proc.stderr.strip()}")
            
            # NVChecker with --logger=json outputs JSON log lines to STDOUT
            # It also writes results to newver.json specified in its config
            
            # Prioritize reading from newver.json as it's the primary output mechanism for versions
            if newver_json_path.is_file():
                try:
                    new_versions_content = json.loads(newver_json_path.read_text())
                    # new_versions_content format: { "pkgbase1": "version1", "pkgbase2": {"version": "version2", "other_info": ...}, ... }
                    for name, data in new_versions_content.items():
                        version = data if isinstance(data, str) else data.get("version")
                        if version:
                             results_by_pkgbase.setdefault(name, {}).update({"nvchecker_new_version": version, "nvchecker_event": "updated"}) # Assume updated if version present
                             self.logger.info(f"NVCR (newver.json): {name} -> {version}")
                except json.JSONDecodeError as e:
                    self.logger.error(f"Failed to parse newver.json from NVChecker: {e}")
                except Exception as e: # Catch any other error during processing
                    self.logger.error(f"Error processing newver.json: {e}")


            # Fallback or augment with STDOUT json log parsing if needed (more detailed events)
            for line in proc.stdout.strip().split('\n'):
                if not line.strip(): continue
                try:
                    entry = json.loads(line)
                    if entry.get("logger_name") != "nvchecker.core": continue # Focus on core events
                    
                    pkg_name_nv = entry.get("name") # This is the nvchecker entry name (usually pkgbase)
                    if not pkg_name_nv: continue

                    current_result = results_by_pkgbase.setdefault(pkg_name_nv, {})
                    current_result["nvchecker_raw_log"] = entry # Store full log line
                    
                    event = entry.get("event")
                    if event: current_result["nvchecker_event"] = event
                    
                    # If newver.json didn't provide a version, or if this log has more detail
                    if "nvchecker_new_version" not in current_result and entry.get("version"):
                        current_result["nvchecker_new_version"] = entry.get("version")
                    
                    # Log detailed events
                    msg_prefix = f"NVCR (stdout): {pkg_name_nv}"
                    if event == "updated": self.logger.info(f"{msg_prefix} UPDATED {entry.get('old_version','N/A')} -> {entry.get('version','N/A')}")
                    elif event == "up-to-date": self.logger.info(f"{msg_prefix} UP-TO-DATE at {entry.get('version','N/A')}")
                    elif event == "no-result": self.logger.warning(f"{msg_prefix} NO-RESULT. {entry.get('msg','')}")
                    elif entry.get("level") == "error" or entry.get("exc_info"):
                        self.logger.warning(f"{msg_prefix} ERROR - {entry.get('exc_info', entry.get('msg',''))}")

                except json.JSONDecodeError:
                    self.logger.warning(f"Failed to parse NVChecker JSON log line: {line[:100]}...")
        
        except FileNotFoundError: # nvchecker command itself not found
            self.logger.critical("nvchecker command not found. Ensure it is installed and in PATH for the builder user.")
        except Exception as e:
            self.logger.error(f"Global NVChecker execution failed: {e}", exc_info=self.config.debug_mode)

        self.logger.info(f"NVChecker global run finished. Found potential updates for {len(results_by_pkgbase)} pkgbase(s).")
        end_group()
        return results_by_pkgbase

    def run_nvchecker_for_package_aur_clone(self, nvchecker_toml_in_aur_clone_path: Path) -> Optional[str]:
        """Runs nvchecker for a single .toml file (typically in an AUR package's cloned dir)."""
        # This is similar to buildscript2's check_version_update
        if not nvchecker_toml_in_aur_clone_path.is_file():
            self.logger.debug(f"No .nvchecker.toml at {nvchecker_toml_in_aur_clone_path}, skipping single package nvcheck.")
            return None

        start_group(f"NVCheck for single package: {nvchecker_toml_in_aur_clone_path.parent.name}")
        new_version = None
        try:
            # nvchecker usually logs "updated to X" to stderr by default without --logger=json
            cmd = ['nvchecker', '-c', str(nvchecker_toml_in_aur_clone_path)]
            
            # Keyfile for single run - use the global one if it exists
            # The keyfile path needs to be absolute or relative to CWD
            # NVCheckerRunner has self.config.nvchecker_keyfile_path which is absolute
            if self.config.nvchecker_keyfile_path.is_file():
                 cmd.extend(['-k', str(self.config.nvchecker_keyfile_path)])


            # Run as builder, from the directory containing the .toml (AUR clone root)
            proc = self.runner.run(
                cmd, 
                cwd=nvchecker_toml_in_aur_clone_path.parent, 
                check=False, 
                run_as_user=BUILDER_USER, 
                user_home_dir=BUILDER_HOME
            )

            # Parse stderr for "updated to X"
            # Example: "[I DATE TIME core:LINE] pkgname: updated to 1.2.3"
            update_pattern = re.compile(r":\s*updated to\s+([^\s,]+)", re.IGNORECASE)
            pkg_name_from_dir = nvchecker_toml_in_aur_clone_path.parent.name # Heuristic for package name

            for line in proc.stderr.splitlines():
                line = line.strip()
                if not line: continue
                match = update_pattern.search(line)
                # Check if the line mentions the package name for more specific match (optional)
                if match and (pkg_name_from_dir in line or "updated to" in line): # Broader match if pkg_name_from_dir is not in log
                    new_version_candidate = match.group(1)
                    new_version = new_version_candidate
                    self.logger.info(f"NVChecker (single pkg stderr) found new version: {new_version} for {pkg_name_from_dir}")
                    break
                elif f"{pkg_name_from_dir}: current" in line:
                    self.logger.info(f"NVChecker (single pkg stderr) reports {pkg_name_from_dir} is current.")
                    break # No update
            
            if not new_version:
                 self.logger.info(f"NVChecker (single pkg) found no new version for {pkg_name_from_dir} via stderr.")

        except Exception as e:
            self.logger.error(f"NVChecker for single package {nvchecker_toml_in_aur_clone_path.parent.name} failed: {e}", exc_info=self.config.debug_mode)
        
        end_group()
        return new_version


# --- Version Comparison Logic ---
class VersionComparator:
    def __init__(self, logger: logging.Logger):
        self.logger = logger

    def _normalize_version_str(self, ver_str: str) -> str:
        return ver_str.replace('_', '.') # Common normalization

    def compare_pkg_versions(self, base_ver1_str: Optional[str], rel1_str: Optional[str],
                             base_ver2_str: Optional[str], rel2_str: Optional[str]) -> str:
        """Compares two package versions (base, release). Returns 'upgrade', 'downgrade', 'same', 'unknown'."""
        comp_logger = self.logger 
        #logging.getLogger(f"{self.logger.name}.version_compare") # Sub-logger if desired

        if base_ver1_str is None and base_ver2_str is None: return "same"
        if base_ver1_str is None: return "upgrade" # Local doesn't exist, remote does
        if base_ver2_str is None: return "downgrade" # Remote doesn't exist, local does (local is "newer")

        norm_base1 = self._normalize_version_str(base_ver1_str)
        norm_base2 = self._normalize_version_str(base_ver2_str)
        
        try:
            lv1 = LooseVersion(norm_base1)
            lv2 = LooseVersion(norm_base2)
        except Exception as e:
            comp_logger.error(f"Error instantiating LooseVersion for '{norm_base1}' or '{norm_base2}': {e}")
            return "unknown"

        if lv1 < lv2: return "upgrade"
        if lv1 > lv2: return "downgrade"

        # Base versions are equivalent, compare release numbers
        try:
            num_rel1 = int(rel1_str) if rel1_str and rel1_str.strip().isdigit() else 0
            num_rel2 = int(rel2_str) if rel2_str and rel2_str.strip().isdigit() else 0
        except ValueError:
            comp_logger.error(f"Invalid non-integer release: '{rel1_str}' or '{rel2_str}' with base '{base_ver1_str}'.")
            return "unknown"
        
        if num_rel1 < num_rel2: return "upgrade"
        if num_rel1 > num_rel2: return "downgrade"
        
        return "same"

    def get_full_version_string(self, v_str: Optional[str], r_str: Optional[str]) -> Optional[str]:
        if not v_str: return None
        r_val = str(r_str) if r_str is not None and str(r_str).strip() and str(r_str) != "0" else ""
        return f"{v_str}-{r_val}" if r_val else v_str

# --- Main Application Logic ---
class ArchPackageManager:
    def __init__(self, config: Config):
        self.config = config
        self.logger = logging.getLogger("ArchPackageManager") # Main app logger
        self._configure_logger()

        # Services
        self.runner = CommandRunner(self.logger) # For root/workflow user commands
        self.builder_runner = CommandRunner(self.logger, default_user=BUILDER_USER, default_home=BUILDER_HOME)

        self.pkgbuild_parser = PKGBUILDParser(self.builder_runner, self.logger) # Sourcing PKGBUILDs as builder
        self.aur_fetcher = AURInfoFetcher(self.logger)
        self.nvchecker = NVCheckerRunner(self.builder_runner, self.logger, self.config) # Global and single package
        self.version_comparator = VersionComparator(self.logger)
        
        # Build results accumulator
        self.build_operation_results: List[BuildOpResult] = []


    def _configure_logger(self):
        log_level = logging.DEBUG if self.config.debug_mode else logging.INFO
        self.logger.setLevel(log_level)
        # GHA messages go to stdout, so configure logger to use stderr for its own messages
        # if not already handled by GHA logging functions
        if not self.logger.hasHandlers(): # Avoid duplicate handlers if run multiple times
            handler = logging.StreamHandler(sys.stderr) # Python logs to stderr
            formatter = logging.Formatter('%(levelname)s: [%(name)s] %(message)s')
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)
        self.logger.propagate = False # Don't let root logger also print if it's configured
        log_notice("Logger Config", f"Logger '{self.logger.name}' configured. Level: {logging.getLevelName(self.logger.getEffectiveLevel())}")


    def _initial_environment_setup(self) -> bool:
        start_group("Initial Environment Setup")
        self.logger.info(f"Workspace: {GITHUB_WORKSPACE}")
        self.logger.info(f"PKGBUILD files root: {self.config.pkgbuild_files_root_in_workspace}")
        
        dirs_to_create_as_builder = [
            self.config.nvchecker_run_dir,
            self.config.package_build_base_dir,
        ]
        for d_path in dirs_to_create_as_builder:
            try:
                # Use root to mkdir -p, then chown to builder
                self.runner.run(["mkdir", "-p", str(d_path)]) 
                self.runner.run(["chown", f"{BUILDER_USER}:{BUILDER_USER}", str(d_path)])
                self.logger.info(f"Ensured directory exists and is builder-owned: {d_path}")
            except Exception as e:
                log_error("EnvSetupFail", f"Failed to create/chown dir {d_path}: {e}")
                end_group(); return False

        try: # Artifacts dir is in workspace, GHA runner (root) creates, builder needs to write into subdirs
            self.config.artifacts_dir_base.mkdir(parents=True, exist_ok=True)
            self.runner.run(["chown", f"{BUILDER_USER}:{BUILDER_USER}", str(self.config.artifacts_dir_base)], check=False) # Best effort chown
            self.logger.info(f"Ensured artifacts base directory: {self.config.artifacts_dir_base}")
        except Exception as e:
            log_error("EnvSetupFail", f"Failed to create artifacts base dir {self.config.artifacts_dir_base}: {e}")
            end_group(); return False
        
        # Check critical paths
        if not self.config.pkgbuild_files_root_in_workspace.is_dir():
            log_error("Config Error", f"PKGBUILD_FILES_ROOT '{self.config.pkgbuild_files_root_in_workspace}' is not a valid directory.")
            end_group(); return False

        log_notice("EnvSetup", "Initial environment setup complete.")
        end_group()
        return True

    def _analyze_package_statuses(self,
                                 local_data: Dict[str, PKGBUILDInfo],
                                 aur_data: Dict[str, Dict[str, str]],
                                 nvchecker_data: Dict[str, Dict[str, Any]]
                                 ) -> List[PackageOverallStatus]:
        """
        Merges all data sources and determines update status for each package.
        This replaces `process_and_compare_data` from `aur_package_updater_cli.py`.
        """
        start_group("Analyzing Package Statuses")
        all_pkgbases = set(local_data.keys()) | set(aur_data.keys()) | set(nvchecker_data.keys())
        processed_statuses: List[PackageOverallStatus] = []

        self.logger.info(f"Found {len(all_pkgbases)} unique pkgbases across all sources.")

        for pkgbase in sorted(list(all_pkgbases)):
            local_info = local_data.get(pkgbase)
            aur_info = aur_data.get(pkgbase)
            nv_info = nvchecker_data.get(pkgbase)

            if not local_info or not local_info.pkgfile_abs_path:
                self.logger.debug(f"Skipping {pkgbase}: No local PKGBUILD info or path.")
                continue # Must have a local PKGBUILD to be managed by this script

            status = PackageOverallStatus(
                pkgbase=pkgbase,
                pkgname_display=local_info.pkgname or pkgbase, # Best effort display name
                local_pkgbuild_info=local_info
            )
            
            # Try to determine pkgbuild_dir_rel_to_workspace
            try:
                status.pkgbuild_dir_rel_to_workspace = local_info.pkgfile_abs_path.parent.relative_to(GITHUB_WORKSPACE)
            except ValueError:
                status.comparison_errors.append(f"PKGBUILD path {local_info.pkgfile_abs_path.parent} not relative to workspace {GITHUB_WORKSPACE}.")
                status.is_update_candidate = False


            if aur_info:
                status.aur_pkgver = aur_info.get("aur_pkgver")
                status.aur_pkgrel = aur_info.get("aur_pkgrel")
                status.aur_actual_pkgname = aur_info.get("aur_actual_pkgname")
                status.aur_version_str = aur_info.get("aur_version_str")

            if nv_info:
                status.nvchecker_new_version = nv_info.get("nvchecker_new_version")
                status.nvchecker_event = nv_info.get("nvchecker_event")
                status.nvchecker_raw_log = nv_info.get("nvchecker_raw_log")

            # --- Version Comparison and Update Logic ---
            # 1. Sanity checks (errors that prevent update candidacy)
            if status.aur_pkgver and status.nvchecker_new_version:
                # Compare base versions only for AUR vs NVChecker upstream
                # AUR version should generally not be ahead of upstream.
                # None for rel implies comparing base versions only.
                comp_aur_nv = self.version_comparator.compare_pkg_versions(status.aur_pkgver, None, status.nvchecker_new_version, None)
                status.comparison_log["aur_vs_nv_base"] = f"AUR Base ({status.aur_pkgver}) vs NV Base ({status.nvchecker_new_version}) -> {comp_aur_nv}"
                if comp_aur_nv == "downgrade": # AUR is "newer" than NVChecker
                    status.comparison_errors.append(f"AUR base version {status.aur_pkgver} is newer than NVChecker upstream {status.nvchecker_new_version}.")
                    status.is_update_candidate = False
            
            if local_info.pkgver and status.nvchecker_new_version:
                # Local PKGBUILD version should not be ahead of upstream.
                comp_local_nv = self.version_comparator.compare_pkg_versions(local_info.pkgver, None, status.nvchecker_new_version, None)
                status.comparison_log["local_vs_nv_base"] = f"Local Base ({local_info.pkgver}) vs NV Base ({status.nvchecker_new_version}) -> {comp_local_nv}"
                if comp_local_nv == "downgrade": # Local is "newer" than NVChecker
                    status.comparison_errors.append(f"Local PKGBUILD base version {local_info.pkgver} is newer than NVChecker upstream {status.nvchecker_new_version}.")
                    status.is_update_candidate = False

            if not status.is_update_candidate:
                processed_statuses.append(status)
                self.logger.info(f"{pkgbase}: Not an update candidate due to errors: {status.comparison_errors}")
                continue

            # 2. Determine if an update is needed
            # Priority: NVChecker (upstream) > AUR
            update_via_nvchecker = False
            if status.nvchecker_new_version:
                # Compare local full version to NVChecker base version (NVChecker doesn't provide pkgrel)
                comp_local_to_nv = self.version_comparator.compare_pkg_versions(
                    local_info.pkgver, local_info.pkgrel,
                    status.nvchecker_new_version, None # Compare against NVChecker's base version
                )
                status.comparison_log["local_full_vs_nv_base"] = f"Local Full ({self.version_comparator.get_full_version_string(local_info.pkgver, local_info.pkgrel)}) vs NV Base ({status.nvchecker_new_version}) -> {comp_local_to_nv}"

                # Also check if AUR is behind NVChecker. If AUR is same or ahead of NVChecker, NVChecker doesn't offer an "update" over AUR.
                # This check was part of old process_and_compare_data, ensure logic is sound.
                # If local is older than NV, AND AUR is older than NV, then NV is a valid update path.
                aur_is_older_than_nv = True # Assume true if no AUR version
                if status.aur_pkgver:
                    comp_aur_to_nv = self.version_comparator.compare_pkg_versions(
                        status.aur_pkgver, status.aur_pkgrel, # AUR full version
                        status.nvchecker_new_version, None    # NVChecker base version
                    )
                    status.comparison_log["aur_full_vs_nv_base"] = f"AUR Full ({status.aur_version_str}) vs NV Base ({status.nvchecker_new_version}) -> {comp_aur_to_nv}"
                    if comp_aur_to_nv != "upgrade": # AUR is same or newer than NV
                        aur_is_older_than_nv = False
                
                if comp_local_to_nv == "upgrade" and aur_is_older_than_nv :
                    status.needs_update = True
                    status.update_source_type = "nvchecker (upstream)"
                    status.version_for_update = status.nvchecker_new_version # This is just base version from nvchecker
                    update_via_nvchecker = True
                    self.logger.info(f"{pkgbase}: Update found via NVChecker to {status.version_for_update}")

            if not update_via_nvchecker and status.aur_pkgver:
                # Compare local full version to AUR full version
                comp_local_to_aur = self.version_comparator.compare_pkg_versions(
                    local_info.pkgver, local_info.pkgrel,
                    status.aur_pkgver, status.aur_pkgrel
                )
                status.comparison_log["local_full_vs_aur_full"] = f"Local Full ({self.version_comparator.get_full_version_string(local_info.pkgver, local_info.pkgrel)}) vs AUR Full ({status.aur_version_str}) -> {comp_local_to_aur}"
                if comp_local_to_aur == "upgrade":
                    status.needs_update = True
                    status.update_source_type = "aur"
                    status.version_for_update = status.aur_version_str
                    self.logger.info(f"{pkgbase}: Update found via AUR to {status.version_for_update}")
            
            # 3. Check if local is ahead
            if not status.needs_update and not status.comparison_errors and local_info.pkgver:
                is_ahead_of_aur = True # Assume ahead if no AUR version to compare against
                if status.aur_pkgver:
                    is_ahead_of_aur = self.version_comparator.compare_pkg_versions(
                        local_info.pkgver, local_info.pkgrel, status.aur_pkgver, status.aur_pkgrel
                    ) == "downgrade" # "downgrade" means local > remote

                is_ahead_of_nv = True # Assume ahead if no NVChecker version
                if status.nvchecker_new_version:
                     is_ahead_of_nv = self.version_comparator.compare_pkg_versions(
                        local_info.pkgver, local_info.pkgrel, status.nvchecker_new_version, None
                    ) == "downgrade"
                
                # Local is ahead if it's ahead of ALL available upstream sources
                # This logic matches original aur_package_updater_cli.py
                expected_comparisons = (1 if status.aur_pkgver else 0) + (1 if status.nvchecker_new_version else 0)
                actual_ahead_count = (1 if is_ahead_of_aur and status.aur_pkgver else 0) + \
                                     (1 if is_ahead_of_nv and status.nvchecker_new_version else 0)
                
                if expected_comparisons > 0 and actual_ahead_count == expected_comparisons:
                    status.local_is_ahead = True
                elif expected_comparisons == 0: # No remote versions, local is inherently "ahead" or standalone
                    status.local_is_ahead = True
                
                if status.local_is_ahead:
                    self.logger.info(f"{pkgbase}: Local version {self.version_comparator.get_full_version_string(local_info.pkgver, local_info.pkgrel)} is ahead.")

            if not status.needs_update and not status.local_is_ahead and not status.comparison_errors:
                self.logger.info(f"{pkgbase}: Up-to-date or no action needed.")

            processed_statuses.append(status)
        
        end_group()
        return processed_statuses

    def _prepare_inputs_for_build_script(self, packages_for_build: List[PackageOverallStatus]) -> bool:
        """Generates the JSON file used as input by the build operations."""
        start_group("Preparing Build Script Inputs")
        build_inputs_data: Dict[str, Dict[str, Any]] = {}

        if not packages_for_build:
            self.logger.info("No packages to build, build input JSON will be empty.")
        
        for pkg_status in packages_for_build:
            if pkg_status.local_pkgbuild_info:
                build_inputs_data[pkg_status.pkgbase] = {
                    "depends": pkg_status.local_pkgbuild_info.depends,
                    "makedepends": pkg_status.local_pkgbuild_info.makedepends,
                    "checkdepends": pkg_status.local_pkgbuild_info.checkdepends,
                    "sources": pkg_status.local_pkgbuild_info.sources, # From PKGBUILD
                }
        
        try:
            self.config.package_build_inputs_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.config.package_build_inputs_path, "w") as f:
                json.dump(build_inputs_data, f, indent=2)
            # Ensure builder can read it
            self.runner.run(["chown", f"{BUILDER_USER}:{BUILDER_USER}", str(self.config.package_build_inputs_path)], run_as_user="root", check=False)
            self.logger.info(f"Build script input JSON created at {self.config.package_build_inputs_path}")
            log_debug(f"Build inputs JSON content: {json.dumps(build_inputs_data, indent=2, sort_keys=True)[:500]}")
            end_group(); return True
        except Exception as e:
            log_error("BuildInputFail", f"Failed to write build inputs JSON: {e}")
            end_group(); return False


    def _determine_build_mode(self, pkg_status: PackageOverallStatus) -> str:
        """Determines build mode ('build', 'test', 'nobuild') based on directory structure."""
        # e.g., if PKGBUILD is in 'maintain/build/pkgname/PKGBUILD', mode is 'build'
        # e.g., if PKGBUILD is in 'maintain/test/pkgname/PKGBUILD', mode is 'test'
        # default 'nobuild'
        if pkg_status.pkgbuild_dir_rel_to_workspace:
            # Path is like "maintain/build/actual_package_dir" or "nobuild/actual_package_dir"
            # The *parent* of actual_package_dir determines the mode.
            # So, if pkgbuild_dir_rel_to_workspace is "maintain/build/mypkg", its parent is "maintain/build".
            # The name of this parent dir is "build".
            mode_determining_dir_name = pkg_status.pkgbuild_dir_rel_to_workspace.parent.name
            if mode_determining_dir_name in ["build", "test"]:
                self.logger.info(f"Build mode for {pkg_status.pkgbase} determined as '{mode_determining_dir_name}' from path.")
                return mode_determining_dir_name
        
        self.logger.info(f"Defaulting build mode to 'nobuild' for {pkg_status.pkgbase}.")
        return "nobuild" # Default

# (Within ArchPackageManager class)

    # --- Package Build Orchestration and Helpers ---

    def _get_current_pkgbuild_version_from_file(self, pkgbuild_path: Path) -> Tuple[Optional[str], Optional[str]]:
        """Reads pkgver and pkgrel directly from a PKGBUILD file."""
        if not pkgbuild_path.is_file():
            self.logger.warning(f"PKGBUILD not found at {pkgbuild_path} for version retrieval.")
            return None, None
        try:
            content = pkgbuild_path.read_text()
            pkgver_match = re.search(r"^\s*pkgver=([^\s#]+)", content, re.MULTILINE)
            pkgrel_match = re.search(r"^\s*pkgrel=([^\s#]+)", content, re.MULTILINE)
            pkgver = pkgver_match.group(1) if pkgver_match else None
            pkgrel = pkgrel_match.group(1) if pkgrel_match else "1" # Default to 1 if not found
            return pkgver, pkgrel
        except Exception as e:
            self.logger.error(f"Error reading version from PKGBUILD {pkgbuild_path}: {e}")
            return None, None

    def _setup_package_build_environment(self, pkg_status: PackageOverallStatus, op_result: BuildOpResult) -> bool:
        pkgbase = pkg_status.pkgbase
        unique_suffix = os.urandom(4).hex()
        # Store paths in op_result for later use (e.g., cleanup)
        op_result.package_specific_build_dir_abs = self.config.package_build_base_dir / f"build-{pkgbase}-{unique_suffix}"
        op_result.aur_clone_dir_abs = op_result.package_specific_build_dir_abs / pkgbase
        
        pkg_artifact_output_dir = self.config.artifacts_dir_base / pkgbase
        op_result.log_artifact_subdir = pkg_artifact_output_dir.relative_to(self.config.artifacts_dir_base)

        try:
            self.builder_runner.run(["mkdir", "-p", str(op_result.aur_clone_dir_abs)])
            self.logger.info(f"Created package build directory: {op_result.aur_clone_dir_abs}")
            self.builder_runner.run(["mkdir", "-p", str(pkg_artifact_output_dir)]) # For artifacts

            # Clone AUR repository
            aur_repo_url = f"ssh://aur@aur.archlinux.org/{pkgbase}.git"
            self.builder_runner.run(["git", "clone", aur_repo_url, str(op_result.aur_clone_dir_abs)])
            self.logger.info(f"Cloned AUR repo for {pkgbase} into {op_result.aur_clone_dir_abs}")

            # Overlay files from workspace
            source_pkg_dir_in_workspace = pkg_status.local_pkgbuild_info.pkgfile_abs_path.parent
            self.logger.info(f"Overlaying files from {source_pkg_dir_in_workspace} to {op_result.aur_clone_dir_abs} using rsync")
            self.builder_runner.run([
                "rsync", "-ah", "--delete", "--no-owner", "--no-group", # -a implies recursive, links, perms, times. -h human readable.
                str(source_pkg_dir_in_workspace) + "/", # Trailing slash for content copy
                str(op_result.aur_clone_dir_abs) + "/"
            ])
            op_result.setup_env_ok = True
            return True
        except Exception as e:
            op_result.error_message = f"Failed to setup build environment for {pkgbase}: {e}"
            self.logger.error(op_result.error_message, exc_info=self.config.debug_mode)
            return False

    def _install_package_dependencies(self, pkg_status: PackageOverallStatus, op_result: BuildOpResult, build_inputs_data: Dict[str, Any]) -> bool:
        pkgbase = pkg_status.pkgbase
        self.logger.info(f"Processing dependencies for {pkgbase}...")
        pkg_deps_info = build_inputs_data.get(pkgbase, {}) # From the JSON generated earlier
        
        all_deps_to_install = list(set(
            pkg_deps_info.get("depends", []) +
            pkg_deps_info.get("makedepends", []) +
            pkg_deps_info.get("checkdepends", [])
        ))
        
        cleaned_deps = [re.split(r'[<>=!]', dep)[0].strip() for dep in all_deps_to_install if dep.strip()]
        cleaned_deps = sorted(list(set(filter(None, cleaned_deps))))

        if not cleaned_deps:
            self.logger.info(f"No explicit dependencies listed in build inputs for {pkgbase} to install via paru.")
            op_result.dependencies_installed_ok = True # No deps to install is a success for this step
            return True

        self.logger.info(f"Attempting to install dependencies for {pkgbase} via paru: {cleaned_deps}")
        paru_cmd = ["paru", "-S", "--noconfirm", "--needed", "--norebuild", "--sudoloop"] + cleaned_deps
        try:
            # cwd should be somewhere neutral or builder's home, not the AUR clone yet as it might not have a PKGBUILD paru likes
            dep_install_res = self.builder_runner.run(paru_cmd, cwd=BUILDER_HOME, check=True) # check=True
            self.logger.info(f"Dependencies for {pkgbase} installed successfully.")
            op_result.dependencies_installed_ok = True
            return True
        except subprocess.CalledProcessError as e:
            op_result.error_message = f"Dependency installation failed for {pkgbase}. Paru RC: {e.returncode}. Stderr: {e.stderr[:200]}"
            self.logger.error(op_result.error_message)
            return False

    def _manage_pkgbuild_versioning_in_clone(self, pkg_status: PackageOverallStatus, op_result: BuildOpResult) -> bool:
        pkgbase = pkg_status.pkgbase
        pkgsbuild_file_in_clone = op_result.aur_clone_dir_abs / "PKGBUILD"
        
        new_pkgver_target = None
        new_pkgrel_target = "1" # Always reset pkgrel to 1 for a version bump

        # Determine the target pkgver based on analysis results
        if pkg_status.update_source_type == "nvchecker (upstream)" and pkg_status.version_for_update:
            new_pkgver_target = pkg_status.version_for_update # This is base version from global nvchecker
            op_result.target_version_for_build = f"{new_pkgver_target}-{new_pkgrel_target}"
        elif pkg_status.update_source_type == "aur" and pkg_status.aur_pkgver:
            # If update is from AUR, it means local PKGBUILD is older.
            # We want to update PKGBUILD to match AUR's version_for_update (which includes pkgrel)
            new_pkgver_target = pkg_status.aur_pkgver
            new_pkgrel_target = pkg_status.aur_pkgrel or "1" # Use AUR's pkgrel
            op_result.target_version_for_build = f"{new_pkgver_target}-{new_pkgrel_target}"
        
        # Further check: if there's a .nvchecker.toml in the package's own directory (now in clone)
        # This allows fine-grained upstream checks that might be more current.
        local_nvchecker_toml_in_clone = op_result.aur_clone_dir_abs / ".nvchecker.toml"
        if local_nvchecker_toml_in_clone.is_file():
            upstream_ver_from_pkg_nv = self.nvchecker.run_nvchecker_for_package_aur_clone(local_nvchecker_toml_in_clone)
            if upstream_ver_from_pkg_nv:
                self.logger.info(f"Package-specific .nvchecker.toml for {pkgbase} suggests upstream version: {upstream_ver_from_pkg_nv}")
                if new_pkgver_target:
                    # If this new version is "greater than" the one from global/AUR analysis
                    comp_res = self.version_comparator.compare_pkg_versions(new_pkgver_target, None, upstream_ver_from_pkg_nv, None)
                    if comp_res == "upgrade": # upstream_ver_from_pkg_nv is newer
                        self.logger.info(f"Overriding target version for {pkgbase} to {upstream_ver_from_pkg_nv} based on package's .nvchecker.toml.")
                        new_pkgver_target = upstream_ver_from_pkg_nv
                        new_pkgrel_target = "1" # Reset pkgrel if version changes due to this
                        op_result.target_version_for_build = f"{new_pkgver_target}-{new_pkgrel_target}"
                else: # No target version yet, use this one
                    new_pkgver_target = upstream_ver_from_pkg_nv
                    new_pkgrel_target = "1"
                    op_result.target_version_for_build = f"{new_pkgver_target}-{new_pkgrel_target}"

        if not new_pkgver_target:
            # This case implies pkg_status.needs_update was true, but no version_for_update, which shouldn't happen.
            # Or, it's a 'nobuild' mode where we just check if PKGBUILD needs SRCINFO update.
            # For build/test modes, we expect a target version.
            current_pkgver, current_pkgrel = self._get_current_pkgbuild_version_from_file(pkgsbuild_file_in_clone)
            op_result.target_version_for_build = self.version_comparator.get_full_version_string(current_pkgver, current_pkgrel)
            self.logger.info(f"No new version target for {pkgbase} from update analysis. PKGBUILD versioning unchanged unless by later steps (e.g. updpkgsums). Current: {op_result.target_version_for_build}")
            op_result.pkgbuild_versioned_ok = True # No change needed is OK.
            return True

        self.logger.info(f"Targeting version {new_pkgver_target}-{new_pkgrel_target} for {pkgbase} in PKGBUILD.")
        try:
            content = pkgsbuild_file_in_clone.read_text()
            original_content = content
            
            # Update pkgver
            content = re.sub(
                r"(^\s*pkgver=)([^\s#]+)", rf"\g<1>{new_pkgver_target}",
                content, count=1, flags=re.MULTILINE
            )
            # Update pkgrel
            content = re.sub(
                r"(^\s*pkgrel=)([^\s#]+)", rf"\g<1>{new_pkgrel_target}",
                content, count=1, flags=re.MULTILINE
            )
            
            if content != original_content:
                pkgsbuild_file_in_clone.write_text(content)
                self.logger.info(f"PKGBUILD for {pkgbase} updated: pkgver -> {new_pkgver_target}, pkgrel -> {new_pkgrel_target}.")
                op_result.changes_made_to_aur_clone_files = True
            else:
                self.logger.info(f"PKGBUILD for {pkgbase} already reflects target version {new_pkgver_target}-{new_pkgrel_target} or regex miss.")
            
            op_result.pkgbuild_versioned_ok = True
            return True
        except Exception as e:
            op_result.error_message = f"Failed to update PKGBUILD version for {pkgbase}: {e}"
            self.logger.error(op_result.error_message, exc_info=self.config.debug_mode)
            return False

    def _execute_makepkg_and_install(self, pkg_status: PackageOverallStatus, op_result: BuildOpResult, build_mode: str) -> bool:
        if build_mode not in ["build", "test"]:
            self.logger.info(f"Skipping makepkg build for {pkg_status.pkgbase} (mode: {build_mode}).")
            # Check if SRCINFO needs update even in nobuild if PKGBUILD was touched
            pkgsbuild_file_in_clone = op_result.aur_clone_dir_abs / "PKGBUILD"
            srcinfo_file_in_clone = op_result.aur_clone_dir_abs / ".SRCINFO"
            if op_result.changes_made_to_aur_clone_files or \
               (pkgsbuild_file_in_clone.exists() and srcinfo_file_in_clone.exists() and \
                pkgsbuild_file_in_clone.stat().st_mtime > srcinfo_file_in_clone.stat().st_mtime) or \
               not srcinfo_file_in_clone.exists() and pkgsbuild_file_in_clone.exists():
                try:
                    self.logger.info(f"Regenerating .SRCINFO for {pkg_status.pkgbase} in mode '{build_mode}'.")
                    srcinfo_res = self.builder_runner.run(["makepkg", "--printsrcinfo"], cwd=op_result.aur_clone_dir_abs, check=True)
                    srcinfo_file_in_clone.write_text(srcinfo_res.stdout)
                    op_result.changes_made_to_aur_clone_files = True # SRCINFO is a change
                except Exception as e_srcinfo:
                    op_result.error_message = f"Failed to regenerate .SRCINFO for {pkg_status.pkgbase} in '{build_mode}': {e_srcinfo}"
                    self.logger.error(op_result.error_message)
                    return False # Critical for AUR consistency
            op_result.makepkg_ran_ok = True # True because it was skipped as per mode
            op_result.local_install_ok = True # Skipped
            return True

        pkgbase = pkg_status.pkgbase
        self.logger.info(f"Executing makepkg build steps for {pkgbase} (mode: {build_mode}). CWD: {op_result.aur_clone_dir_abs}")
        try:
            # updpkgsums (modifies PKGBUILD)
            self.builder_runner.run(["updpkgsums"], cwd=op_result.aur_clone_dir_abs, check=True)
            op_result.changes_made_to_aur_clone_files = True 

            # makepkg --printsrcinfo > .SRCINFO (regenerate .SRCINFO)
            srcinfo_res = self.builder_runner.run(["makepkg", "--printsrcinfo"], cwd=op_result.aur_clone_dir_abs, check=True)
            (op_result.aur_clone_dir_abs / ".SRCINFO").write_text(srcinfo_res.stdout)
            op_result.changes_made_to_aur_clone_files = True
            
            # makepkg -Lcs --noconfirm --needed (actual build)
            makepkg_cmd = ["makepkg", "-Lcs", "--noconfirm", "--needed", "--noprogressbar", "--log"]
            self.builder_runner.run(makepkg_cmd, cwd=op_result.aur_clone_dir_abs, check=True)
            op_result.makepkg_ran_ok = True
            
            # Collect built package files (*.pkg.tar.zst)
            # Prefer specific pkgbase name, then any pkg.tar.zst
            built_archives = sorted(list(op_result.aur_clone_dir_abs.glob(f"{pkgbase}*.pkg.tar.zst")))
            if not built_archives:
                built_archives = sorted(list(op_result.aur_clone_dir_abs.glob("*.pkg.tar.zst")))
            
            if not built_archives:
                raise Exception("No package files (*.pkg.tar.zst) found after makepkg.")
            
            op_result.built_package_archive_files = [p.resolve() for p in built_archives] # Store absolute paths
            self.logger.info(f"Successfully built: {', '.join(p.name for p in op_result.built_package_archive_files)}")

            # Read final version from PKGBUILD in clone, as updpkgsums might alter it (rarely pkgver, but pkgrel)
            final_pkgver, final_pkgrel = self._get_current_pkgbuild_version_from_file(op_result.aur_clone_dir_abs / "PKGBUILD")
            op_result.final_pkgbuild_version_in_clone = self.version_comparator.get_full_version_string(final_pkgver, final_pkgrel)


            # Install built package locally for testing (as builder, using sudo for pacman)
            self.builder_runner.run(
                ["sudo", "pacman", "-U", "--noconfirm"] + [str(p.name) for p in op_result.built_package_archive_files],
                cwd=op_result.aur_clone_dir_abs, check=True
            )
            self.logger.info("Installed built package(s) locally.")
            op_result.local_install_ok = True
            return True
        except Exception as e:
            op_result.error_message = f"makepkg build process failed for {pkgbase}: {e}"
            self.logger.error(op_result.error_message, exc_info=self.config.debug_mode)
            return False

    def _handle_github_release(self, pkg_status: PackageOverallStatus, op_result: BuildOpResult, build_mode: str) -> bool:
        if build_mode != "build":
            self.logger.info(f"Skipping GitHub release for {pkg_status.pkgbase} (mode: {build_mode}).")
            op_result.github_release_ok = True # Skipped is OK for this step
            return True

        pkgbase = pkg_status.pkgbase
        version_for_tag = op_result.final_pkgbuild_version_in_clone or op_result.target_version_for_build
        if not version_for_tag:
            op_result.error_message = f"Cannot create GitHub release for {pkgbase}: version unknown."
            self.logger.error(op_result.error_message)
            return False
        if not op_result.built_package_archive_files:
            op_result.error_message = f"Cannot create GitHub release for {pkgbase}: no built package files found."
            self.logger.error(op_result.error_message)
            return False

        tag_name = f"{pkgbase}-{version_for_tag}"
        release_title = f"{pkgbase} {version_for_tag}"
        self.logger.info(f"Handling GitHub release {tag_name} for {pkgbase} in repo {self.config.github_repo}.")
        
        try:
            # gh release upload will create if not exists, and clobber assets if it does.
            # Simpler than create then upload separately.
            # However, this doesn't allow setting release notes easily if created implicitly.
            # Let's use `gh release create` or `edit` then `upload`.

            # Check if release exists
            gh_release_view_cmd = ["gh", "release", "view", tag_name, "--repo", self.config.github_repo]
            view_res = self.builder_runner.run(gh_release_view_cmd, cwd=op_result.aur_clone_dir_abs, check=False)

            release_notes = f"Automated CI release for {pkgbase} {version_for_tag}.\n\nTo install, run:\n`sudo pacman -U {op_result.built_package_archive_files[0].name}` (adjust filename if multiple files)"

            if view_res.returncode != 0 : # Release does not exist, create it
                self.logger.info(f"Release {tag_name} does not exist. Creating...")
                gh_release_create_cmd = ["gh", "release", "create", tag_name,
                                       "--repo", self.config.github_repo,
                                       "--title", release_title,
                                       "--notes", release_notes]
                self.builder_runner.run(gh_release_create_cmd, cwd=op_result.aur_clone_dir_abs, check=True)
            else: # Release exists, maybe edit notes
                self.logger.info(f"Release {tag_name} already exists. Will update assets.")
                # Optionally edit notes:
                # gh_release_edit_cmd = ["gh", "release", "edit", tag_name, "--repo", self.config.github_repo, "--notes", release_notes]
                # self.builder_runner.run(gh_release_edit_cmd, cwd=op_result.aur_clone_dir_abs, check=True)


            for pkg_archive_path_abs in op_result.built_package_archive_files:
                upload_cmd = ["gh", "release", "upload", tag_name, str(pkg_archive_path_abs),
                              "--repo", self.config.github_repo, "--clobber"]
                self.builder_runner.run(upload_cmd, cwd=op_result.aur_clone_dir_abs, check=True)
            self.logger.info(f"Uploaded assets to GitHub release {tag_name}.")
            op_result.github_release_ok = True
            return True
        except Exception as e:
            op_result.error_message = f"GitHub release handling failed for {pkgbase} (tag {tag_name}): {e}"
            self.logger.error(op_result.error_message, exc_info=self.config.debug_mode)
            return False

    def _commit_and_push_to_aur_repo(self, pkg_status: PackageOverallStatus, op_result: BuildOpResult, build_inputs_data: Dict[str, Any]) -> bool:
        pkgbase = pkg_status.pkgbase
        
        # Files to add: PKGBUILD, .SRCINFO are standard. .nvchecker.toml if exists.
        # Local source files from PKGBUILD's source=() array.
        files_to_stage_for_aur_commit = ["PKGBUILD", ".SRCINFO"] # Relative to aur_clone_dir_abs
        if (op_result.aur_clone_dir_abs / ".nvchecker.toml").is_file():
            files_to_stage_for_aur_commit.append(".nvchecker.toml")

        pkg_sources_info = build_inputs_data.get(pkgbase, {}).get("sources", [])
        for src_entry in pkg_sources_info:
            # source entry can be "filename" or "filename::url" or just "url"
            src_filename_part = src_entry.split('::')[0] # If "file::url", this is "file". If "url", this is "url". If "file", this is "file".
            is_url_like = "://" in src_entry or src_entry.startswith("git+") # Check original entry
            
            if not is_url_like: # It's a local file name
                # Check if this local file actually exists in the AUR clone (copied from workspace)
                if (op_result.aur_clone_dir_abs / src_filename_part).is_file():
                    if src_filename_part not in files_to_stage_for_aur_commit:
                        files_to_stage_for_aur_commit.append(src_filename_part)
                else:
                    self.logger.warning(f"Local source '{src_filename_part}' for {pkgbase} listed in PKGBUILD/inputs but not found in AUR clone dir. Not staging for git.")
        
        self.logger.info(f"Files to stage for AUR commit for {pkgbase}: {files_to_stage_for_aur_commit}")
        
        # Check if any of the staged files actually changed or are new
        # Run 'git add' first, then 'git status --porcelain'
        try:
            self.builder_runner.run(["git", "add"] + files_to_stage_for_aur_commit, cwd=op_result.aur_clone_dir_abs, check=True)
        except subprocess.CalledProcessError as e_add:
            # This can happen if a file listed to add doesn't exist (e.g. .nvchecker.toml was expected but not present)
            # Only fatal if it's PKGBUILD or .SRCINFO (which should always be there by now)
            if any(f in str(e_add.stderr) for f in ["PKGBUILD", ".SRCINFO"]):
                op_result.error_message = f"Failed to 'git add' critical files for {pkgbase}: {e_add.stderr}"
                self.logger.error(op_result.error_message)
                return False
            self.logger.warning(f"'git add' reported minor issues for {pkgbase} (e.g. non-critical file not found), continuing. Stderr: {e_add.stderr}")


        status_res = self.builder_runner.run(["git", "status", "--porcelain"], cwd=op_result.aur_clone_dir_abs, check=True)
        if not status_res.stdout.strip() and not op_result.changes_made_to_aur_clone_files: # Check our flag too
            self.logger.info(f"No git changes to commit to AUR for {pkgbase} and no explicit file changes tracked.")
            op_result.git_commit_to_aur_ok = True # No changes is OK for this step
            op_result.git_push_to_aur_ok = True   # No commit means no push
            return True
        
        self.logger.info(f"Git changes detected for {pkgbase}. Committing and pushing to AUR...")
        commit_version_suffix = f" (v{op_result.final_pkgbuild_version_in_clone})" if op_result.final_pkgbuild_version_in_clone else \
                               (f" (v{op_result.target_version_for_build})" if op_result.target_version_for_build else "")
        aur_commit_msg = f"CI: Auto update {pkgbase}{commit_version_suffix}"
        
        try:
            # Git commit to local AUR clone (as builder)
            self.builder_runner.run(["git", "commit", "-m", aur_commit_msg], cwd=op_result.aur_clone_dir_abs, check=True)
            op_result.git_commit_to_aur_ok = True
            
            # Git push to AUR remote (as builder, using SSH key setup by GHA)
            # Default branch on AUR is 'master'. If it ever changes to 'main', this needs update.
            self.builder_runner.run(["git", "push", "origin", "master"], cwd=op_result.aur_clone_dir_abs, check=True)
            self.logger.info(f"Changes successfully committed and pushed to AUR for {pkgbase}.")
            op_result.git_push_to_aur_ok = True
            return True
        except Exception as e:
            op_result.error_message = f"Git commit/push to AUR failed for {pkgbase}: {e}"
            self.logger.error(op_result.error_message, exc_info=self.config.debug_mode)
            return False

    def _sync_changes_to_source_repo(self, pkg_status: PackageOverallStatus, op_result: BuildOpResult) -> bool:
        if not op_result.git_commit_to_aur_ok : # Only sync if AUR push was successful (or if no changes were needed for AUR)
            # If AUR push was skipped due to no changes, but we *did* modify PKGBUILD/etc. (e.g. updpkgsums in nobuild mode),
            # we might still want to sync. The flag op_result.changes_made_to_aur_clone_files tracks this.
             if not op_result.changes_made_to_aur_clone_files:
                self.logger.info(f"Skipping sync to source repo for {pkg_status.pkgbase}: No changes were made or pushed to AUR.")
                op_result.source_repo_sync_ok = True # Skipped is OK
                return True
             self.logger.info(f"Proceeding with sync to source for {pkg_status.pkgbase} as local changes were made, even if AUR push was skipped.")


        pkgbase = pkg_status.pkgbase
        self.logger.info(f"Syncing committed/changed files for {pkgbase} back to source GitHub repo {self.config.github_repo}...")
        
        # Files that were part of the AUR commit logic (even if not pushed due to no diff)
        # are candidates for syncing back. These are files like PKGBUILD, .SRCINFO, .nvchecker.toml, local patches.
        # We need the list of files that *could* have been committed.
        # Reconstruct this list similar to _commit_and_push_to_aur_repo
        files_to_consider_for_sync = ["PKGBUILD", ".SRCINFO"] 
        if (op_result.aur_clone_dir_abs / ".nvchecker.toml").is_file():
            files_to_consider_for_sync.append(".nvchecker.toml")
        
        # Get local sources from build_inputs_data (populated from original PKGBUILD parse)
        # This is a bit indirect; ideally, parse sources from the PKGBUILD *in the clone* if it could change.
        # For now, assume local source file names don't change during the build.
        global_build_inputs_data = {} # Need to re-read or pass this properly
        try:
            with open(self.config.package_build_inputs_path, "r") as f:
                global_build_inputs_data = json.load(f)
        except Exception as e_read_json:
            self.logger.warning(f"Could not read {self.config.package_build_inputs_path} for source file list: {e_read_json}")

        pkg_sources_from_input = global_build_inputs_data.get(pkgbase, {}).get("sources", [])
        for src_entry in pkg_sources_from_input:
            src_filename_part = src_entry.split('::')[0]
            is_url_like = "://" in src_entry or src_entry.startswith("git+")
            if not is_url_like and (op_result.aur_clone_dir_abs / src_filename_part).is_file():
                if src_filename_part not in files_to_consider_for_sync:
                    files_to_consider_for_sync.append(src_filename_part)

        all_synced_successfully = True
        for file_name_in_clone in files_to_consider_for_sync:
            file_path_in_aur_clone_abs = op_result.aur_clone_dir_abs / file_name_in_clone
            if not file_path_in_aur_clone_abs.is_file():
                self.logger.debug(f"File {file_name_in_clone} not found in AUR clone for {pkgbase}. Skipping sync for this file.")
                continue

            # pkg_status.pkgbuild_dir_rel_to_workspace is like "maintain/build/pkgname"
            path_in_source_repo_relative_to_workspace = pkg_status.pkgbuild_dir_rel_to_workspace / file_name_in_clone
            
            self.logger.info(f"Attempting to sync '{file_name_in_clone}' to source repo path: '{path_in_source_repo_relative_to_workspace}'")
            try:
                content_bytes = file_path_in_aur_clone_abs.read_bytes()
                content_b64 = base64.b64encode(content_bytes).decode("utf-8")

                gh_api_get_url = f"repos/{self.config.github_repo}/contents/{str(path_in_source_repo_relative_to_workspace)}"
                sha_res = self.builder_runner.run(
                    ["gh", "api", gh_api_get_url, "--jq", ".sha"], 
                    check=False # Don't fail if file is new
                )
                current_sha = sha_res.stdout.strip() if sha_res.returncode == 0 and sha_res.stdout.strip() != "null" else None

                commit_version_suffix = f" (v{op_result.final_pkgbuild_version_in_clone})" if op_result.final_pkgbuild_version_in_clone else \
                                       (f" (v{op_result.target_version_for_build})" if op_result.target_version_for_build else "")
                source_repo_commit_message = f"Sync: Update {file_name_in_clone} for {pkgbase}{commit_version_suffix}"
                
                update_fields = ["-f", f"message={source_repo_commit_message}", "-f", f"content={content_b64}"]
                if current_sha:
                    update_fields.extend(["-f", f"sha={current_sha}"])
                
                gh_api_put_url = f"repos/{self.config.github_repo}/contents/{str(path_in_source_repo_relative_to_workspace)}"
                self.builder_runner.run(
                    ["gh", "api", "--method", "PUT", gh_api_put_url] + update_fields,
                    check=True
                )
                self.logger.info(f"Successfully synced '{file_name_in_clone}' to source repo path '{path_in_source_repo_relative_to_workspace}'.")
                op_result.files_synced_to_source_repo.append(str(path_in_source_repo_relative_to_workspace))
            except Exception as e_sync:
                error_detail = f"Failed to sync '{file_name_in_clone}' for {pkgbase} to source repo: {e_sync}"
                op_result.error_message = (op_result.error_message + "; " if op_result.error_message else "") + error_detail
                self.logger.error(error_detail, exc_info=self.config.debug_mode)
                all_synced_successfully = False # Mark that at least one file failed
        
        op_result.source_repo_sync_ok = all_synced_successfully
        return all_synced_successfully

    def _collect_package_artifacts(self, pkg_status: PackageOverallStatus, op_result: BuildOpResult) -> bool:
        pkgbase = pkg_status.pkgbase
        pkg_artifact_output_dir_abs = self.config.artifacts_dir_base / pkgbase # Already created
        
        self.logger.info(f"Collecting artifacts for {pkgbase} from {op_result.aur_clone_dir_abs} to {pkg_artifact_output_dir_abs}...")
        try:
            # Copy logs produced by makepkg (*.log in aur_clone_dir_abs)
            for log_file_path_abs in op_result.aur_clone_dir_abs.glob("*.log"):
                shutil.copy2(log_file_path_abs, pkg_artifact_output_dir_abs / log_file_path_abs.name)
            
            # Copy PKGBUILD, .SRCINFO, .nvchecker.toml from the final state in clone
            for important_file_name in ["PKGBUILD", ".SRCINFO", ".nvchecker.toml"]:
                src_file_abs = op_result.aur_clone_dir_abs / important_file_name
                if src_file_abs.is_file():
                    shutil.copy2(src_file_abs, pkg_artifact_output_dir_abs / important_file_name)

            # Copy built package archives
            for built_pkg_archive_abs_path in op_result.built_package_archive_files: # These are absolute paths
                if built_pkg_archive_abs_path.is_file():
                     shutil.copy2(built_pkg_archive_abs_path, pkg_artifact_output_dir_abs / built_pkg_archive_abs_path.name)
            
            self.logger.info(f"Artifact collection for {pkgbase} finished.")
            return True
        except Exception as e_artifact:
            error_detail = f"Error during artifact collection for {pkgbase}: {e_artifact}"
            op_result.error_message = (op_result.error_message + "; " if op_result.error_message else "") + error_detail
            self.logger.warning(error_detail, exc_info=self.config.debug_mode)
            return False

    def _cleanup_package_build_environment(self, op_result: BuildOpResult) -> bool:
        if op_result.package_specific_build_dir_abs and op_result.package_specific_build_dir_abs.exists():
            self.logger.info(f"Cleaning up build directory: {op_result.package_specific_build_dir_abs}")
            try:
                # This needs to run as root if builder created files root cannot easily remove,
                # or if builder doesn't have permission to remove the base dir itself.
                # Since builder_runner creates these, and builder owns them, builder should be able to remove.
                # However, to be safe, use root runner.
                self.runner.run(["rm", "-rf", str(op_result.package_specific_build_dir_abs)], check=True)
                return True
            except Exception as e:
                error_detail = f"Failed to cleanup package build directory {op_result.package_specific_build_dir_abs}: {e}"
                op_result.error_message = (op_result.error_message + "; " if op_result.error_message else "") + error_detail
                self.logger.warning(error_detail)
                return False
        self.logger.debug(f"Package-specific build directory {op_result.package_specific_build_dir_abs} not found or not set, skipping cleanup.")
        return True


    def _perform_package_build_operations(self, pkg_status: PackageOverallStatus,
                                        build_mode: str,
                                        build_inputs_data: Dict[str, Any]) -> BuildOpResult:
        """
        Manages the full build lifecycle for a single package.
        """
        pkgbase = pkg_status.pkgbase
        start_group(f"Processing Package: {pkgbase} (Mode: {build_mode})")
        
        op_result = BuildOpResult(package_name=pkgbase) # success defaults to False

        try:
            # 1. Setup build environment (dirs, AUR clone, overlay workspace files)
            if not self._setup_package_build_environment(pkg_status, op_result):
                raise Exception(op_result.error_message or "Failed in environment setup step.")

            # 2. Install dependencies
            if not self._install_package_dependencies(pkg_status, op_result, build_inputs_data):
                raise Exception(op_result.error_message or "Failed in dependency installation step.")

            # 3. Manage PKGBUILD versioning in the clone
            if not self._manage_pkgbuild_versioning_in_clone(pkg_status, op_result):
                raise Exception(op_result.error_message or "Failed in PKGBUILD version management step.")

            # 4. Execute makepkg and local install (if build/test mode)
            if not self._execute_makepkg_and_install(pkg_status, op_result, build_mode):
                raise Exception(op_result.error_message or "Failed in makepkg/local install step.")

            # 5. Handle GitHub Release (if build mode)
            if not self._handle_github_release(pkg_status, op_result, build_mode):
                # This is often non-fatal for the overall AUR update, so log warning
                self.logger.warning(f"GitHub Release step had issues for {pkgbase}: {op_result.error_message or 'Unknown GH Release issue'}")
                # Don't raise, let AUR update proceed if possible. op_result.success won't be True unless all main steps pass.

            # 6. Commit and Push to AUR
            if not self._commit_and_push_to_aur_repo(pkg_status, op_result, build_inputs_data):
                # If this fails, we probably shouldn't sync to source repo
                raise Exception(op_result.error_message or "Failed to commit/push to AUR.")

            # 7. Sync changes back to the source GitHub repository
            if not self._sync_changes_to_source_repo(pkg_status, op_result):
                 # Also often non-fatal for AUR update itself, but indicates an issue with repo state
                self.logger.warning(f"Syncing changes to source GitHub repo had issues for {pkgbase}: {op_result.error_message or 'Unknown source sync issue'}")

            # If all critical steps passed up to this point
            op_result.success = op_result.setup_env_ok and \
                                op_result.dependencies_installed_ok and \
                                op_result.pkgbuild_versioned_ok and \
                                op_result.makepkg_ran_ok and \
                                op_result.local_install_ok and \
                                op_result.git_commit_to_aur_ok and \
                                op_result.git_push_to_aur_ok
            # Optional successes (release, source_sync) don't make op_result.success False if they fail,
            # but their individual flags will be False.

            if op_result.success:
                self.logger.info(f"Successfully processed and updated package {pkgbase}.")
            else:
                # This path taken if a step returned False but didn't raise, or if an optional step failed
                # that we want to make the overall build for this package unsuccessful.
                # For now, the logic above relies on raising exceptions for critical failures.
                # If an optional step (like GH release) fails, op_result.success might still be true
                # if its flag isn't checked here. This is fine.
                self.logger.warning(f"Package {pkgbase} processing completed, but op_result.success is False. Error: {op_result.error_message}")


        except Exception as e_main_build_op:
            # This catches exceptions raised explicitly by helper methods
            self.logger.error(f"Main build operation for {pkgbase} failed critically: {e_main_build_op}", exc_info=self.config.debug_mode)
            if not op_result.error_message: # Ensure some message is there
                op_result.error_message = str(e_main_build_op)
            op_result.success = False # Explicitly ensure failure
        
        finally:
            # 8. Collect artifacts (always attempt)
            self._collect_package_artifacts(pkg_status, op_result)
            
            # 9. Cleanup build environment (always attempt)
            self._cleanup_package_build_environment(op_result)

        end_group() # End of "Processing Package: {pkgbase}"
        return op_result    


    def _write_summary_to_file(self, overall_statuses: List[PackageOverallStatus]):
        if not self.config.github_step_summary_file:
            self.logger.info("GITHUB_STEP_SUMMARY_FILE_PATH not set, skipping summary file generation.")
            return

        start_group("Generating Workflow Summary")
        try:
            with open(self.config.github_step_summary_file, "w", encoding="utf-8") as f: # Overwrite initially
                f.write("## Arch Package Update Summary\n\n")
                f.write("| Package (`pkgbase`) | Version (Local) | Status | Details | AUR Link | Build Logs |\n")
                f.write("|---|---|---|---|---|---|\n")

                if not self.build_operation_results and not any(s.needs_update or s.local_is_ahead or s.comparison_errors for s in overall_statuses):
                     f.write("| *No updates or significant status changes found* | - | - | - | - | - |\n")

                # Merge overall_statuses with build_operation_results
                # Create a map of build results by package name for easy lookup
                build_results_map = {res.package_name: res for res in self.build_operation_results}

                for status in overall_statuses:
                    pkgbase = status.pkgbase
                    local_ver_str = self.version_comparator.get_full_version_string(
                        status.local_pkgbuild_info.pkgver if status.local_pkgbuild_info else None,
                        status.local_pkgbuild_info.pkgrel if status.local_pkgbuild_info else None
                    ) or "N/A"
                    
                    aur_link = f"[{pkgbase}](https://aur.archlinux.org/packages/{pkgbase})"
                    
                    status_text = "Up-to-date"
                    details_text = ""
                    logs_link = "N/A"

                    build_res = build_results_map.get(pkgbase)
                    if build_res: # This package was attempted to be built
                        if build_res.success:
                            status_text = f"✅ Built: v{build_res.new_version_built or 'Unknown'}"
                            if build_res.git_changes_committed_aur: details_text += "AUR updated. "
                            if build_res.github_release_created_updated: details_text += "GH Release. "
                            if build_res.source_repo_files_synced: details_text += "Source repo synced."
                        else:
                            status_text = f"❌ Build Failed: v{build_res.new_version_built or local_ver_str}"
                            details_text = f"<small>{build_res.error_message.replace('|','-').replace(chr(10),'<br>') if build_res.error_message else 'Unknown error'}</small>"
                        
                        if build_res.log_artifact_subdir and self.config.github_run_id:
                            logs_link = f"Artifacts: `build-artifacts-{self.config.github_run_id}/{build_res.log_artifact_subdir.name}/`"
                        elif build_res.log_artifact_subdir: # Fallback if run_id is missing
                             logs_link = f"Artifacts: `{build_res.log_artifact_subdir.name}/`"


                    elif status.needs_update:
                        status_text = f"⚠️ Update Pending: to v{status.version_for_update}"
                        details_text = f"Source: {status.update_source_type}."
                    elif status.local_is_ahead:
                        status_text = "ℹ️ Local Ahead"
                        details_text = f"Local ({local_ver_str}) is newer than available sources."
                    elif status.comparison_errors:
                        status_text = f"❗ Error"
                        details_text = "; ".join(status.comparison_errors)
                    
                    f.write(f"| **{pkgbase}** | `{local_ver_str}` | {status_text} | {details_text} | {aur_link} | {logs_link} |\n")
            
            self.logger.info(f"Workflow summary written to {self.config.github_step_summary_file}")
        except Exception as e:
            log_error("SummaryWriteFail", f"Failed to write workflow summary: {e}")
        end_group()


    def run_workflow(self):
        log_notice("WorkflowStart", "Arch Package Management Workflow Starting...")
        if not self._initial_environment_setup():
            log_error("Fatal", "Environment setup failed. Exiting.")
            if self.config.github_step_summary_file:
                 with open(self.config.github_step_summary_file, "a") as f: f.write("| **SETUP** | N/A | ❌ Failure: Env setup | - | - | Check Logs |\n")
            return 1 # Exit code

        # 1. Fetch all local PKGBUILD data
        local_pkg_data = self.pkgbuild_parser.fetch_all_local_pkgbuild_data(self.config.pkgbuild_files_root_in_workspace)
        if not local_pkg_data:
            log_warning("NoLocalData", "No local PKGBUILD data found. Workflow might not do much.")

        # 2. Fetch AUR data for the maintainer
        aur_pkg_data = self.aur_fetcher.fetch_data_for_maintainer(self.config.aur_maintainer_name)

        # 3. Prepare oldver data for global NVChecker (use AUR version if available, else local)
        nvchecker_oldver_input = {}
        all_relevant_pkgbases = set(local_pkg_data.keys()) | set(aur_pkg_data.keys())
        for pkgbase in all_relevant_pkgbases:
            aur_info = aur_pkg_data.get(pkgbase)
            local_info = local_pkg_data.get(pkgbase)
            version_to_use = None
            if aur_info and aur_info.get("aur_pkgver"):
                version_to_use = aur_info["aur_pkgver"] # Use AUR base version for nvchecker's oldver
            elif local_info and local_info.pkgver:
                version_to_use = local_info.pkgver
            
            if version_to_use:
                nvchecker_oldver_input[pkgbase] = {"version": version_to_use}
        
        # 4. Run global NVChecker
        nvchecker_global_results = self.nvchecker.run_global_nvchecker(self.config.pkgbuild_files_root_in_workspace, nvchecker_oldver_input)

        # 5. Analyze all statuses
        all_package_statuses = self._analyze_package_statuses(local_pkg_data, aur_pkg_data, nvchecker_global_results)
        
        # Save the status report as an artifact
        try:
            self.config.package_status_report_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.config.package_status_report_path, "w") as f:
                json.dump([dataclasses.asdict(s) for s in all_package_statuses], f, indent=2, default=str) # default=str for Path objects
            self.runner.run(["cp", str(self.config.package_status_report_path), str(self.config.artifacts_dir_base / self.config.package_status_report_path.name)], check=False)
            log_notice("StatusArtifact", f"Package status report saved to artifacts: {self.config.package_status_report_path.name}")
        except Exception as e:
            log_warning("StatusArtifactFail", f"Failed to save package status report artifact: {e}")


        # 6. Filter for packages that need an update and are candidates
        packages_needing_build_action = [
            s for s in all_package_statuses if s.needs_update and s.is_update_candidate and s.local_pkgbuild_info and s.pkgbuild_dir_rel_to_workspace
        ]

        if not packages_needing_build_action:
            log_notice("NoUpdatesToBuild", "No packages require build actions after analysis.")
            self._write_summary_to_file(all_package_statuses)
            return 0

        log_notice("UpdatesFound", f"Found {len(packages_needing_build_action)} package(s) requiring build actions.")

        # 7. Prepare inputs for the build process (e.g., dependency lists)
        if not self._prepare_inputs_for_build_script(packages_needing_build_action):
            log_error("Fatal", "Failed to prepare inputs for build script. Exiting.")
            self._write_summary_to_file(all_package_statuses) # Write summary with what we have
            return 1
        
        with open(self.config.package_build_inputs_path, "r") as f: # Read the prepared data
            build_inputs_json_content = json.load(f)


        # 8. Loop through packages and perform build operations
        overall_success = True
        for pkg_status_to_build in packages_needing_build_action:
            build_mode = self._determine_build_mode(pkg_status_to_build)
            
            # Get specific inputs for this package
            current_pkg_build_inputs = build_inputs_json_content.get(pkg_status_to_build.pkgbase, {})

            build_op_res = self._perform_package_build_operations(
                pkg_status_to_build,
                build_mode,
                current_pkg_build_inputs 
            )
            self.build_operation_results.append(build_op_res)
            if not build_op_res.success:
                overall_success = False
                log_error("PackageBuildFail", f"Build operation failed for {pkg_status_to_build.pkgbase}.")
        
        # 9. Final summary
        self._write_summary_to_file(all_package_statuses)

        if not overall_success:
            log_error("WorkflowEndFail", "One or more package build operations failed.")
            return 1
            
        log_notice("WorkflowEndSuccess", "All package management tasks completed successfully.")
        return 0


def main():
    # --- Environment Variable Based Configuration ---
    try:
        cfg = Config(
            aur_maintainer_name=os.environ["AUR_MAINTAINER_NAME"],
            github_repo=os.environ["GITHUB_REPO_OWNER_SLASH_NAME"],
            pkgbuild_files_root_in_workspace=Path(os.environ["PKGBUILD_FILES_ROOT"]).resolve(),
            git_commit_user_name=os.environ["GIT_COMMIT_USER_NAME"],
            git_commit_user_email=os.environ["GIT_COMMIT_USER_EMAIL"],
            gh_token=os.environ["GH_TOKEN_FOR_RELEASES_AND_NVCHECKER"],
        )
    except KeyError as e:
        log_error("ConfigFatal", f"Missing critical environment variable: {e}. Please set it in the GitHub Actions workflow.")
        sys.exit(2)
    except Exception as e_cfg:
        log_error("ConfigFatal", f"Error initializing configuration: {e_cfg}")
        sys.exit(2)

    # Initialize and run the main manager
    manager = ArchPackageManager(cfg)
    exit_code = manager.run_workflow()
    sys.exit(exit_code)

if __name__ == "__main__":
    # Basic logger for pre-config errors, GHA helpers take over later
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", stream=sys.stderr)
    main()
