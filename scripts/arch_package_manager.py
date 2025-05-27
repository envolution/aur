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
BUILDER_HOME = Path(os.getenv("BUILDER_HOME_OVERRIDE", f"/home/{BUILDER_USER}"))
GITHUB_WORKSPACE = Path(os.getenv("GITHUB_WORKSPACE", "/github/workspace"))
NVCHECKER_RUN_TEMP_DIR_BASE = BUILDER_HOME / "nvchecker_run"
PACKAGE_BUILD_TEMP_DIR_BASE = BUILDER_HOME / "pkg_builds"
ARTIFACTS_OUTPUT_DIR_BASE = GITHUB_WORKSPACE / "artifacts"

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
    pkgbuild_files_root_in_workspace: Path
    git_commit_user_name: str
    git_commit_user_email: str
    
    gh_token: str
    
    nvchecker_run_dir: Path = NVCHECKER_RUN_TEMP_DIR_BASE
    package_build_base_dir: Path = PACKAGE_BUILD_TEMP_DIR_BASE
    artifacts_dir_base: Path = ARTIFACTS_OUTPUT_DIR_BASE

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
    pkgname: Optional[str] = None 
    all_pkgnames: List[str] = dataclasses.field(default_factory=list) 
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
    pkgname_display: str 

    local_pkgbuild_info: Optional[PKGBUILDInfo] = None
    
    aur_version_str: Optional[str] = None 
    aur_pkgver: Optional[str] = None
    aur_pkgrel: Optional[str] = None
    aur_actual_pkgname: Optional[str] = None

    nvchecker_new_version: Optional[str] = None
    nvchecker_event: Optional[str] = None
    nvchecker_raw_log: Optional[Dict[str, Any]] = None

    comparison_errors: List[str] = dataclasses.field(default_factory=list)
    is_update_candidate: bool = True
    needs_update: bool = False
    update_source_type: Optional[str] = None
    version_for_update: Optional[str] = None 
    local_is_ahead: bool = False
    comparison_log: Dict[str, str] = dataclasses.field(default_factory=dict)

    pkgbuild_dir_rel_to_workspace: Optional[Path] = None

@dataclasses.dataclass
class BuildOpResult:
    package_name: str
    success: bool = False
    target_version_for_build: Optional[str] = None
    final_pkgbuild_version_in_clone: Optional[str] = None
    built_package_archive_files: List[Path] = dataclasses.field(default_factory=list)
    
    setup_env_ok: bool = False
    dependencies_installed_ok: bool = False
    pkgbuild_versioned_ok: bool = False
    makepkg_ran_ok: bool = False
    local_install_ok: bool = False
    
    changes_made_to_aur_clone_files: bool = False
    git_commit_to_aur_ok: bool = False
    git_push_to_aur_ok: bool = False
    github_release_ok: bool = False
    source_repo_sync_ok: bool = False
    files_synced_to_source_repo: List[str] = dataclasses.field(default_factory=list)

    error_message: Optional[str] = None
    log_artifact_subdir: Optional[Path] = None
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
            if user_to_run == BUILDER_USER: 
                current_env["PATH"] = f"/usr/local/bin:/usr/bin:/bin:{home_for_run / '.local/bin' if home_for_run else ''}"

        if env_extra:
            current_env.update(env_extra)
        
        if print_command:
            log_debug(f"Running command: {shlex.join(final_cmd)} (CWD: {cwd or '.'})")

        try:
            result = subprocess.run(
                final_cmd, check=check, text=True, capture_output=capture_output,
                cwd=cwd, env=current_env, input=input_data, timeout=1800 # 30 min timeout
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
            return subprocess.CompletedProcess(args=final_cmd, returncode=124, stdout=e.stdout or "", stderr=e.stderr or "TimeoutExpired")
        except FileNotFoundError as e:
            log_error("CMD_NOT_FOUND", f"Command not found: {final_cmd[0]}. Ensure it's installed and in PATH.")
            if check: raise
            return subprocess.CompletedProcess(args=final_cmd, returncode=127, stdout="", stderr=str(e))
        except Exception as e:
            log_error("CMD_UNEXPECTED_ERROR", f"Unexpected error running '{shlex.join(final_cmd)}': {type(e).__name__} - {e}")
            if check: raise
            return subprocess.CompletedProcess(args=final_cmd, returncode=1, stdout="", stderr=str(e))

# --- PKGBUILD Parser ---
class PKGBUILDParser:
    def __init__(self, runner: CommandRunner, logger: logging.Logger, config: Config):
        self.runner = runner
        self.logger = logger
        self.config = config

    def _source_and_extract_pkgbuild_vars(self, pkgbuild_file_path: Path) -> PKGBUILDInfo:
        """Sources a single PKGBUILD using a bash script and extracts variables."""
        info = PKGBUILDInfo(pkgfile_abs_path=pkgbuild_file_path)
        escaped_pkgbuild_path = shlex.quote(str(pkgbuild_file_path))
        
        bash_script = f"""
        _SHELL_OPTS_OLD=$(set +o)
        set -e
        PKGBUILD_DIR=$(dirname {escaped_pkgbuild_path})
        cd "$PKGBUILD_DIR"
        unset pkgbase pkgname pkgver pkgrel epoch depends makedepends checkdepends optdepends provides conflicts replaces options source md5sums sha1sums sha224sums sha256sums sha384sums sha512sums b2sums
        pkgver() {{ :; }}; prepare() {{ :; }}; build() {{ :; }}; check() {{ :; }}; package() {{ :; }};
        package_prepend() {{ :; }}; package_append() {{ :; }};
        for _func_name in $(declare -F | awk '{{print $3}}' | grep -E '^package_'); do eval "$_func_name() {{ :; }}"; done
        set +u; . {escaped_pkgbuild_path}; set -u;
        set +e
        echo "PKGBASE_START"; echo "${{pkgbase:-__VAR_NOT_SET__}}"; echo "PKGBASE_END"
        _primary_pkgname_val="__VAR_NOT_SET__"
        if [ -n "${{pkgname+x}}" ]; then
            if declare -p pkgname 2>/dev/null | grep -q '^declare -a'; then _primary_pkgname_val="${{pkgname[0]:-__VAR_NOT_SET__}}"; else _primary_pkgname_val="${{pkgname:-__VAR_NOT_SET__}}"; fi
        fi
        echo "PRIMARY_PKGNAME_START"; echo "${{_primary_pkgname_val}}"; echo "PRIMARY_PKGNAME_END"
        _print_array() {{ local var_name="$1";
            if [ -n "${{!var_name+x}}" ] && declare -p "$var_name" 2>/dev/null | grep -q '^declare -a'; then local -n arr="$var_name"; if [ "${{#arr[@]}}" -gt 0 ]; then printf '%s\\n' "${{arr[@]}}"; else echo "__EMPTY_ARRAY__"; fi
            elif [ -n "${{!var_name+x}}" ]; then echo "${{!var_name}}"; else echo "__EMPTY_ARRAY__"; fi
        }}
        echo "ALL_PKGNAMES_START"; _print_array pkgname; echo "ALL_PKGNAMES_END"
        echo "PKGVER_START";  echo "${{pkgver:-__VAR_NOT_SET__}}"; echo "PKGVER_END"
        echo "PKGREL_START";  echo "${{pkgrel:-__VAR_NOT_SET__}}"; echo "PKGREL_END"
        echo "DEPENDS_START"; _print_array depends; echo "DEPENDS_END"
        echo "MAKEDEPENDS_START"; _print_array makedepends; echo "MAKEDEPENDS_END"
        echo "CHECKDEPENDS_START"; _print_array checkdepends; echo "CHECKDEPENDS_END"
        echo "SOURCES_START"; _print_array source; echo "SOURCES_END"
        eval "$_SHELL_OPTS_OLD"
        """
        try:
            result = self.runner.run(['bash', '-c', bash_script], check=False, run_as_user=BUILDER_USER, user_home_dir=BUILDER_HOME)

            if result.stderr.strip(): self.logger.debug(f"Bash stderr for {pkgbuild_file_path} (RC {result.returncode}):\n{result.stderr.strip()}")
            if self.config.debug_mode or result.returncode != 0:
                 if result.stdout.strip(): self.logger.debug(f"Bash stdout for {pkgbuild_file_path}:\n{result.stdout.strip()}")
                 else: self.logger.debug(f"Bash stdout for {pkgbuild_file_path} was empty.")

            if result.returncode != 0 and not result.stdout.strip():
                info.error = f"Bash script sourcing failed (RC {result.returncode}) with no variable output."
                if result.stderr.strip(): info.error += f" Stderr: {result.stderr.strip()[:150]}"
                self.logger.warning(f"Critical sourcing failure for {pkgbuild_file_path}: {info.error}")
                return info

            output_lines = result.stdout.splitlines()
            
            def _parse_section_robust(start_marker: str, end_marker: str, is_array: bool) -> Union[Optional[str], List[str]]:
                raw_values = []
                in_section = False
                for line_content in output_lines:
                    if line_content == start_marker: in_section = True; continue
                    if line_content == end_marker: in_section = False; break 
                    if in_section: raw_values.append(line_content)
                
                if is_array:
                    if not raw_values or raw_values == ["__EMPTY_ARRAY__"]: return []
                    return [v for v in raw_values if v.strip()] 
                else: # Scalar
                    if not raw_values or raw_values[0] == "__VAR_NOT_SET__": return None
                    return raw_values[0].strip()

            raw_pkgbase_val = _parse_section_robust("PKGBASE_START", "PKGBASE_END", False)
            raw_primary_pkgname_val = _parse_section_robust("PRIMARY_PKGNAME_START", "PRIMARY_PKGNAME_END", False)
            info.all_pkgnames = _parse_section_robust("ALL_PKGNAMES_START", "ALL_PKGNAMES_END", True)

            if raw_pkgbase_val: info.pkgbase = raw_pkgbase_val
            elif raw_primary_pkgname_val:
                info.pkgbase = raw_primary_pkgname_val
                self.logger.debug(f"Derived pkgbase '{info.pkgbase}' from primary_pkgname for {pkgbuild_file_path}")
            else: info.pkgbase = None

            info.pkgname = raw_primary_pkgname_val if raw_primary_pkgname_val else info.pkgbase
            info.pkgver = _parse_section_robust("PKGVER_START", "PKGVER_END", False)
            info.pkgrel = _parse_section_robust("PKGREL_START", "PKGREL_END", False)
            if info.pkgrel is None and info.pkgver is not None : info.pkgrel = "1"

            info.depends = _parse_section_robust("DEPENDS_START", "DEPENDS_END", True)
            info.makedepends = _parse_section_robust("MAKEDEPENDS_START", "MAKEDEPENDS_END", True)
            info.checkdepends = _parse_section_robust("CHECKDEPENDS_START", "CHECKDEPENDS_END", True)
            info.sources = _parse_section_robust("SOURCES_START", "SOURCES_END", True)
            
            current_errors = []
            if result.returncode != 0:
                err_msg = f"Sourcing script exited with RC {result.returncode}."
                if result.stderr.strip(): err_msg += f" Stderr hint: {result.stderr.strip()[:100]}"
                current_errors.append(err_msg)
            if not info.pkgbase and not info.pkgname: current_errors.append("Neither pkgbase nor a primary pkgname could be determined.")
            if not info.pkgver: current_errors.append("pkgver could not be extracted.")
            if current_errors:
                info.error = "; ".join(current_errors)
                self.logger.debug(f"Issues after parsing {pkgbuild_file_path}: {info.error}")
        except Exception as e:
            info.error = f"Python exception during PKGBUILD processing for {pkgbuild_file_path}: {type(e).__name__} - {e}"
            self.logger.error(info.error, exc_info=self.config.debug_mode)
        return info        

    def fetch_all_local_pkgbuild_data(self, pkgbuild_root_dir: Path) -> Dict[str, PKGBUILDInfo]:
        start_group("Parsing Local PKGBUILD Files")
        self.logger.info(f"Searching for PKGBUILDs in {pkgbuild_root_dir}...")
        pkgbuild_files = list(pkgbuild_root_dir.rglob("PKGBUILD"))
        self.logger.info(f"Found {len(pkgbuild_files)} PKGBUILD file(s) to process.")

        results_by_pkgbase: Dict[str, PKGBUILDInfo] = {}
        if not pkgbuild_files:
            self.logger.warning("No PKGBUILD files found.")
            end_group(); return results_by_pkgbase

        num_workers = os.cpu_count() or 1
        self.logger.info(f"Processing PKGBUILDs using up to {num_workers} worker(s).")

        with ProcessPoolExecutor(max_workers=num_workers) as executor:
            future_to_path = { executor.submit(self._source_and_extract_pkgbuild_vars, filepath): filepath for filepath in pkgbuild_files }
            for future in as_completed(future_to_path):
                original_filepath = future_to_path[future]
                try:
                    pkg_info = future.result()
                except Exception as exc:
                    self.logger.error(f"Worker for {original_filepath} generated unhandled exception: {exc}", exc_info=self.config.debug_mode)
                    pkg_info = PKGBUILDInfo(pkgfile_abs_path=original_filepath, error=f"Worker process exception: {exc}")

                if not pkg_info.pkgbase and pkg_info.pkgver and pkg_info.pkgfile_abs_path:
                    derived_from_dir = pkg_info.pkgfile_abs_path.parent.name
                    self.logger.warning(f"PKGBUILD {pkg_info.pkgfile_abs_path.name} has no explicit pkgbase/primary_pkgname. Falling back to dir name '{derived_from_dir}' as pkgbase (pkgver: {pkg_info.pkgver}).")
                    pkg_info.pkgbase = derived_from_dir
                    if not pkg_info.pkgname: pkg_info.pkgname = derived_from_dir
                    if pkg_info.error and "Neither pkgbase nor a primary pkgname could be determined" in pkg_info.error:
                        pkg_info.error = pkg_info.error.replace("Neither pkgbase nor a primary pkgname could be determined from PKGBUILD variables.", "").strip("; ")
                        if not pkg_info.error.strip(): pkg_info.error = None 
                
                if not pkg_info.pkgbase: pkg_info.error = (pkg_info.error + "; " if pkg_info.error else "") + "Critical: pkgbase could not be determined."
                if not pkg_info.pkgver: pkg_info.error = (pkg_info.error + "; " if pkg_info.error else "") + "Critical: pkgver could not be extracted."
                
                if not pkg_info.pkgbase or not pkg_info.pkgver:
                    self.logger.error(f"Skipping {pkg_info.pkgfile_abs_path or original_filepath} due to critical missing data: {pkg_info.error or 'Unknown critical error'}")
                    continue
                
                if pkg_info.error: self.logger.warning(f"PKGBUILD {pkg_info.pkgfile_abs_path.name} (pkgbase: {pkg_info.pkgbase}) processed with issues: {pkg_info.error}")
                if pkg_info.pkgbase in results_by_pkgbase:
                    self.logger.warning(f"Duplicate pkgbase '{pkg_info.pkgbase}'. Original from: '{results_by_pkgbase[pkg_info.pkgbase].pkgfile_abs_path}'. New from: '{pkg_info.pkgfile_abs_path}'. Overwriting.")
                results_by_pkgbase[pkg_info.pkgbase] = pkg_info
                self.logger.debug(f"Stored local PKGBUILD info for: {pkg_info.pkgbase} (Name: {pkg_info.pkgname}, Version: {pkg_info.pkgver}-{pkg_info.pkgrel}) from file: {pkg_info.pkgfile_abs_path.name}")
        
        self.logger.info(f"Successfully processed and stored data for {len(results_by_pkgbase)} unique pkgbase entries.")
        end_group()
        return results_by_pkgbase

# --- AUR Info Fetcher ---
class AURInfoFetcher:
    def __init__(self, logger: logging.Logger):
        self.logger = logger

    def fetch_data_for_maintainer(self, maintainer: str) -> Dict[str, Dict[str, str]]:
        start_group(f"Fetching AUR Data for Maintainer: {maintainer}")
        aur_data_by_pkgbase: Dict[str, Dict[str, str]] = {}
        url = f"https://aur.archlinux.org/rpc/v5/search/{maintainer}?by=maintainer"
        self.logger.info(f"Querying AUR: {url}")
        try:
            response = requests.get(url, timeout=20)
            response.raise_for_status()
            data = response.json()

            if data.get("type") == "error": self.logger.error(f"AUR API error: {data.get('error')}"); end_group(); return aur_data_by_pkgbase
            if data.get("resultcount", 0) == 0: self.logger.info(f"No packages found on AUR for maintainer '{maintainer}'."); end_group(); return aur_data_by_pkgbase

            for result in data.get("results", []):
                pkgbase, aur_name, full_ver = result.get("PackageBase"), result.get("Name"), result.get("Version")
                if not pkgbase or not full_ver: continue
                ver_no_epoch = full_ver.split(':', 1)[-1]
                parts = ver_no_epoch.rsplit('-', 1)
                base_v, rel_v = parts[0], (parts[1] if len(parts) > 1 and parts[1].isdigit() else "0")
                
                if pkgbase not in aur_data_by_pkgbase:
                    aur_data_by_pkgbase[pkgbase] = {
                        "aur_pkgver": base_v, "aur_pkgrel": rel_v,
                        "aur_actual_pkgname": aur_name, 
                        "aur_version_str": f"{base_v}-{rel_v}" if rel_v != "0" else base_v
                    }
                    self.logger.debug(f"  AUR: {pkgbase} (Name: {aur_name}) -> v{base_v}-{rel_v}")
            self.logger.info(f"Fetched info for {len(aur_data_by_pkgbase)} unique PkgBase(s) from AUR.")
        except requests.Timeout: self.logger.error("AUR query timed out.")
        except requests.RequestException as e: self.logger.error(f"AUR query failed: {e}")
        except json.JSONDecodeError as e: self.logger.error(f"Failed to parse AUR JSON response: {e}")
        end_group()
        return aur_data_by_pkgbase

# --- NVChecker Runner ---
class NVCheckerRunner:
    def __init__(self, runner: CommandRunner, logger: logging.Logger, config: Config):
        self.runner = runner
        self.logger = logger
        self.config = config

    def _generate_nvchecker_keyfile(self) -> Optional[Path]:
        if not self.config.gh_token: log_debug("No GH_TOKEN, nvchecker runs without GitHub keys."); return None
        keyfile_content = f"[keys]\ngithub = '{self.config.gh_token}'\n"
        try:
            self.config.nvchecker_keyfile_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.config.nvchecker_keyfile_path, "w") as f: f.write(keyfile_content)
            self.runner.run(["chown", f"{BUILDER_USER}:{BUILDER_USER}", str(self.config.nvchecker_keyfile_path)], run_as_user="root", check=False)
            self.logger.info(f"NVChecker keyfile created at {self.config.nvchecker_keyfile_path}.")
            return self.config.nvchecker_keyfile_path
        except Exception as e: self.logger.error(f"Failed to create NVChecker keyfile: {e}"); return None

    def run_global_nvchecker(self, pkgbuild_root_dir: Path, oldver_data_for_nvchecker: Dict[str, Dict[str, str]]) -> Dict[str, Dict[str, Any]]:
        start_group("Running Global NVChecker (STDOUT JSON Log Mode)")
        results_by_pkgbase: Dict[str, Dict[str, Any]] = {}
        toml_files = list(pkgbuild_root_dir.rglob(".nvchecker.toml"))
        if not toml_files: self.logger.info("No .nvchecker.toml files found. Skipping."); end_group(); return results_by_pkgbase
        self.logger.info(f"Found {len(toml_files)} .nvchecker.toml files.")

        all_nv_tomls_path = self.config.nvchecker_run_dir / "all_project_nv.toml"
        oldver_json_path = self.config.nvchecker_run_dir / "oldver.json"
        newver_json_path = self.config.nvchecker_run_dir / "newver.json"

        if not self.config.nvchecker_run_dir.is_dir(): self.runner.run(["mkdir", "-p", str(self.config.nvchecker_run_dir)], check=True)
        self.runner.run(["chown", "-R", f"{BUILDER_USER}:{BUILDER_USER}", str(self.config.nvchecker_run_dir)], check=True, run_as_user="root")
        self.logger.info(f"Ensured {self.config.nvchecker_run_dir} exists and is owned by {BUILDER_USER}")
        keyfile_to_use = self._generate_nvchecker_keyfile()

        try:
            content_list_for_toml = ["[__config__]\n", f"oldver = '{oldver_json_path.name}'\n", f"newver = '{newver_json_path.name}'\n\n"]
            for tf_path in toml_files:
                try:
                    rel_path_from_workspace = tf_path.relative_to(GITHUB_WORKSPACE)
                    content_list_for_toml.extend([f"# Source: {rel_path_from_workspace}\n", tf_path.read_text(), "\n\n"])
                except Exception as e: self.logger.warning(f"Error reading {tf_path}: {e}. Skipping."); continue
            
            concatenated_toml_content = "".join(content_list_for_toml)
            self.runner.run(["bash", "-c", f"echo {shlex.quote(concatenated_toml_content)} > {shlex.quote(str(all_nv_tomls_path))}"], check=True)
            log_debug(f"NVChecker TOML config ({all_nv_tomls_path.name}) written to CWD ({self.config.nvchecker_run_dir}):\n{concatenated_toml_content[:1000]}...")

            oldver_json_content_str = json.dumps({"version": 2, "data": oldver_data_for_nvchecker})
            self.runner.run(["bash", "-c", f"echo {shlex.quote(oldver_json_content_str)} > {shlex.quote(str(oldver_json_path))}"], check=True)
            log_debug(f"Oldver JSON ({oldver_json_path.name}) written to CWD ({self.config.nvchecker_run_dir}):\n{oldver_json_content_str}")

            self.runner.run(["touch", str(newver_json_path)], check=True)
            self.runner.run(["truncate", "-s", "0", str(newver_json_path)], check=True)
            log_debug(f"Ensured newver.json ({newver_json_path.name}) is empty in CWD ({self.config.nvchecker_run_dir}).")

            cmd = ['nvchecker', '-c', all_nv_tomls_path.name, '--logger=json']
            if keyfile_to_use and keyfile_to_use.is_file(): cmd.extend(['-k', str(keyfile_to_use)])

            self.logger.info(f"Running NVChecker command: {shlex.join(cmd)} (CWD: {self.config.nvchecker_run_dir})")
            proc = self.runner.run(cmd, cwd=self.config.nvchecker_run_dir, check=False, run_as_user=BUILDER_USER, user_home_dir=BUILDER_HOME)
            
            log_debug(f"NVChecker raw STDOUT:\n{proc.stdout}")
            log_debug(f"NVChecker raw STDERR:\n{proc.stderr}")
            log_debug(f"NVChecker return code: {proc.returncode}")

            if proc.returncode != 0: self.logger.error(f"NVChecker exited with code {proc.returncode}.")
            if proc.stderr.strip(): self.logger.info(f"NVChecker STDERR (Info/Warnings):\n{proc.stderr.strip()}")
            
            if not proc.stdout.strip(): self.logger.warning("NVChecker produced no STDOUT.")

            for line_num, line in enumerate(proc.stdout.strip().split('\n')):
                if not line.strip(): continue; log_debug(f"NVCR STDOUT Log Line {line_num + 1}: {line}")
                try:
                    entry = json.loads(line)
                    if entry.get("logger_name") != "nvchecker.core": log_debug(f"  Skipping log line: logger_name is '{entry.get('logger_name')}'"); continue
                    pkg_name_nv = entry.get("name")
                    if not pkg_name_nv: log_debug(f"  Skipping log line: 'name' field missing."); continue

                    current_result_for_pkg = results_by_pkgbase.setdefault(pkg_name_nv, {})
                    current_result_for_pkg["nvchecker_raw_log"] = entry
                    event = entry.get("event")
                    if event: current_result_for_pkg["nvchecker_event"] = event; self.logger.debug(f"  PKG '{pkg_name_nv}': Event set to '{event}'")
                    version_from_log_entry = entry.get("version")
                    
                    if event == "updated" and version_from_log_entry is not None:
                        new_ver_str = str(version_from_log_entry)
                        current_result_for_pkg["nvchecker_new_version"] = new_ver_str
                        self.logger.info(f"  PKG '{pkg_name_nv}': Event 'updated'. New version '{new_ver_str}'. Old: {entry.get('old_version','N/A')}")
                    elif event == "up-to-date": self.logger.info(f"  PKG '{pkg_name_nv}': Event 'up-to-date' at version '{version_from_log_entry}'.")
                    elif event == "no-result": self.logger.warning(f"  PKG '{pkg_name_nv}': Event 'no-result'. Msg: {entry.get('msg','')}")
                    elif entry.get("level") == "error" or entry.get("exc_info"): self.logger.warning(f"  PKG '{pkg_name_nv}': Logged ERROR by nvchecker. Msg: {entry.get('exc_info', entry.get('msg',''))}")
                    
                    if current_result_for_pkg.get("nvchecker_new_version") and current_result_for_pkg.get("nvchecker_event") != "updated":
                        if current_result_for_pkg.get("nvchecker_event"): self.logger.debug(f"  PKG '{pkg_name_nv}': Ensuring event is 'updated' (was '{current_result_for_pkg.get('nvchecker_event')}').")
                        current_result_for_pkg["nvchecker_event"] = "updated"
                except json.JSONDecodeError: self.logger.warning(f"Failed to parse NVChecker JSON log line: {line[:100]}...")
        
        except FileNotFoundError: self.logger.critical("nvchecker command not found.")
        except subprocess.CalledProcessError as e_subproc: self.logger.error(f"Subprocess error during NVChecker setup: {e_subproc}. Cmd: '{e_subproc.cmd}'. Stderr: {e_subproc.stderr}", exc_info=self.config.debug_mode)
        except Exception as e: self.logger.error(f"Global NVChecker execution failed: {e}", exc_info=self.config.debug_mode)

        packages_with_new_version = sum(1 for pkg_data in results_by_pkgbase.values() if pkg_data.get("nvchecker_new_version"))
        self.logger.info(f"NVChecker global run finished. Found 'nvchecker_new_version' for {packages_with_new_version} pkgbase(s).")
        end_group()
        return results_by_pkgbase

    def run_nvchecker_for_package_aur_clone(self, nvchecker_toml_in_aur_clone_path: Path) -> Optional[str]:
        if not nvchecker_toml_in_aur_clone_path.is_file():
            self.logger.debug(f"No .nvchecker.toml at {nvchecker_toml_in_aur_clone_path}, skipping single nvcheck.")
            return None

        start_group(f"NVCheck for single package: {nvchecker_toml_in_aur_clone_path.parent.name}")
        new_version = None
        try:
            cmd = ['nvchecker', '-c', str(nvchecker_toml_in_aur_clone_path)]
            if self.config.nvchecker_keyfile_path.is_file(): cmd.extend(['-k', str(self.config.nvchecker_keyfile_path)])

            proc = self.runner.run(cmd, cwd=nvchecker_toml_in_aur_clone_path.parent, check=False, run_as_user=BUILDER_USER, user_home_dir=BUILDER_HOME)
            update_pattern = re.compile(r":\s*updated to\s+([^\s,]+)", re.IGNORECASE)
            pkg_name_from_dir = nvchecker_toml_in_aur_clone_path.parent.name

            for line in proc.stderr.splitlines():
                line = line.strip();
                if not line: continue
                match = update_pattern.search(line)
                if match and (pkg_name_from_dir in line or "updated to" in line):
                    new_version = match.group(1)
                    self.logger.info(f"NVChecker (single pkg stderr) found new version: {new_version} for {pkg_name_from_dir}")
                    break
                elif f"{pkg_name_from_dir}: current" in line:
                    self.logger.info(f"NVChecker (single pkg stderr) reports {pkg_name_from_dir} is current.")
                    break
            if not new_version: self.logger.info(f"NVChecker (single pkg) found no new version for {pkg_name_from_dir} via stderr.")
        except Exception as e: self.logger.error(f"NVChecker for single package {nvchecker_toml_in_aur_clone_path.parent.name} failed: {e}", exc_info=self.config.debug_mode)
        end_group()
        return new_version

# --- Version Comparison Logic ---
class VersionComparator:
    def __init__(self, logger: logging.Logger): self.logger = logger
    def _normalize_version_str(self, ver_str: str) -> str: return ver_str.replace('_', '.')
    def compare_pkg_versions(self, base_ver1_str: Optional[str], rel1_str: Optional[str], base_ver2_str: Optional[str], rel2_str: Optional[str]) -> str:
        if base_ver1_str is None and base_ver2_str is None: return "same"
        if base_ver1_str is None: return "upgrade";  # Ver1 (e.g. local) doesn't exist, Ver2 (e.g. remote) does
        if base_ver2_str is None: return "downgrade" # Ver2 doesn't exist, Ver1 does (Ver1 is "newer")
        try: lv1, lv2 = LooseVersion(self._normalize_version_str(base_ver1_str)), LooseVersion(self._normalize_version_str(base_ver2_str))
        except Exception as e: self.logger.error(f"Error LooseVersion for '{base_ver1_str}' or '{base_ver2_str}': {e}"); return "unknown"
        if lv1 < lv2: return "upgrade";
        if lv1 > lv2: return "downgrade"
        try: num_rel1, num_rel2 = int(rel1_str or 0), int(rel2_str or 0)
        except ValueError: self.logger.error(f"Invalid non-integer release: '{rel1_str}' or '{rel2_str}' with base '{base_ver1_str}'."); return "unknown"
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
        self.logger = logging.getLogger("ArchPackageManager")
        self._configure_logger()
        self.runner = CommandRunner(self.logger)
        self.builder_runner = CommandRunner(self.logger, default_user=BUILDER_USER, default_home=BUILDER_HOME)
        self.pkgbuild_parser = PKGBUILDParser(self.builder_runner, self.logger, self.config)
        self.aur_fetcher = AURInfoFetcher(self.logger)
        self.nvchecker = NVCheckerRunner(self.builder_runner, self.logger, self.config)
        self.version_comparator = VersionComparator(self.logger)
        self.build_operation_results: List[BuildOpResult] = []

    def _configure_logger(self):
        log_level = logging.DEBUG if self.config.debug_mode else logging.INFO
        self.logger.setLevel(log_level)
        if not self.logger.hasHandlers():
            handler = logging.StreamHandler(sys.stderr)
            formatter = logging.Formatter('%(levelname)s: [%(name)s] %(message)s')
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)
        self.logger.propagate = False
        log_notice("Logger Config", f"Logger '{self.logger.name}' configured. Level: {logging.getLevelName(self.logger.getEffectiveLevel())}")

    def _initial_environment_setup(self) -> bool:
        start_group("Initial Environment Setup")
        self.logger.info(f"Workspace: {GITHUB_WORKSPACE}, PKGBUILD root: {self.config.pkgbuild_files_root_in_workspace}")
        for d_path in [self.config.nvchecker_run_dir, self.config.package_build_base_dir]:
            try:
                self.runner.run(["mkdir", "-p", str(d_path)]) 
                self.runner.run(["chown", f"{BUILDER_USER}:{BUILDER_USER}", str(d_path)])
                self.logger.info(f"Ensured directory exists and is builder-owned: {d_path}")
            except Exception as e: log_error("EnvSetupFail", f"Failed to create/chown dir {d_path}: {e}"); end_group(); return False
        if not self.config.artifacts_dir_base.is_dir(): log_error("EnvSetupFail", f"Base artifacts dir {self.config.artifacts_dir_base} missing."); end_group(); return False
        self.logger.info(f"Base artifacts directory confirmed: {self.config.artifacts_dir_base}")
        if not self.config.pkgbuild_files_root_in_workspace.is_dir(): log_error("Config Error", f"PKGBUILD_FILES_ROOT '{self.config.pkgbuild_files_root_in_workspace}' invalid."); end_group(); return False
        log_notice("EnvSetup", "Initial environment setup complete."); end_group(); return True

    def _analyze_package_statuses(self, local_data: Dict[str, PKGBUILDInfo], aur_data: Dict[str, Dict[str, str]], nvchecker_data: Dict[str, Dict[str, Any]]) -> List[PackageOverallStatus]:
        start_group("Analyzing Package Statuses")
        all_pkgbases = set(local_data.keys()) | set(aur_data.keys()) | set(nvchecker_data.keys())
        processed_statuses: List[PackageOverallStatus] = []
        self.logger.info(f"Found {len(all_pkgbases)} unique pkgbases across all sources.")

        for pkgbase in sorted(list(all_pkgbases)):
            local_info, aur_info, nv_info = local_data.get(pkgbase), aur_data.get(pkgbase), nvchecker_data.get(pkgbase)
            if not local_info or not local_info.pkgfile_abs_path: self.logger.debug(f"Skipping {pkgbase}: No local PKGBUILD info."); continue

            status = PackageOverallStatus(pkgbase=pkgbase, pkgname_display=local_info.pkgname or pkgbase, local_pkgbuild_info=local_info)
            try: status.pkgbuild_dir_rel_to_workspace = local_info.pkgfile_abs_path.parent.relative_to(GITHUB_WORKSPACE)
            except ValueError: status.comparison_errors.append(f"PKGBUILD path not relative to workspace."); status.is_update_candidate = False
            if aur_info: status.aur_pkgver, status.aur_pkgrel, status.aur_actual_pkgname, status.aur_version_str = aur_info.get("aur_pkgver"), aur_info.get("aur_pkgrel"), aur_info.get("aur_actual_pkgname"), aur_info.get("aur_version_str")
            if nv_info: status.nvchecker_new_version, status.nvchecker_event, status.nvchecker_raw_log = nv_info.get("nvchecker_new_version"), nv_info.get("nvchecker_event"), nv_info.get("nvchecker_raw_log")

            if status.aur_pkgver and status.nvchecker_new_version:
                comp_aur_nv = self.version_comparator.compare_pkg_versions(status.aur_pkgver, None, status.nvchecker_new_version, None)
                status.comparison_log["aur_vs_nv_base"] = f"AUR Base ({status.aur_pkgver}) vs NV Base ({status.nvchecker_new_version}) -> {comp_aur_nv}"
                if comp_aur_nv == "downgrade": status.comparison_errors.append(f"AUR base {status.aur_pkgver} > NV upstream {status.nvchecker_new_version}."); status.is_update_candidate = False
            if local_info.pkgver and status.nvchecker_new_version:
                comp_local_nv = self.version_comparator.compare_pkg_versions(local_info.pkgver, None, status.nvchecker_new_version, None)
                status.comparison_log["local_vs_nv_base"] = f"Local Base ({local_info.pkgver}) vs NV Base ({status.nvchecker_new_version}) -> {comp_local_nv}"
                if comp_local_nv == "downgrade": status.comparison_errors.append(f"Local PKGBUILD base {local_info.pkgver} > NV upstream {status.nvchecker_new_version}."); status.is_update_candidate = False

            if not status.is_update_candidate: processed_statuses.append(status); self.logger.info(f"{pkgbase}: Not update candidate: {status.comparison_errors}"); continue

            update_via_nvchecker = False
            if status.nvchecker_new_version:
                comp_local_to_nv = self.version_comparator.compare_pkg_versions(local_info.pkgver, local_info.pkgrel, status.nvchecker_new_version, None)
                status.comparison_log["local_full_vs_nv_base"] = f"Local Full ({self.version_comparator.get_full_version_string(local_info.pkgver, local_info.pkgrel)}) vs NV Base ({status.nvchecker_new_version}) -> {comp_local_to_nv}"
                aur_is_older_than_nv = True
                if status.aur_pkgver:
                    comp_aur_to_nv = self.version_comparator.compare_pkg_versions(status.aur_pkgver, status.aur_pkgrel, status.nvchecker_new_version, None)
                    status.comparison_log["aur_full_vs_nv_base"] = f"AUR Full ({status.aur_version_str}) vs NV Base ({status.nvchecker_new_version}) -> {comp_aur_to_nv}"
                    if comp_aur_to_nv != "upgrade": aur_is_older_than_nv = False
                if comp_local_to_nv == "upgrade" and aur_is_older_than_nv :
                    status.needs_update, status.update_source_type, status.version_for_update = True, "nvchecker (upstream)", status.nvchecker_new_version
                    update_via_nvchecker = True; self.logger.info(f"{pkgbase}: Update found via NVChecker to {status.version_for_update}")

            if not update_via_nvchecker and status.aur_pkgver:
                comp_local_to_aur = self.version_comparator.compare_pkg_versions(local_info.pkgver, local_info.pkgrel, status.aur_pkgver, status.aur_pkgrel)
                status.comparison_log["local_full_vs_aur_full"] = f"Local Full ({self.version_comparator.get_full_version_string(local_info.pkgver, local_info.pkgrel)}) vs AUR Full ({status.aur_version_str}) -> {comp_local_to_aur}"
                if comp_local_to_aur == "upgrade":
                    status.needs_update, status.update_source_type, status.version_for_update = True, "aur", status.aur_version_str
                    self.logger.info(f"{pkgbase}: Update found via AUR to {status.version_for_update}")
            
            if not status.needs_update and not status.comparison_errors and local_info.pkgver:
                is_ahead_of_aur = (self.version_comparator.compare_pkg_versions(local_info.pkgver, local_info.pkgrel, status.aur_pkgver, status.aur_pkgrel) == "downgrade") if status.aur_pkgver else True
                is_ahead_of_nv = (self.version_comparator.compare_pkg_versions(local_info.pkgver, local_info.pkgrel, status.nvchecker_new_version, None) == "downgrade") if status.nvchecker_new_version else True
                expected_comparisons = (1 if status.aur_pkgver else 0) + (1 if status.nvchecker_new_version else 0)
                actual_ahead_count = (1 if is_ahead_of_aur and status.aur_pkgver else 0) + (1 if is_ahead_of_nv and status.nvchecker_new_version else 0)
                if (expected_comparisons > 0 and actual_ahead_count == expected_comparisons) or expected_comparisons == 0: status.local_is_ahead = True
                if status.local_is_ahead: self.logger.info(f"{pkgbase}: Local version {self.version_comparator.get_full_version_string(local_info.pkgver, local_info.pkgrel)} is ahead.")

            if not status.needs_update and not status.local_is_ahead and not status.comparison_errors: self.logger.info(f"{pkgbase}: Up-to-date or no action needed.")
            processed_statuses.append(status)
        end_group()
        return processed_statuses

    def _prepare_inputs_for_build_script(self, packages_for_build: List[PackageOverallStatus]) -> bool:
        start_group("Preparing Build Script Inputs")
        build_inputs_data: Dict[str, Dict[str, Any]] = {}
        if not packages_for_build: self.logger.info("No packages to build, input JSON will be empty.")
        for pkg_status in packages_for_build:
            if pkg_status.local_pkgbuild_info:
                build_inputs_data[pkg_status.pkgbase] = {"depends": pkg_status.local_pkgbuild_info.depends, "makedepends": pkg_status.local_pkgbuild_info.makedepends, "checkdepends": pkg_status.local_pkgbuild_info.checkdepends, "sources": pkg_status.local_pkgbuild_info.sources}
        try:
            self.config.package_build_inputs_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.config.package_build_inputs_path, "w") as f: json.dump(build_inputs_data, f, indent=2)
            self.runner.run(["chown", f"{BUILDER_USER}:{BUILDER_USER}", str(self.config.package_build_inputs_path)], run_as_user="root", check=False)
            self.logger.info(f"Build script input JSON created: {self.config.package_build_inputs_path}")
            log_debug(f"Build inputs JSON content: {json.dumps(build_inputs_data, indent=2, sort_keys=True)[:500]}")
            end_group(); return True
        except Exception as e: log_error("BuildInputFail", f"Failed to write build inputs JSON: {e}"); end_group(); return False

    def _determine_build_mode(self, pkg_status: PackageOverallStatus) -> str:
        if pkg_status.pkgbuild_dir_rel_to_workspace:
            mode_determining_dir_name = pkg_status.pkgbuild_dir_rel_to_workspace.parent.name
            if mode_determining_dir_name in ["build", "test"]:
                self.logger.info(f"Build mode for {pkg_status.pkgbase} is '{mode_determining_dir_name}'.")
                return mode_determining_dir_name
        self.logger.info(f"Defaulting build mode to 'nobuild' for {pkg_status.pkgbase}.")
        return "nobuild"

    def _get_current_pkgbuild_version_from_file(self, pkgbuild_path: Path) -> Tuple[Optional[str], Optional[str]]:
        if not pkgbuild_path.is_file(): self.logger.warning(f"PKGBUILD not found at {pkgbuild_path}."); return None, None
        try:
            content = pkgbuild_path.read_text()
            pkgver = (m.group(1) if (m := re.search(r"^\s*pkgver=([^\s#]+)", content, re.MULTILINE)) else None)
            pkgrel = (m.group(1) if (m := re.search(r"^\s*pkgrel=([^\s#]+)", content, re.MULTILINE)) else "1")
            return pkgver, pkgrel
        except Exception as e: self.logger.error(f"Error reading version from PKGBUILD {pkgbuild_path}: {e}"); return None, None

    def _setup_package_build_environment(self, pkg_status: PackageOverallStatus, op_result: BuildOpResult) -> bool:
        pkgbase = pkg_status.pkgbase
        op_result.package_specific_build_dir_abs = self.config.package_build_base_dir / f"build-{pkgbase}-{os.urandom(4).hex()}"
        op_result.aur_clone_dir_abs = op_result.package_specific_build_dir_abs / pkgbase
        pkg_artifact_output_dir = self.config.artifacts_dir_base / pkgbase
        op_result.log_artifact_subdir = pkg_artifact_output_dir.relative_to(self.config.artifacts_dir_base)

        try:
            self.builder_runner.run(["mkdir", "-p", str(op_result.aur_clone_dir_abs)])
            self.logger.info(f"Created package build directory: {op_result.aur_clone_dir_abs}")
            self.builder_runner.run(["mkdir", "-p", str(pkg_artifact_output_dir)])

            aur_repo_url = f"ssh://aur@aur.archlinux.org/{pkgbase}.git"
            self.logger.info(f"Cloning AUR repo: {aur_repo_url} into {op_result.aur_clone_dir_abs}")
            self.builder_runner.run(["git", "clone", aur_repo_url, str(op_result.aur_clone_dir_abs)], check=True) # check=True added
            self.builder_runner.run(["git", "status"], cwd=op_result.aur_clone_dir_abs, check=True) # Verify clone

            source_pkg_dir_in_workspace = pkg_status.local_pkgbuild_info.pkgfile_abs_path.parent
            self.logger.info(f"Overlaying files from {source_pkg_dir_in_workspace} to {op_result.aur_clone_dir_abs} using rsync")
            self.builder_runner.run([
                "rsync", "-rltvH", "--no-owner", "--no-group", 
                "--exclude=.git", "--delete-after", # MODIFIED: exclude .git, use delete-after
                str(source_pkg_dir_in_workspace) + "/", 
                str(op_result.aur_clone_dir_abs) + "/"
            ])

            if not (op_result.aur_clone_dir_abs / ".git").is_dir(): # CRITICAL check
                self.logger.error(f"CRITICAL: .git directory MISSING in {op_result.aur_clone_dir_abs} after rsync and setup.")
                op_result.error_message = f".git directory missing in {op_result.aur_clone_dir_abs} post-setup."
                return False

            op_result.setup_env_ok = True
            return True
        except Exception as e:
            op_result.error_message = f"Failed to setup build environment for {pkgbase}: {e}"
            self.logger.error(op_result.error_message, exc_info=self.config.debug_mode)
            return False

    def _install_package_dependencies(self, pkg_status: PackageOverallStatus, op_result: BuildOpResult, build_inputs_data: Dict[str, Any]) -> bool:
        pkgbase = pkg_status.pkgbase; self.logger.info(f"Processing dependencies for {pkgbase}...")
        pkg_deps_info = build_inputs_data.get(pkgbase, {})
        all_deps_to_install = list(set(pkg_deps_info.get("depends", []) + pkg_deps_info.get("makedepends", []) + pkg_deps_info.get("checkdepends", [])))
        cleaned_deps = sorted(list(set(filter(None, [re.split(r'[<>=!]', dep)[0].strip() for dep in all_deps_to_install if dep.strip()]))))

        if not cleaned_deps: self.logger.info(f"No explicit dependencies for {pkgbase}."); op_result.dependencies_installed_ok = True; return True
        self.logger.info(f"Installing dependencies for {pkgbase} via paru: {cleaned_deps}")
        try:
            self.builder_runner.run(["paru", "-S", "--noconfirm", "--needed", "--norebuild", "--sudoloop"] + cleaned_deps, cwd=BUILDER_HOME, check=True)
            self.logger.info(f"Dependencies for {pkgbase} installed."); op_result.dependencies_installed_ok = True; return True
        except subprocess.CalledProcessError as e:
            op_result.error_message = f"Dependency installation failed for {pkgbase}. Paru RC: {e.returncode}. Stderr: {e.stderr[:200]}"
            self.logger.error(op_result.error_message); return False

    def _manage_pkgbuild_versioning_in_clone(self, pkg_status: PackageOverallStatus, op_result: BuildOpResult) -> bool:
        pkgbase = pkg_status.pkgbase
        pkgsbuild_file_in_clone = op_result.aur_clone_dir_abs / "PKGBUILD"
        new_pkgver_target, new_pkgrel_target = None, "1"

        if pkg_status.update_source_type == "nvchecker (upstream)" and pkg_status.version_for_update:
            new_pkgver_target = pkg_status.version_for_update
            op_result.target_version_for_build = f"{new_pkgver_target}-{new_pkgrel_target}"
        elif pkg_status.update_source_type == "aur" and pkg_status.aur_pkgver:
            new_pkgver_target, new_pkgrel_target = pkg_status.aur_pkgver, pkg_status.aur_pkgrel or "1"
            op_result.target_version_for_build = f"{new_pkgver_target}-{new_pkgrel_target}"
        
        local_nvchecker_toml_in_clone = op_result.aur_clone_dir_abs / ".nvchecker.toml"
        if local_nvchecker_toml_in_clone.is_file():
            upstream_ver_from_pkg_nv = self.nvchecker.run_nvchecker_for_package_aur_clone(local_nvchecker_toml_in_clone)
            if upstream_ver_from_pkg_nv:
                self.logger.info(f"Package .nvchecker.toml for {pkgbase} suggests upstream: {upstream_ver_from_pkg_nv}")
                if not new_pkgver_target or self.version_comparator.compare_pkg_versions(new_pkgver_target, None, upstream_ver_from_pkg_nv, None) == "upgrade":
                    self.logger.info(f"Overriding target version for {pkgbase} to {upstream_ver_from_pkg_nv} (pkgrel 1) from package's .nvchecker.toml.")
                    new_pkgver_target, new_pkgrel_target = upstream_ver_from_pkg_nv, "1"
                    op_result.target_version_for_build = f"{new_pkgver_target}-{new_pkgrel_target}"

        if not new_pkgver_target:
            current_pkgver, current_pkgrel = self._get_current_pkgbuild_version_from_file(pkgsbuild_file_in_clone)
            op_result.target_version_for_build = self.version_comparator.get_full_version_string(current_pkgver, current_pkgrel)
            self.logger.info(f"No new version target for {pkgbase}. PKGBUILD versioning unchanged. Current: {op_result.target_version_for_build}")
            op_result.pkgbuild_versioned_ok = True; return True

        self.logger.info(f"Targeting version {new_pkgver_target}-{new_pkgrel_target} for {pkgbase} in PKGBUILD.")
        try:
            content = original_content = pkgsbuild_file_in_clone.read_text()
            content = re.sub(r"(^\s*pkgver=)([^\s#]+)", rf"\g<1>{new_pkgver_target}", content, count=1, flags=re.MULTILINE)
            content = re.sub(r"(^\s*pkgrel=)([^\s#]+)", rf"\g<1>{new_pkgrel_target}", content, count=1, flags=re.MULTILINE)
            if content != original_content: pkgsbuild_file_in_clone.write_text(content); self.logger.info(f"PKGBUILD for {pkgbase} updated."); op_result.changes_made_to_aur_clone_files = True
            else: self.logger.info(f"PKGBUILD for {pkgbase} already reflects target version or regex miss.")
            op_result.pkgbuild_versioned_ok = True; return True
        except Exception as e:
            op_result.error_message = f"Failed to update PKGBUILD version for {pkgbase}: {e}"
            self.logger.error(op_result.error_message, exc_info=self.config.debug_mode); return False

    def _execute_makepkg_and_install(self, pkg_status: PackageOverallStatus, op_result: BuildOpResult, build_mode: str) -> bool:
        if build_mode not in ["build", "test"]:
            self.logger.info(f"Skipping makepkg build for {pkg_status.pkgbase} (mode: {build_mode}).")
            pkgsbuild_file_in_clone = op_result.aur_clone_dir_abs / "PKGBUILD"
            srcinfo_file_in_clone = op_result.aur_clone_dir_abs / ".SRCINFO"
            if op_result.changes_made_to_aur_clone_files or \
               (pkgsbuild_file_in_clone.exists() and srcinfo_file_in_clone.exists() and pkgsbuild_file_in_clone.stat().st_mtime > srcinfo_file_in_clone.stat().st_mtime) or \
               (not srcinfo_file_in_clone.exists() and pkgsbuild_file_in_clone.exists()):
                try:
                    self.logger.info(f"Regenerating .SRCINFO for {pkg_status.pkgbase} in mode '{build_mode}'.")
                    srcinfo_res = self.builder_runner.run(["makepkg", "--printsrcinfo"], cwd=op_result.aur_clone_dir_abs, check=True)
                    srcinfo_file_in_clone.write_text(srcinfo_res.stdout); op_result.changes_made_to_aur_clone_files = True
                except Exception as e_srcinfo:
                    op_result.error_message = f"Failed to regenerate .SRCINFO for {pkg_status.pkgbase} in '{build_mode}': {e_srcinfo}"
                    self.logger.error(op_result.error_message); return False
            op_result.makepkg_ran_ok = op_result.local_install_ok = True; return True

        pkgbase = pkg_status.pkgbase
        self.logger.info(f"Executing makepkg build for {pkgbase} (mode: {build_mode}). CWD: {op_result.aur_clone_dir_abs}")
        try:
            self.builder_runner.run(["updpkgsums"], cwd=op_result.aur_clone_dir_abs, check=True); op_result.changes_made_to_aur_clone_files = True 
            srcinfo_res = self.builder_runner.run(["makepkg", "--printsrcinfo"], cwd=op_result.aur_clone_dir_abs, check=True)
            (op_result.aur_clone_dir_abs / ".SRCINFO").write_text(srcinfo_res.stdout); op_result.changes_made_to_aur_clone_files = True
            
            self.builder_runner.run(["makepkg", "-Lcs", "--noconfirm", "--needed", "--noprogressbar", "--log"], cwd=op_result.aur_clone_dir_abs, check=True)
            op_result.makepkg_ran_ok = True
            
            if self.config.debug_mode:
                self.logger.info(f"Directory structure of {op_result.aur_clone_dir_abs} after makepkg for {pkgbase}:")
                tree_log = self.builder_runner.run(["tree", "-L", "3", "-a"], cwd=op_result.aur_clone_dir_abs, check=False, capture_output=True)
                if tree_log.stdout: self.logger.info(f"\n{tree_log.stdout.strip()}")
                if tree_log.stderr: self.logger.warning(f"Tree command stderr (after makepkg): {tree_log.stderr.strip()}")            
            
            built_archives = sorted(list(op_result.aur_clone_dir_abs.glob(f"{pkgbase}*.pkg.tar.zst"))) or \
                             sorted(list(op_result.aur_clone_dir_abs.glob("*.pkg.tar.zst")))
            if not built_archives: raise Exception("No package files (*.pkg.tar.zst) found after makepkg.")
            op_result.built_package_archive_files = [p.resolve() for p in built_archives]
            self.logger.info(f"Successfully built: {', '.join(p.name for p in op_result.built_package_archive_files)}")

            final_pkgver, final_pkgrel = self._get_current_pkgbuild_version_from_file(op_result.aur_clone_dir_abs / "PKGBUILD")
            op_result.final_pkgbuild_version_in_clone = self.version_comparator.get_full_version_string(final_pkgver, final_pkgrel)

            self.builder_runner.run(["sudo", "pacman", "-U", "--noconfirm"] + [str(p.name) for p in op_result.built_package_archive_files], cwd=op_result.aur_clone_dir_abs, check=True)
            self.logger.info("Installed built package(s) locally."); op_result.local_install_ok = True; return True
        except Exception as e:
            op_result.error_message = f"makepkg build process failed for {pkgbase}: {e}"
            self.logger.error(op_result.error_message, exc_info=self.config.debug_mode); return False

    def _handle_github_release(self, pkg_status: PackageOverallStatus, op_result: BuildOpResult, build_mode: str) -> bool:
        if build_mode != "build": self.logger.info(f"Skipping GitHub release for {pkg_status.pkgbase} (mode: {build_mode})."); op_result.github_release_ok = True; return True
        pkgbase = pkg_status.pkgbase
        version_for_tag = op_result.final_pkgbuild_version_in_clone or op_result.target_version_for_build
        if not version_for_tag: op_result.error_message = f"Cannot create GitHub release for {pkgbase}: version unknown."; self.logger.error(op_result.error_message); return False
        if not op_result.built_package_archive_files: op_result.error_message = f"Cannot create GitHub release for {pkgbase}: no built package files."; self.logger.error(op_result.error_message); return False

        tag_name, release_title = f"{pkgbase}-{version_for_tag}", f"{pkgbase} {version_for_tag}"
        self.logger.info(f"Handling GitHub release {tag_name} for {pkgbase} in repo {self.config.github_repo}.")
        gh_env_extra = {"GITHUB_TOKEN": self.config.gh_token}
        try:
            view_res = self.builder_runner.run(["gh", "release", "view", tag_name, "--repo", self.config.github_repo], cwd=op_result.aur_clone_dir_abs, check=False, env_extra=gh_env_extra)
            release_notes = f"Automated CI release for {pkgbase} {version_for_tag}." # Keep notes simple
            if view_res.returncode != 0 : # Release does not exist
                self.logger.info(f"Release {tag_name} does not exist. Creating...")
                self.builder_runner.run(["gh", "release", "create", tag_name, "--repo", self.config.github_repo, "--title", release_title, "--notes", release_notes], cwd=op_result.aur_clone_dir_abs, check=True, env_extra=gh_env_extra)
            else: self.logger.info(f"Release {tag_name} already exists. Will update assets.")
            for pkg_archive_path_abs in op_result.built_package_archive_files:
                self.builder_runner.run(["gh", "release", "upload", tag_name, str(pkg_archive_path_abs), "--repo", self.config.github_repo, "--clobber"], cwd=op_result.aur_clone_dir_abs, check=True, env_extra=gh_env_extra)
            self.logger.info(f"Uploaded assets to GitHub release {tag_name}."); op_result.github_release_ok = True; return True
        except Exception as e:
            op_result.error_message = f"GitHub release handling failed for {pkgbase} (tag {tag_name}): {e}"
            self.logger.error(op_result.error_message, exc_info=self.config.debug_mode); return False

    def _commit_and_push_to_aur_repo(self, pkg_status: PackageOverallStatus, op_result: BuildOpResult, build_inputs_data: Dict[str, Any]) -> bool:
        pkgbase = pkg_status.pkgbase
        files_to_stage_for_aur_commit = ["PKGBUILD", ".SRCINFO"]
        if (op_result.aur_clone_dir_abs / ".nvchecker.toml").is_file(): files_to_stage_for_aur_commit.append(".nvchecker.toml")

        pkg_sources_info = build_inputs_data.get(pkgbase, {}).get("sources", [])
        for src_entry in pkg_sources_info:
            src_filename_part = src_entry.split('::')[0]
            if not ("://" in src_entry or src_entry.startswith("git+")): # Local file
                if (op_result.aur_clone_dir_abs / src_filename_part).is_file():
                    if src_filename_part not in files_to_stage_for_aur_commit: files_to_stage_for_aur_commit.append(src_filename_part)
                else: self.logger.warning(f"Local source '{src_filename_part}' for {pkgbase} not found in AUR clone. Not staging.")
        
        self.logger.info(f"Files to stage for AUR commit for {pkgbase}: {files_to_stage_for_aur_commit}")
        if self.config.debug_mode:
            self.logger.info(f"Directory structure of {op_result.aur_clone_dir_abs} before git add for {pkgbase}:")
            tree_log = self.builder_runner.run(["tree", "-L", "3", "-a"], cwd=op_result.aur_clone_dir_abs, check=False, capture_output=True)
            if tree_log.stdout: self.logger.info(f"\n{tree_log.stdout.strip()}")
            if tree_log.stderr: self.logger.warning(f"Tree command stderr (before git add): {tree_log.stderr.strip()}")        

        if not (op_result.aur_clone_dir_abs / ".git").is_dir(): # This is the critical check
            op_result.error_message = f"AUR clone {op_result.aur_clone_dir_abs} is not a git repository (missing .git dir before git add). Clone/setup likely failed."
            self.logger.error(op_result.error_message); return False

        try:
            self.builder_runner.run(["git", "add"] + files_to_stage_for_aur_commit, cwd=op_result.aur_clone_dir_abs, check=True)
        except subprocess.CalledProcessError as e_add:
            if any(f in str(e_add.stderr) for f in ["PKGBUILD", ".SRCINFO"]):
                op_result.error_message = f"Failed 'git add' of critical files for {pkgbase}: {e_add.stderr}"; self.logger.error(op_result.error_message); return False
            self.logger.warning(f"'git add' reported minor issues for {pkgbase} (e.g. non-critical file not found), continuing. Stderr: {e_add.stderr}")

        status_res = self.builder_runner.run(["git", "status", "--porcelain"], cwd=op_result.aur_clone_dir_abs, check=True)
        if not status_res.stdout.strip() and not op_result.changes_made_to_aur_clone_files:
            self.logger.info(f"No git changes to commit to AUR for {pkgbase}."); op_result.git_commit_to_aur_ok = op_result.git_push_to_aur_ok = True; return True
        
        self.logger.info(f"Git changes detected for {pkgbase}. Committing and pushing to AUR...")
        commit_version_suffix = f" (v{op_result.final_pkgbuild_version_in_clone or op_result.target_version_for_build or 'unknown'})"
        aur_commit_msg = f"CI: Auto update {pkgbase}{commit_version_suffix}"
        try:
            self.builder_runner.run(["git", "commit", "-m", aur_commit_msg], cwd=op_result.aur_clone_dir_abs, check=True); op_result.git_commit_to_aur_ok = True
            self.builder_runner.run(["git", "push", "origin", "master"], cwd=op_result.aur_clone_dir_abs, check=True) # Default AUR branch
            self.logger.info(f"Changes successfully pushed to AUR for {pkgbase}."); op_result.git_push_to_aur_ok = True; return True
        except Exception as e:
            op_result.error_message = f"Git commit/push to AUR failed for {pkgbase}: {e}"
            self.logger.error(op_result.error_message, exc_info=self.config.debug_mode); return False

    def _sync_changes_to_source_repo(self, pkg_status: PackageOverallStatus, op_result: BuildOpResult) -> bool:
        if not op_result.git_commit_to_aur_ok and not op_result.changes_made_to_aur_clone_files:
            self.logger.info(f"Skipping sync to source repo for {pkg_status.pkgbase}: No changes made or pushed to AUR."); op_result.source_repo_sync_ok = True; return True
        if op_result.changes_made_to_aur_clone_files and not op_result.git_commit_to_aur_ok: # Files changed but not pushed
             self.logger.info(f"Proceeding with sync to source for {pkg_status.pkgbase} as local changes were made, even if AUR push was skipped/failed.")

        pkgbase = pkg_status.pkgbase
        self.logger.info(f"Syncing changed files for {pkgbase} back to source GitHub repo {self.config.github_repo}...")
        files_to_consider_for_sync = ["PKGBUILD", ".SRCINFO"] 
        if (op_result.aur_clone_dir_abs / ".nvchecker.toml").is_file(): files_to_consider_for_sync.append(".nvchecker.toml")
        
        global_build_inputs_data = {}
        try:
            with open(self.config.package_build_inputs_path, "r") as f: global_build_inputs_data = json.load(f)
        except Exception as e: self.logger.warning(f"Could not read {self.config.package_build_inputs_path} for source file list: {e}")

        for src_entry in global_build_inputs_data.get(pkgbase, {}).get("sources", []):
            src_filename_part = src_entry.split('::')[0]
            if not ("://" in src_entry or src_entry.startswith("git+")) and (op_result.aur_clone_dir_abs / src_filename_part).is_file():
                if src_filename_part not in files_to_consider_for_sync: files_to_consider_for_sync.append(src_filename_part)

        all_synced_successfully = True
        for file_name_in_clone in files_to_consider_for_sync:
            file_path_in_aur_clone_abs = op_result.aur_clone_dir_abs / file_name_in_clone
            if not file_path_in_aur_clone_abs.is_file(): self.logger.debug(f"File {file_name_in_clone} not in AUR clone for {pkgbase}. Skipping sync."); continue

            path_in_source_repo_relative_to_workspace = pkg_status.pkgbuild_dir_rel_to_workspace / file_name_in_clone
            self.logger.info(f"Syncing '{file_name_in_clone}' to source repo path: '{path_in_source_repo_relative_to_workspace}'")
            gh_env_extra = {"GITHUB_TOKEN": self.config.gh_token}
            try:
                content_b64 = base64.b64encode(file_path_in_aur_clone_abs.read_bytes()).decode("utf-8")
                sha_res = self.builder_runner.run(["gh", "api", f"repos/{self.config.github_repo}/contents/{str(path_in_source_repo_relative_to_workspace)}", "--jq", ".sha"], check=False)
                current_sha = sha_res.stdout.strip() if sha_res.returncode == 0 and sha_res.stdout.strip() != "null" else None
                commit_version_suffix = f" (v{op_result.final_pkgbuild_version_in_clone or op_result.target_version_for_build or 'unknown'})"
                source_repo_commit_message = f"Sync: Update {file_name_in_clone} for {pkgbase}{commit_version_suffix}"
                update_fields = ["-f", f"message={source_repo_commit_message}", "-f", f"content={content_b64}"]
                if current_sha: update_fields.extend(["-f", f"sha={current_sha}"])
                self.builder_runner.run(["gh", "api", "--method", "PUT", f"repos/{self.config.github_repo}/contents/{str(path_in_source_repo_relative_to_workspace)}"] + update_fields, check=True, env_extra=gh_env_extra)
                self.logger.info(f"Successfully synced '{file_name_in_clone}' to source repo path '{path_in_source_repo_relative_to_workspace}'.")
                op_result.files_synced_to_source_repo.append(str(path_in_source_repo_relative_to_workspace))
            except Exception as e_sync:
                error_detail = f"Failed to sync '{file_name_in_clone}' for {pkgbase} to source repo: {e_sync}"
                op_result.error_message = (op_result.error_message + "; " if op_result.error_message else "") + error_detail
                self.logger.error(error_detail, exc_info=self.config.debug_mode); all_synced_successfully = False
        op_result.source_repo_sync_ok = all_synced_successfully; return all_synced_successfully

    def _collect_package_artifacts(self, pkg_status: PackageOverallStatus, op_result: BuildOpResult) -> bool:
        pkgbase = pkg_status.pkgbase
        pkg_artifact_output_dir_abs = self.config.artifacts_dir_base / pkgbase 
        self.logger.info(f"Collecting artifacts for {pkgbase} from {op_result.aur_clone_dir_abs} to {pkg_artifact_output_dir_abs}...")

        def _try_copy(src_path: Path, dest_dir: Path, dest_rel_path: Optional[Path] = None):
            if src_path.is_file():
                try:
                    final_dest_path = dest_dir / (dest_rel_path if dest_rel_path else src_path.name)
                    final_dest_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_path, final_dest_path)
                    self.logger.debug(f"Copied artifact: {src_path.name} to {final_dest_path}")
                except Exception as e:
                    self.logger.warning(f"Could not copy artifact {src_path}: {e}")
            else:
                self.logger.debug(f"Artifact source file not found, skipping: {src_path}")

        try:
            # PKGBUILD, .SRCINFO, .nvchecker.toml from clone root
            for file_name in ["PKGBUILD", ".SRCINFO", ".nvchecker.toml"]:
                _try_copy(op_result.aur_clone_dir_abs / file_name, pkg_artifact_output_dir_abs)

            # Logs from clone root (e.g., makepkg logs)
            for log_file_path_abs in op_result.aur_clone_dir_abs.glob("*.log"):
                 _try_copy(log_file_path_abs, pkg_artifact_output_dir_abs)
            
            # Files from pkg/ subdirectories (.BUILDINFO, .MTREE, .PKGINFO)
            pkg_build_artifacts_dir = op_result.aur_clone_dir_abs / "pkg"
            if pkg_build_artifacts_dir.is_dir():
                for item_type in [".BUILDINFO", ".MTREE", ".PKGINFO"]:
                    for found_file in pkg_build_artifacts_dir.rglob(f"*{item_type}"): # Recursive glob
                        # Create same relative structure in artifacts, e.g. artifacts/pkgbase/pkg/subpkgname/.BUILDINFO
                        relative_path_in_clone = found_file.relative_to(op_result.aur_clone_dir_abs)
                        _try_copy(found_file, pkg_artifact_output_dir_abs, dest_rel_path=relative_path_in_clone)
            
            # DO NOT copy built package archives (*.pkg.tar.zst) to artifacts dir
            # They are handled by GitHub Releases.

            self.logger.info(f"Artifact collection for {pkgbase} finished.")
            return True
        except Exception as e_artifact_main: # Catch-all for unexpected issues during collection logic
            error_detail = f"Error during artifact collection logic for {pkgbase}: {e_artifact_main}"
            op_result.error_message = (op_result.error_message + "; " if op_result.error_message else "") + error_detail
            self.logger.warning(error_detail, exc_info=self.config.debug_mode)
            return False


    def _cleanup_package_build_environment(self, op_result: BuildOpResult) -> bool:
        if op_result.package_specific_build_dir_abs and op_result.package_specific_build_dir_abs.exists():
            self.logger.info(f"Cleaning up build directory: {op_result.package_specific_build_dir_abs}")
            try:
                self.runner.run(["rm", "-rf", str(op_result.package_specific_build_dir_abs)], check=True, run_as_user="root")
                return True
            except Exception as e:
                error_detail = f"Failed to cleanup package build directory {op_result.package_specific_build_dir_abs}: {e}"
                op_result.error_message = (op_result.error_message + "; " if op_result.error_message else "") + error_detail
                self.logger.warning(error_detail); return False
        self.logger.debug(f"Build directory {op_result.package_specific_build_dir_abs} not found/set, skipping cleanup.")
        return True

    def _perform_package_build_operations(self, pkg_status: PackageOverallStatus, build_mode: str, build_inputs_data: Dict[str, Any]) -> BuildOpResult:
        pkgbase = pkg_status.pkgbase
        start_group(f"Processing Package: {pkgbase} (Mode: {build_mode})")
        op_result = BuildOpResult(package_name=pkgbase)
        try:
            if not self._setup_package_build_environment(pkg_status, op_result): raise Exception(op_result.error_message or "Env setup failed.")
            if not self._install_package_dependencies(pkg_status, op_result, build_inputs_data): raise Exception(op_result.error_message or "Dep install failed.")
            if not self._manage_pkgbuild_versioning_in_clone(pkg_status, op_result): raise Exception(op_result.error_message or "PKGBUILD versioning failed.")
            if not self._execute_makepkg_and_install(pkg_status, op_result, build_mode): raise Exception(op_result.error_message or "Makepkg/install failed.")
            if not self._handle_github_release(pkg_status, op_result, build_mode): self.logger.warning(f"GitHub Release issue for {pkgbase}: {op_result.error_message or 'Unknown GH Release issue'}")
            if not self._commit_and_push_to_aur_repo(pkg_status, op_result, build_inputs_data): raise Exception(op_result.error_message or "AUR commit/push failed.")
            if not self._sync_changes_to_source_repo(pkg_status, op_result): self.logger.warning(f"Source repo sync issue for {pkgbase}: {op_result.error_message or 'Unknown source sync issue'}")

            op_result.success = (op_result.setup_env_ok and op_result.dependencies_installed_ok and 
                                 op_result.pkgbuild_versioned_ok and op_result.makepkg_ran_ok and 
                                 op_result.local_install_ok and op_result.git_commit_to_aur_ok and op_result.git_push_to_aur_ok)
            if op_result.success: self.logger.info(f"Successfully processed and updated package {pkgbase}.")
            else: self.logger.warning(f"Package {pkgbase} processed, but op_result.success is False. Error: {op_result.error_message}")
        except Exception as e_main_build_op:
            self.logger.error(f"Main build operation for {pkgbase} failed critically: {e_main_build_op}", exc_info=self.config.debug_mode)
            if not op_result.error_message: op_result.error_message = str(e_main_build_op)
            op_result.success = False
        finally:
            self._collect_package_artifacts(pkg_status, op_result)
            self._cleanup_package_build_environment(op_result)
        end_group()
        return op_result    

    def _write_summary_to_file(self, overall_statuses: List[PackageOverallStatus]):
        if not self.config.github_step_summary_file: self.logger.info("No GITHUB_STEP_SUMMARY_FILE_PATH, skipping summary."); return
        start_group("Generating Workflow Summary")
        try:
            with open(self.config.github_step_summary_file, "w", encoding="utf-8") as f:
                f.write("## Arch Package Update Summary\n\n| Package (`pkgbase`) | Version (Local) | Status | Details | AUR Link | Build Artifacts Context |\n|---|---|---|---|---|---|\n")
                if not self.build_operation_results and not any(s.needs_update or s.local_is_ahead or s.comparison_errors for s in overall_statuses):
                     f.write("| *No updates or significant status changes found* | - | - | - | - | - |\n")

                build_results_map = {res.package_name: res for res in self.build_operation_results}
                for status in overall_statuses:
                    pkgbase = status.pkgbase
                    local_ver_str = self.version_comparator.get_full_version_string(
                        status.local_pkgbuild_info.pkgver if status.local_pkgbuild_info else None,
                        status.local_pkgbuild_info.pkgrel if status.local_pkgbuild_info else None
                    ) or "N/A"
                    aur_link = f"[{pkgbase}](https://aur.archlinux.org/packages/{pkgbase})"
                    status_text, details_text, logs_link_context = "Up-to-date", "", "N/A"

                    if (build_res := build_results_map.get(pkgbase)):
                        version_shown = build_res.final_pkgbuild_version_in_clone or build_res.target_version_for_build or "Unknown"
                        if build_res.success:
                            status_text = f"✅ Built: v{version_shown}"
                            if build_res.git_push_to_aur_ok : details_text += "AUR updated. " # git_push_to_aur_ok implies commit_ok
                            if build_res.github_release_ok: details_text += "GH Release. "
                            if build_res.source_repo_sync_ok: details_text += "Source repo synced."
                        else:
                            status_text = f"❌ Build Failed: v{version_shown}"
                            details_text = f"<small>{(build_res.error_message or 'Unknown error').replace('|','-').replace(chr(10),'<br>')}</small>"
                        if build_res.log_artifact_subdir and self.config.github_run_id:
                            logs_link_context = f"`{build_res.log_artifact_subdir.name}/` in `build-artifacts-{self.config.github_run_id}.zip`"
                        elif build_res.log_artifact_subdir: logs_link_context = f"`{build_res.log_artifact_subdir.name}/` in artifact zip"
                    elif status.needs_update: status_text, details_text = f"⚠️ Update Pending: to v{status.version_for_update}", f"Source: {status.update_source_type}."
                    elif status.local_is_ahead: status_text, details_text = "ℹ️ Local Ahead", f"Local ({local_ver_str}) > sources."
                    elif status.comparison_errors: status_text, details_text = f"❗ Error", "; ".join(status.comparison_errors)
                    f.write(f"| **{pkgbase}** | `{local_ver_str}` | {status_text} | {details_text} | {aur_link} | {logs_link_context} |\n")
            self.logger.info(f"Workflow summary written to {self.config.github_step_summary_file}")
        except Exception as e: log_error("SummaryWriteFail", f"Failed to write workflow summary: {e}")
        end_group()

    def run_workflow(self):
        log_notice("WorkflowStart", "Arch Package Management Workflow Starting...")
        if not self._initial_environment_setup():
            log_error("Fatal", "Environment setup failed. Exiting.")
            if self.config.github_step_summary_file:
                 with open(self.config.github_step_summary_file, "a") as f: f.write("| **SETUP** | N/A | ❌ Failure: Env setup | - | - | Check Logs |\n")
            return 1

        local_pkg_data = self.pkgbuild_parser.fetch_all_local_pkgbuild_data(self.config.pkgbuild_files_root_in_workspace)
        if not local_pkg_data: log_warning("NoLocalData", "No local PKGBUILD data found.")
        aur_pkg_data = self.aur_fetcher.fetch_data_for_maintainer(self.config.aur_maintainer_name)
        
        nvchecker_oldver_input = {}
        for pkgbase in set(local_pkg_data.keys()) | set(aur_pkg_data.keys()):
            version_to_use = (aur_pkg_data.get(pkgbase, {}).get("aur_pkgver") or 
                              (local_pkg_data.get(pkgbase).pkgver if local_pkg_data.get(pkgbase) else None))
            if version_to_use: nvchecker_oldver_input[pkgbase] = {"version": version_to_use}
        
        nvchecker_global_results = self.nvchecker.run_global_nvchecker(self.config.pkgbuild_files_root_in_workspace, nvchecker_oldver_input)
        all_package_statuses = self._analyze_package_statuses(local_pkg_data, aur_pkg_data, nvchecker_global_results)
        
        try: # Save status report artifact
            self.config.package_status_report_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.config.package_status_report_path, "w") as f: json.dump([dataclasses.asdict(s) for s in all_package_statuses], f, indent=2, default=str)
            self.runner.run(["cp", str(self.config.package_status_report_path), str(self.config.artifacts_dir_base / self.config.package_status_report_path.name)], check=False)
            log_notice("StatusArtifact", f"Package status report saved to artifacts: {self.config.package_status_report_path.name}")
        except Exception as e: log_warning("StatusArtifactFail", f"Failed to save status report artifact: {e}")

        packages_needing_build_action = [s for s in all_package_statuses if s.needs_update and s.is_update_candidate and s.local_pkgbuild_info and s.pkgbuild_dir_rel_to_workspace]
        if not packages_needing_build_action: log_notice("NoUpdatesToBuild", "No packages require build actions."); self._write_summary_to_file(all_package_statuses); return 0
        log_notice("UpdatesFound", f"Found {len(packages_needing_build_action)} package(s) requiring build actions.")

        if not self._prepare_inputs_for_build_script(packages_needing_build_action):
            log_error("Fatal", "Failed to prepare inputs for build script. Exiting."); self._write_summary_to_file(all_package_statuses); return 1
        with open(self.config.package_build_inputs_path, "r") as f: build_inputs_json_content = json.load(f)

        overall_success = True
        for pkg_status_to_build in packages_needing_build_action:
            build_mode = self._determine_build_mode(pkg_status_to_build)
            current_pkg_build_inputs = build_inputs_json_content.get(pkg_status_to_build.pkgbase, {})
            build_op_res = self._perform_package_build_operations(pkg_status_to_build, build_mode, current_pkg_build_inputs)
            self.build_operation_results.append(build_op_res)
            if not build_op_res.success: overall_success = False; log_error("PackageBuildFail", f"Build operation failed for {pkg_status_to_build.pkgbase}.")
        
        self._write_summary_to_file(all_package_statuses)
        if not overall_success: log_error("WorkflowEndFail", "One or more package build operations failed."); return 1
        log_notice("WorkflowEndSuccess", "All tasks completed successfully."); return 0

def main():
    try:
        cfg = Config(
            aur_maintainer_name=os.environ["AUR_MAINTAINER_NAME"],
            github_repo=os.environ["GITHUB_REPO_OWNER_SLASH_NAME"],
            pkgbuild_files_root_in_workspace=Path(os.environ["PKGBUILD_FILES_ROOT"]).resolve(),
            git_commit_user_name=os.environ["GIT_COMMIT_USER_NAME"],
            git_commit_user_email=os.environ["GIT_COMMIT_USER_EMAIL"],
            gh_token=os.environ["GH_TOKEN_FOR_RELEASES_AND_NVCHECKER"],
        )
    except KeyError as e: log_error("ConfigFatal", f"Missing critical env var: {e}."); sys.exit(2)
    except Exception as e_cfg: log_error("ConfigFatal", f"Error initializing config: {e_cfg}"); sys.exit(2)

    manager = ArchPackageManager(cfg)
    sys.exit(manager.run_workflow())

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", stream=sys.stderr)
    main()
