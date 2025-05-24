#!/usr/bin/env python3

import argparse
import json
import logging
import requests
import os
import subprocess
import sys
import re
import shlex 
from dataclasses import dataclass, asdict, field
from pathlib import Path
import base64
import shutil
import tempfile
from typing import List, Tuple, Dict, Optional, Any

# Setup logger early, but allow ArchPackageBuilder to customize further
# Logs will be configured to go to stderr by ArchPackageBuilder instance
logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s", stream=sys.stderr)
logger = logging.getLogger("arch_builder_script")


@dataclass
class CommandResult:
    command: List[str]
    returncode: int
    stdout: str
    stderr: str


class SubprocessRunner:
    def __init__(self, logger_instance: logging.Logger):
        self.logger = logger_instance

    def run_command(
        self, cmd: List[str], check: bool = True, input_data: Optional[str] = None,
        env: Optional[Dict[str, str]] = None
    ) -> CommandResult:
        self.logger.debug(f"Running command: {shlex.join(cmd)}")
        # Merge with current environment if new env is provided
        current_env = os.environ.copy()
        if env:
            current_env.update(env)
        
        try:
            process = subprocess.run(
                cmd, check=check, text=True, capture_output=True, input=input_data, env=current_env
            )
            return CommandResult(
                command=cmd,
                returncode=process.returncode,
                stdout=process.stdout,
                stderr=process.stderr,
            )
        except subprocess.CalledProcessError as e:
            error_msg = f"Command '{shlex.join(e.cmd)}' failed with return code {e.returncode}\n"
            if e.stdout:
                error_msg += f"Stdout: {e.stdout.strip()}\n"
            if e.stderr:
                error_msg += f"Stderr: {e.stderr.strip()}\n"
            self.logger.error(error_msg.strip())
            if check:
                raise
            return CommandResult(
                command=cmd,
                returncode=e.returncode,
                stdout=e.stdout or "",
                stderr=e.stderr or "",
            )


@dataclass
class BuildConfig:
    github_repo: str
    github_token: str
    github_workspace: str
    package_name: str
    depends_json: str
    pkgbuild_path: str
    commit_message: str
    base_build_dir: Path # New: Base directory for creating package-specific build dirs
    build_mode: str = "nobuild" 
    artifacts_dir: Optional[str] = None
    debug: bool = False


@dataclass
class BuildResult:
    success: bool
    package_name: str
    version: Optional[str] = None
    built_packages: List[str] = field(default_factory=list) 
    error_message: Optional[str] = None
    changes_detected: bool = False


class ArchPackageBuilder:
    RELEASE_BODY = "To install, run: sudo pacman -U PACKAGENAME.pkg.tar.zst"
    TRACKED_FILES = ["PKGBUILD", ".SRCINFO", ".nvchecker.toml"] # Base set of files

    def __init__(self, config: BuildConfig):
        self.config = config
        self.logger = self._setup_logger() 
        
        # Create package-specific build directory inside the base_build_dir
        # Ensure base_build_dir exists (should be handled by calling script, but double check)
        self.config.base_build_dir.mkdir(parents=True, exist_ok=True) 
        # Use a unique name for the package-specific build directory
        unique_suffix = os.urandom(4).hex()
        self.build_dir = self.config.base_build_dir / f"build-{config.package_name}-{unique_suffix}"
        try:
            self.build_dir.mkdir(parents=True, exist_ok=True)
            self.logger.info(f"Created package build directory: {self.build_dir}")
        except Exception as e:
            self.logger.error(f"Failed to create package build directory {self.build_dir}: {e}")
            # This is a critical failure for the builder instance
            raise RuntimeError(f"Failed to create package build directory: {e}") from e

        self.tracked_files: List[str] = self.TRACKED_FILES.copy()
        self.result = BuildResult(success=True, package_name=config.package_name) # Start optimistic
        self.subprocess_runner = SubprocessRunner(self.logger)
        
        self.artifacts_path: Optional[Path] = None
        if self.config.artifacts_dir:
            self.artifacts_path = Path(self.config.artifacts_dir)
            # Ensure artifacts_path (e.g., /github_workspace/artifacts/package_name) exists
            try:
                self.artifacts_path.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                self.logger.warning(f"Could not create artifacts directory {self.artifacts_path}: {e}. Artifacts may not be saved.")
                self.artifacts_path = None # Disable artifact saving if dir creation fails


    def _setup_logger(self) -> logging.Logger:
        # Use the global logger name, but configure its handler and level per instance
        instance_logger = logging.getLogger("arch_builder_script") 
        for handler in instance_logger.handlers[:]:
            instance_logger.removeHandler(handler)
            
        handler = logging.StreamHandler(sys.stderr) # Ensure logs go to STDERR
        formatter = logging.Formatter("[%(levelname)s] %(name)s: %(message)s") # Keep it simple for GHA
        handler.setFormatter(formatter)
        instance_logger.addHandler(handler)
        instance_logger.setLevel(logging.DEBUG if self.config.debug else logging.INFO)
        instance_logger.propagate = False 
        return instance_logger

    def authenticate_github(self) -> bool:
        # Ensure HOME is set for gh CLI operations if they rely on it for config
        # self.subprocess_runner already inherits env, which should include HOME from the caller
        try:
            self.subprocess_runner.run_command(
                ["gh", "auth", "login", "--with-token"], input_data=self.config.github_token
            )
            self.subprocess_runner.run_command(["gh", "auth", "status"])
            return True
        except Exception as e:
            self.logger.error(f"GitHub authentication error: {str(e)}")
            self.result.error_message = f"GitHub authentication failed: {str(e)}"
            return False

    def setup_build_environment(self):
        # AUR repo will be cloned into self.build_dir
        aur_repo = f"ssh://aur@aur.archlinux.org/{self.config.package_name}.git"
        self.subprocess_runner.run_command(
            ["git", "clone", aur_repo, str(self.build_dir)]
        )
        # All subsequent operations relative to build_dir should be done after cd
        os.chdir(self.build_dir)
        self.logger.info(f"Changed directory to {self.build_dir}")


    def collect_package_files(self):
        workspace_source_path = Path(self.config.github_workspace) / self.config.pkgbuild_path
        self.logger.info(f"Collecting package files from {workspace_source_path} to {self.build_dir}")

        if not workspace_source_path.is_dir():
            self.logger.warning(f"Provided PKGBUILD path {workspace_source_path} is not a directory. Skipping file collection.")
            return

        # shutil.copytree can simplify this, but glob allows more control if needed later.
        # For now, let's ensure it copies symlinks as symlinks, and overwrites.
        try:
            shutil.copytree(workspace_source_path, self.build_dir, dirs_exist_ok=True, symlinks=True)
            self.logger.info(f"Copied directory tree from {workspace_source_path} to {self.build_dir}")
        except Exception as e:
            self.logger.error(f"Error copying files from {workspace_source_path} to {self.build_dir}: {e}")
            # This might be a critical error depending on what failed.
            # For now, log and continue; subsequent steps might fail if files are missing.
            self.result.error_message = f"Failed to copy package files: {e}"
            raise # Re-raise to stop further processing if essential files are missing

    def process_package_sources(self):
        # This function primarily ensures local files mentioned in PKGBUILD's 'source' array are tracked by git
        # It assumes files are already in self.build_dir (copied by collect_package_files)
        package_name = self.config.package_name
        json_file_path = Path(self.config.depends_json) 

        if not json_file_path.is_file():
            self.logger.error(f"Depends JSON file not found: {json_file_path}")
            self.result.error_message = f"Depends JSON file not found: {json_file_path}"
            return False 

        try:
            with open(json_file_path, "r") as file:
                data = json.load(file)
        except json.JSONDecodeError as e:
            self.logger.error(f"Error decoding JSON from {json_file_path}: {e}")
            self.result.error_message = f"Error decoding JSON from {json_file_path}: {e}"
            return False

        self.logger.info(f"Loaded source/dependency JSON data for package '{package_name}'")

        if package_name not in data or "sources" not in data[package_name]:
            self.logger.info(f"No 'sources' array found for package '{package_name}' in JSON or package not in JSON. Only PKGBUILD defaults apply for source tracking.")
            return True

        sources_from_json = data[package_name].get("sources", [])
        
        for source_entry_raw in sources_from_json:
            # source entry can be "filename" or "filename::url"
            source_filename = source_entry_raw.split('::')[0]
            if self._is_url(source_entry_raw): # Handles "filename::url" and "url"
                self.logger.debug(f"Source is a URL, downloaded by makepkg: {source_entry_raw}")
            else: 
                # This is a local file name, should exist in self.build_dir
                source_file_in_build_dir = self.build_dir / source_filename
                if source_file_in_build_dir.is_file():
                    if source_filename not in self.tracked_files:
                        self.tracked_files.append(source_filename)
                    self.logger.info(f"Local source file '{source_filename}' will be tracked by git.")
                else:
                    self.logger.warning(f"Local source file '{source_filename}' (from JSON) not found at {source_file_in_build_dir}. PKGBUILD might fail or it might be generated.")
        return True


    def _is_url(self, source_string: str) -> bool:
        # Check if the part *after* an optional "filename::" is a URL
        parts = source_string.split('::')
        url_candidate = parts[-1] # If "file::url", this is url. If "url", this is url. If "file", this is file.
        return "://" in url_candidate or url_candidate.startswith("git+")


    def process_dependencies(self) -> bool:
        # Runs as `builder` due to buildscript2.py's invocation. HOME should be /home/builder.
        # Paru will use sudo internally for pacman.
        package_name = self.config.package_name
        json_file_path = Path(self.config.depends_json)

        if not json_file_path.is_file():
            self.logger.error(f"Depends JSON file not found: {json_file_path}")
            self.result.error_message = f"File not found: {json_file_path}"
            return False
        
        try:
            with open(json_file_path, "r") as file:
                data = json.load(file)
        except json.JSONDecodeError as e:
            self.logger.error(f"Error decoding JSON from {json_file_path}: {e}")
            self.result.error_message = f"Error decoding JSON: {e}"
            return False

        self.logger.debug(f"Loaded JSON data for dependencies for package '{package_name}'")

        if package_name not in data:
            self.logger.info(f"Package '{package_name}' not found in JSON. Assuming no specific dependencies to install from JSON.")
            return True

        package_data = data[package_name]
        # Combine all types of dependencies, remove duplicates and version constraints for paru resolution
        deps_to_resolve = list(set(
            package_data.get("depends", []) +
            package_data.get("makedepends", []) +
            package_data.get("checkdepends", [])
        ))
        
        if not deps_to_resolve:
            self.logger.info(f"No dependencies listed for '{package_name}' in JSON.")
            return True

        self.logger.info(f"Raw dependencies for {package_name} from JSON: {deps_to_resolve}")
        
        resolved_packages_to_install = []
        for dep_full_string in deps_to_resolve:
            dep_name_only = re.split(r'[<>=!]', dep_full_string)[0].strip()
            if not dep_name_only: continue # Skip empty strings resulting from split

            # Check if package provides itself (repo or AUR)
            # Using paru -Ss should find it in repos or AUR if name matches
            check_cmd = ["paru", "-Ss", f"^{re.escape(dep_name_only)}$"] 
            result = self.subprocess_runner.run_command(check_cmd, check=False)

            if result.returncode == 0 and result.stdout.strip(): 
                self.logger.debug(f"Dependency '{dep_name_only}' found directly by paru.")
                resolved_packages_to_install.append(dep_name_only)
            else: # Check if it's a virtual package provided by something else
                self.logger.debug(f"Dependency '{dep_name_only}' not directly found. Checking providers via AUR RPC for '{dep_name_only}'.")
                try:
                    # AUR RPC 'search' by 'provides'
                    # Timeout increased, can be slow
                    aur_response = requests.get(
                        f"https://aur.archlinux.org/rpc/v5/search/{dep_name_only}?by=provides",
                        timeout=15 
                    )
                    aur_response.raise_for_status()
                    aur_data = aur_response.json()
                    if aur_data.get("results"):
                        provider_name = aur_data["results"][0].get("Name") # Take first provider
                        if provider_name:
                            self.logger.info(f"Found AUR provider '{provider_name}' for virtual package '{dep_name_only}'.")
                            resolved_packages_to_install.append(provider_name)
                        else:
                             self.logger.warning(f"AUR RPC search for provider of '{dep_name_only}' gave result but no 'Name' field.")
                    else:
                        self.logger.warning(f"No AUR provider found for dependency: {dep_name_only} (original: {dep_full_string}). It might be a repo package not yet synced, a typo, or a non-AUR provided virtual package.")
                except requests.RequestException as e:
                    self.logger.warning(f"Error querying AUR for provider of '{dep_name_only}': {e}")
                except json.JSONDecodeError as e:
                    self.logger.warning(f"Error decoding AUR RPC response for '{dep_name_only}': {e}")
        
        resolved_packages_to_install = sorted(list(set(resolved_packages_to_install))) # Unique and sorted
        if not resolved_packages_to_install:
            self.logger.info("No dependencies to install after resolution.")
            return True
            
        self.logger.info(f"Final list of dependencies to attempt installing for '{package_name}': {resolved_packages_to_install}")
        install_cmd = [
            "paru", "-S", "--norebuild", "--noconfirm", "--needed",
        ] + resolved_packages_to_install
        
        # Paru is run as builder user. It will use sudo for pacman internally.
        # HOME should be /home/builder.
        install_result = self.subprocess_runner.run_command(install_cmd, check=False)
        if install_result.returncode == 0:
            self.logger.info(f"Dependencies for package '{package_name}' installed successfully.")
            return True
        else:
            self.logger.error(f"Dependency installation failed for {package_name}. Paru exit code: {install_result.returncode}")
            self.logger.error(f"Paru stdout:\n{install_result.stdout}")
            self.logger.error(f"Paru stderr:\n{install_result.stderr}")
            self.result.error_message = f"Dependency installation failed for {package_name} (paru exit code {install_result.returncode}). Check logs."
            return False

# In buildscript2.py / ArchPackageBuilder class

    def check_version_update(self) -> Optional[str]:
        nvchecker_config_path = self.build_dir / ".nvchecker.toml"
        if not nvchecker_config_path.is_file():
            self.logger.info(f".nvchecker.toml not found in {self.build_dir}, skipping version check.")
            return self._get_current_pkgbuild_version() # Return current version if no check

        # Construct absolute path to keyfile.toml
        # self.config.base_build_dir is e.g. /home/builder/pkg_builds
        # NVCHECKER_RUN_DIR is a sibling, e.g. /home/builder/nvchecker-run
        nvchecker_run_dir_abs = self.config.base_build_dir.parent / "nvchecker-run"
        keyfile_abs_path = nvchecker_run_dir_abs / "keyfile.toml"

        if not keyfile_abs_path.is_file():
            self.logger.warning(f"NVChecker keyfile not found at {keyfile_abs_path}. Version check might fail for sources requiring auth.")
        
        try:
            cmd = [
                "nvchecker",
                "-c", str(nvchecker_config_path), 
            ]
            if keyfile_abs_path.is_file():
                 cmd.extend(["-k", str(keyfile_abs_path)])

            # nvchecker (without --logger json) sends "updated to X" to stderr.
            # Other info/errors can also go to stderr. stdout might be for specific formatted output.
            result = self.subprocess_runner.run_command(cmd, check=False) 
            
            self.logger.debug(f"NVChecker command: {shlex.join(cmd)}")
            self.logger.debug(f"NVChecker exit code: {result.returncode}")
            self.logger.debug(f"NVChecker stdout: {result.stdout.strip()}")
            self.logger.debug(f"NVChecker stderr: {result.stderr.strip()}")

            new_version = None
            # Parse stderr for the "updated to X" message
            # Example stderr line: "[I 05-23 12:42:07.397 core:416] apache-spark: updated to 4.0.0"
            # Example stderr line for already new: "[I 05-23 ... core:NNN] pkgname: current 1.2.3" (or similar)
            # Example no update found: stderr might be empty or just log noise.
            
            # Regex to find "pkgname: updated to new_version" or "updated to new_version"
            # Make it more specific to nvchecker's logging format if possible
            # nvchecker info log format is typically `[I DATE TIME module:LINE] pkgname: message`
            
            update_pattern = re.compile(rf":\s*updated to\s+([^\s,]+)", re.IGNORECASE)
            # Check if self.config.package_name is mentioned for safety, though nvchecker usually processes one .toml
            # that should correspond to the package.

            for line in result.stderr.splitlines(): # MODIFIED: Parse stderr
                line = line.strip()
                if not line: continue

                match = update_pattern.search(line)
                if match and self.config.package_name in line : # Ensure it's for our package and an update
                    new_version_candidate = match.group(1)
                    new_version = new_version_candidate
                    self.logger.info(f"NVChecker (from stderr) found new version: {new_version} for {self.config.package_name}")
                    self._update_pkgbuild_version(new_version) # This sets self.result.changes_detected
                    self.result.version = new_version 
                    break 
                elif f"{self.config.package_name}: current" in line: # Example: "pkgname: current 1.2.3"
                    self.logger.info(f"NVChecker (from stderr) reports version for {self.config.package_name} is already up-to-date.")
                    current_ver_from_pkgbuild = self._get_current_pkgbuild_version()
                    if current_ver_from_pkgbuild: self.result.version = current_ver_from_pkgbuild
                    break # Stop processing output

            if not new_version and not self.result.version : # If no update found and version not set by "current"
                self.logger.info(f"NVChecker did not report any new or current version for {self.config.package_name} from stderr.")
                self.result.version = self._get_current_pkgbuild_version() # Fallback to PKGBUILD
            elif not new_version and self.result.version:
                 self.logger.info(f"NVChecker found no new version; version remains {self.result.version} (likely current or from PKGBUILD).")


            return self.result.version 

        except Exception as e:
            self.logger.error(f"Version check using NVChecker failed with exception: {e}", exc_info=self.config.debug)
        
        # Fallback if nvchecker fails or no version set
        if not self.result.version:
            self.result.version = self._get_current_pkgbuild_version()
        return self.result.version

    def _get_current_pkgbuild_version(self) -> Optional[str]:
        pkgbuild_path = self.build_dir / "PKGBUILD"
        if not pkgbuild_path.is_file():
            self.logger.error("PKGBUILD not found in build directory for version retrieval.")
            return None
        try:
            content = pkgbuild_path.read_text()
            match = re.search(r"^\s*pkgver=([^\s#]+)", content, re.MULTILINE)
            if match:
                return match.group(1)
        except Exception as e:
            self.logger.error(f"Error reading pkgver from PKGBUILD: {e}")
        return None


    def _update_pkgbuild_version(self, new_version: str):
        pkgbuild_path = self.build_dir / "PKGBUILD"
        if not pkgbuild_path.is_file():
            self.logger.error("PKGBUILD not found in build directory for version update.")
            self.result.error_message = "PKGBUILD not found for version update."
            raise FileNotFoundError("PKGBUILD not found for version update")

        content = pkgbuild_path.read_text()
        
        old_pkgver_match = re.search(r"^\s*pkgver=([^\s#]+)", content, re.MULTILINE)
        if not old_pkgver_match:
            self.logger.error("pkgver not found in PKGBUILD.")
            self.result.error_message = "pkgver not found in PKGBUILD."
            raise ValueError("pkgver not found in PKGBUILD")
        old_version = old_pkgver_match.group(1)

        if old_version == new_version:
            self.logger.info(f"PKGBUILD version ({old_version}) already matches new version ({new_version}). No update needed for pkgver.")
            # Even if version is same, pkgrel might need reset if other files changed.
            # For now, only change pkgrel if pkgver changes.
            return

        self.logger.info(f"Updating PKGBUILD: pkgver {old_version} -> {new_version}, pkgrel -> 1")
        content = re.sub(
            r"(^\s*pkgver=)([^\s#]+)",
            rf"\g<1>{new_version}",
            content,
            count=1,
            flags=re.MULTILINE,
        )
        content = re.sub(
            r"(^\s*pkgrel=)([^\s#]+)",
            r"\g<1>1", # Reset pkgrel to 1
            content,
            count=1,
            flags=re.MULTILINE,
        )
        pkgbuild_path.write_text(content)
        self.result.changes_detected = True 

    def _collect_build_artifacts(self):
        """Collect all build artifacts (logs, PKGBUILD files, package metadata) regardless of build success/failure"""
        if not self.artifacts_path:
            self.logger.debug("No artifacts directory configured, skipping artifact collection.")
            return
            
        self.logger.info(f"Collecting build artifacts to {self.artifacts_path}...")
        
        # Files to collect from the build directory root
        root_artifacts = ["PKGBUILD", ".SRCINFO"]
        
        # Package-specific files from pkg/ subdirectory
        pkg_subdir = self.build_dir / "pkg" / self.config.package_name
        pkg_artifacts = [".BUILDINFO", ".MTREE", ".PKGINFO"]
        
        # Collect root-level artifacts
        for file_name in root_artifacts:
            src_file = self.build_dir / file_name
            if src_file.exists():
                dest_file = self.artifacts_path / file_name
                try:
                    shutil.copy2(src_file, dest_file)
                    self.logger.debug(f"Copied artifact {file_name} to {dest_file}")
                except Exception as e:
                    self.logger.warning(f"Failed to copy artifact {file_name}: {e}")
            else:
                self.logger.debug(f"Artifact file {file_name} not found in build directory")
        
        # Collect package-specific artifacts from pkg/ subdirectory
        if pkg_subdir.exists():
            for file_name in pkg_artifacts:
                src_file = pkg_subdir / file_name
                if src_file.exists():
                    dest_file = self.artifacts_path / file_name
                    try:
                        shutil.copy2(src_file, dest_file)
                        self.logger.debug(f"Copied pkg artifact {file_name} to {dest_file}")
                    except Exception as e:
                        self.logger.warning(f"Failed to copy pkg artifact {file_name}: {e}")
                else:
                    self.logger.debug(f"Package artifact {file_name} not found in {pkg_subdir}")
        else:
            self.logger.debug(f"Package subdirectory {pkg_subdir} does not exist")
        
        # Collect log files (pattern: PACKAGENAME-*.log)
        log_pattern = f"{self.config.package_name}-*.log"
        self.logger.debug(f"Searching for log files with pattern: {log_pattern}")
        for log_file in self.build_dir.glob(log_pattern):
            if log_file.is_file():
                dest_file = self.artifacts_path / log_file.name
                try:
                    shutil.copy2(log_file, dest_file)
                    self.logger.debug(f"Copied log file {log_file.name} to {dest_file}")
                except Exception as e:
                    self.logger.warning(f"Failed to copy log file {log_file.name}: {e}")
        
        # Also collect any additional *.log files (in case there are other logs)
        for log_file in self.build_dir.glob("*.log"):
            if log_file.is_file() and not log_file.name.startswith(f"{self.config.package_name}-"): # Avoid re-copying if pattern matched
                dest_file = self.artifacts_path / log_file.name
                if not dest_file.exists():  # Don't duplicate if already copied above
                    try:
                        shutil.copy2(log_file, dest_file)
                        self.logger.debug(f"Copied additional log file {log_file.name} to {dest_file}")
                    except Exception as e:
                        self.logger.warning(f"Failed to copy additional log file {log_file.name}: {e}")        

    def build_package(self) -> bool:
        if self.config.build_mode not in ["build", "test"]:
            self.logger.info(f"Build mode is '{self.config.build_mode}', skipping actual package build steps (makepkg, release).")
            # If version was updated, .SRCINFO might still need generation for commit_and_push
            if self.result.changes_detected: # True if PKGBUILD version changed
                try:
                    self.logger.info("PKGBUILD changed, regenerating .SRCINFO for nobuild/test mode...")
                    srcinfo_result = self.subprocess_runner.run_command(["makepkg", "--printsrcinfo"])
                    (self.build_dir / ".SRCINFO").write_text(srcinfo_result.stdout)
                    self.logger.info(".SRCINFO regenerated.")
                except Exception as e:
                    self.logger.warning(f"Failed to regenerate .SRCINFO in '{self.config.build_mode}' mode: {e}")
                    # Not fatal for 'nobuild', but might be for 'test' if it implies a build.
                    # For now, let's not make it fatal here.
            return True # Success for 'nobuild' mode.

        build_process_successful = False # Flag to track overall success of this method's core logic
        try:
            # All commands here run as 'builder' user from self.build_dir
            # HOME=/home/builder should be set from the calling script for the python env
            
            self.logger.info("Updating checksums in PKGBUILD (updpkgsums)...")
            self.subprocess_runner.run_command(["updpkgsums"]) # This modifies PKGBUILD
            self.result.changes_detected = True # Assume updpkgsums implies changes or PKGBUILD was already changed
            
            self.logger.info("Regenerating .SRCINFO file...")
            srcinfo_result = self.subprocess_runner.run_command(["makepkg", "--printsrcinfo"])
            (self.build_dir / ".SRCINFO").write_text(srcinfo_result.stdout)
            self.result.changes_detected = True 

            self.logger.info(f"Starting package build (makepkg -Lcs --noconfirm --needed --noprogressbar) in {self.build_dir}...")
            # Added -c (clean up work Dirs), -s (install deps), --needed, --noprogressbar
            # HOME is inherited, should be /home/builder.
            self.subprocess_runner.run_command(["makepkg", "-Lcs", "--noconfirm", "--needed", "--noprogressbar"]) 

            built_package_files = sorted(self.build_dir.glob(f"{self.config.package_name}*.pkg.tar.zst")) # More specific glob
            if not built_package_files: # If main package not found, try any .pkg.tar.zst
                built_package_files = sorted(self.build_dir.glob("*.pkg.tar.zst"))

            if not built_package_files:
                self.logger.error("No packages were built (no .pkg.tar.zst files found).")
                self.result.error_message = "No .pkg.tar.zst files found after makepkg."
                # build_process_successful remains False
            else:
                self.result.built_packages = [p.name for p in built_package_files]
                self.logger.info(f"Successfully built: {', '.join(self.result.built_packages)}")

                # Install the built package(s) locally (as builder, using sudo for pacman)
                self.logger.info(f"Installing built package(s) locally: {', '.join(self.result.built_packages)}")
                self.subprocess_runner.run_command(
                    ["sudo", "pacman", "--noconfirm", "-U"] + [str(p) for p in built_package_files]
                )
                build_process_successful = True # Mark as successful if packages built and installed

            # --- NEW ARTIFACT COLLECTION METHOD ---
            self._collect_build_artifacts()

            if not build_process_successful: # If build (makepkg) failed or produced no packages
                return False 
            
            # If build was successful, proceed with release etc.
            if self.config.build_mode == "build": 
                if self.result.version: 
                    self._create_release(built_package_files)
                else:
                    self.logger.warning("Build mode is 'build' but no version determined. Skipping GitHub Release creation.")
            return True # Build success

        except subprocess.CalledProcessError as e: # Catch errors from run_command if check=True was used
            self.logger.error(f"Build process failed during command: {shlex.join(e.cmd)} (Return code: {e.returncode})", exc_info=self.config.debug)
            self.logger.error(f"Stdout: {e.stdout}")
            self.logger.error(f"Stderr: {e.stderr}")
            self.result.error_message = f"Build failed: {e.cmd} exited {e.returncode}. Stderr: {e.stderr[:200]}"
            self._collect_build_artifacts() # <--- CALL HERE ON EXCEPTION
            return False
        except Exception as e: # Catch other Python exceptions
            self.logger.error(f"Build process failed with Python exception: {e}", exc_info=self.config.debug)
            self.result.error_message = f"Build failed due to Python error: {str(e)}"
            self._collect_build_artifacts() # <--- CALL HERE ON EXCEPTION
            return False

    def _create_release(self, package_files: List[Path]):
        if not self.result.version: # Should have been set by check_version_update or _get_current_pkgbuild_version
            self.logger.error("Cannot create GitHub release: package version is not determined.")
            self.result.error_message = (self.result.error_message or "") + "; GitHub release skipped: version unknown."
            return

        tag_name = f"{self.config.package_name}-{self.result.version}"
        release_title = f"{self.config.package_name} {self.result.version}"
        
        self.logger.info(f"Creating/updating GitHub release with tag '{tag_name}' and title '{release_title}'.")

        try:
            check_release_cmd = ["gh", "release", "view", tag_name, "-R", self.config.github_repo]
            release_exists_result = self.subprocess_runner.run_command(check_release_cmd, check=False)

            if release_exists_result.returncode != 0: # Release does not exist, create it
                self.logger.info(f"Creating new release for tag '{tag_name}'.")
                # Ensure there's at least one package name for the body, fallback if list is empty
                main_package_for_notes = self.result.built_packages[0] if self.result.built_packages else f"{self.config.package_name}-VERSION.pkg.tar.zst"
                release_notes = self.RELEASE_BODY.replace("PACKAGENAME.pkg.tar.zst", main_package_for_notes)
                
                create_release_cmd = [
                    "gh", "release", "create", tag_name,
                    "--title", release_title,
                    "--notes", release_notes,
                    "-R", self.config.github_repo,
                ]
                self.subprocess_runner.run_command(create_release_cmd)
            else:
                self.logger.info(f"Release for tag '{tag_name}' already exists. Will upload/clobber assets.")

            for pkg_file in package_files:
                if pkg_file.is_file():
                    self.logger.info(f"Uploading {pkg_file.name} to release '{tag_name}'.")
                    upload_cmd = [
                        "gh", "release", "upload", tag_name, str(pkg_file),
                        "--clobber", 
                        "-R", self.config.github_repo,
                    ]
                    self.subprocess_runner.run_command(upload_cmd)
                else:
                    self.logger.warning(f"Package file {pkg_file} not found for upload to release.")
            self.logger.info(f"Assets for release '{tag_name}' updated.")

        except subprocess.CalledProcessError as e:
            self.logger.error(f"Failed to create or upload to GitHub release '{tag_name}': CMD: {e.cmd}, RC: {e.returncode}, Stderr: {e.stderr}")
            self.result.error_message = (self.result.error_message or "") + f"; GitHub release failed for {tag_name}: {e.stderr[:100]}"
        except Exception as e: 
            self.logger.error(f"An unexpected error occurred during GitHub release for '{tag_name}': {e}", exc_info=self.config.debug)
            self.result.error_message = (self.result.error_message or "") + f"; Unexpected error during GitHub release for {tag_name}"


    def commit_and_push(self) -> bool:
        # This runs after os.chdir(self.build_dir)
        # Git user should be configured by the calling shell script.
        
        # Ensure all tracked files are up-to-date (e.g. .SRCINFO if PKGBUILD changed)
        # This is typically done in build_package or if version changed. Re-check here.
        if (self.build_dir / "PKGBUILD").is_file() and not (self.build_dir / ".SRCINFO").is_file():
            try:
                self.logger.info(".SRCINFO missing, attempting to generate it...")
                srcinfo_result = self.subprocess_runner.run_command(["makepkg", "--printsrcinfo"])
                (self.build_dir / ".SRCINFO").write_text(srcinfo_result.stdout)
                self.result.changes_detected = True
            except Exception as e:
                self.logger.error(f"Failed to generate .SRCINFO before commit: {e}")
                self.result.error_message = "Failed to generate .SRCINFO"
                return False # Cannot proceed without .SRCINFO

        self.logger.info(f"Preparing to commit to AUR. Tracked files: {self.tracked_files}")
        
        existing_tracked_files_for_add = []
        for file_name in self.tracked_files:
            if (self.build_dir / file_name).is_file():
                existing_tracked_files_for_add.append(file_name)
            else:
                self.logger.warning(f"Tracked file '{file_name}' not found in {self.build_dir}. It will not be added to git.")
        
        if not existing_tracked_files_for_add:
            self.logger.info("No existing tracked files to 'git add'. Checking for other changes...")
        else:
            self.subprocess_runner.run_command(["git", "add"] + existing_tracked_files_for_add)

        if not self._has_git_changes_to_commit():
            self.logger.info("No git changes to commit to AUR.")
            # If self.result.changes_detected is true (e.g. from version bump), but git reports no changes,
            # it could mean the changes were already committed, or .gitattributes are ignoring them.
            # For now, this is considered success (nothing to push).
            return True 

        self.logger.info("Git changes detected. Proceeding with commit and push to AUR.")
        self.result.changes_detected = True # Confirming again as git status shows diff
        
        commit_version_suffix = f" (v{self.result.version})" if self.result.version else ""
        # Ensure commit_message from config is used as base
        final_commit_msg = f"{self.config.commit_message}{commit_version_suffix}"
        
        try:
            # Git commit to local AUR clone
            self.subprocess_runner.run_command(["git", "commit", "-m", final_commit_msg])
            
            # Git push to AUR remote
            self.subprocess_runner.run_command(["git", "push", "origin", "master"]) # Or main, depending on AUR's default
            self.logger.info("Changes successfully committed and pushed to AUR.")

            # Sync changes back to the source GitHub repository
            # Only sync files that were actually part of the AUR commit (existing_tracked_files_for_add)
            # and are expected to be in the source repo.
            self.logger.info("Syncing committed files back to source GitHub repository...")
            for file_name_in_aur_commit in existing_tracked_files_for_add:
                # Path of the file in the AUR clone (self.build_dir)
                file_path_in_aur_clone = self.build_dir / file_name_in_aur_commit
                # Corresponding path in the source GitHub repo.
                # self.config.pkgbuild_path is like "maintain/build/mypackage"
                # file_name_in_aur_commit is like "PKGBUILD"
                path_in_source_repo = Path(self.config.pkgbuild_path) / file_name_in_aur_commit
                
                self._update_github_file(str(path_in_source_repo), file_path_in_aur_clone, final_commit_msg)
            
            return True

        except subprocess.CalledProcessError as e:
            self.logger.error(f"Git operation (commit/push to AUR or update to source repo) failed: CMD: {e.cmd}, RC: {e.returncode}")
            self.logger.error(f"Stdout: {e.stdout}")
            self.logger.error(f"Stderr: {e.stderr}")
            self.result.error_message = (self.result.error_message or "") + f"; Git op failed: {e.cmd} -> {e.stderr[:100]}"
            return False
        except Exception as e:
            self.logger.error(f"Unexpected error during git operations or source repo update: {e}", exc_info=self.config.debug)
            self.result.error_message = (self.result.error_message or "") + f"; Unexpected error in git/source repo update: {str(e)}"
            return False

# In buildscript2.py / ArchPackageBuilder class
    def _update_github_file(self, path_in_repo: str, local_file_path: Path, commit_msg: str):
        self.logger.info(f"Updating '{path_in_repo}' in GitHub repo '{self.config.github_repo}' from local file '{local_file_path}'.")
        try:
            with open(local_file_path, "rb") as f:
                content_bytes = f.read()
            content_b64 = base64.b64encode(content_bytes).decode("utf-8")

            # Get current SHA. No -R needed if repo is in the URL.
            get_sha_cmd = [
                "gh", "api", f"repos/{self.config.github_repo}/contents/{path_in_repo}",
                "--jq", ".sha" 
                # Removed: "-R", self.config.github_repo 
            ]
            sha_result = self.subprocess_runner.run_command(get_sha_cmd, check=False)
            current_sha = sha_result.stdout.strip() if sha_result.returncode == 0 and sha_result.stdout.strip() != "null" else None

            source_repo_commit_message = f"Sync {Path(path_in_repo).name} from AUR build"
            if self.result.version:
                 source_repo_commit_message += f" (v{self.result.version})"
            # If no version, use the broader commit message from AUR commit
            elif commit_msg:
                 source_repo_commit_message = commit_msg


            update_fields = [
                "-f", f"message={source_repo_commit_message}",
                "-f", f"content={content_b64}",
            ]
            if current_sha:
                update_fields.extend(["-f", f"sha={current_sha}"])
            
            # Update command. No -R needed if repo is in the URL.
            update_cmd = [
                "gh", "api", "--method", "PUT",
                f"repos/{self.config.github_repo}/contents/{path_in_repo}",
            ] + update_fields
            # Removed: + ["-R", self.config.github_repo]
            
            self.subprocess_runner.run_command(update_cmd)
            self.logger.info(f"Successfully updated '{path_in_repo}' in source GitHub repository.")

        except subprocess.CalledProcessError as e:
            self.logger.error(f"Failed to update '{path_in_repo}' in GitHub repo. CMD: {e.cmd}, RC: {e.returncode}, Stderr: {e.stderr}")
            # Add more context to the error message stored in the result
            detailed_error = f"gh api update for {path_in_repo} failed. CMD: {shlex.join(e.cmd)}. Stderr: {e.stderr}"
            self.result.error_message = (self.result.error_message or "") + f"; {detailed_error}"
            raise 
        except FileNotFoundError:
            self.logger.error(f"Local file '{local_file_path}' not found for GitHub source update.")
            raise
        except Exception as e:
            self.logger.error(f"Unexpected error updating GitHub source file '{path_in_repo}': {e}", exc_info=self.config.debug)
            raise

    def _has_git_changes_to_commit(self) -> bool:
        # Ensure CWD is self.build_dir for git commands
        if Path.cwd() != self.build_dir:
            self.logger.warning(f"Git check called from {Path.cwd()}, expected {self.build_dir}. This might be an issue.")
            # For safety, let's not chdir here as it might hide issues elsewhere.
        
        result = self.subprocess_runner.run_command(["git", "status", "--porcelain"], check=False)
        if result.stdout and result.stdout.strip():
            self.logger.debug(f"Git porcelain status output:\n{result.stdout.strip()}")
            return True
        return False

    def cleanup(self):
        # self.build_dir is an absolute path like /home/builder/pkg_builds/build-pkg-unique
        if self.build_dir and self.build_dir.exists() and self.build_dir.is_relative_to(self.config.base_build_dir):
            self.logger.info(f"Cleaning up package build directory: {self.build_dir}")
            try:
                # Run as current user (builder). No sudo needed if builder owns base_build_dir and its contents.
                shutil.rmtree(self.build_dir)
            except Exception as e:
                self.logger.warning(f"Failed to clean up package build directory {self.build_dir}: {e}. Manual cleanup might be needed.")
        elif self.build_dir and self.build_dir.exists():
             self.logger.warning(f"Not cleaning up {self.build_dir} as it's not relative to base build dir {self.config.base_build_dir} or some other issue.")


    def run(self) -> Dict[str, Any]:
        original_cwd = Path.cwd() # Should be NVCHECKER_RUN_DIR
        self.logger.info(f"buildscript2.py started. Original CWD: {original_cwd}. HOME env: {os.environ.get('HOME')}")
        self.logger.info(f"Package: {self.config.package_name}, Build Mode: {self.config.build_mode}")
        self.logger.info(f"Using base build directory: {self.config.base_build_dir}")
        self.logger.info(f"Package specific build dir will be: {self.build_dir} (created now)")
        # build_dir is created in __init__

        try:
            # 1. Authenticate with GitHub (if needed, gh auth status checks this)
            #    Run this before changing directory, in case gh relies on CWD for some config.
            try:
                # Check auth status without input to avoid hanging if no token.
                # Errors here are not fatal, authenticate_github will try login.
                self.subprocess_runner.run_command(["gh", "auth", "status"], check=False) 
            except Exception: # Catch broader exceptions if gh not found etc.
                 pass # Let authenticate_github handle it.

            if "gh auth status" in self.subprocess_runner.run_command(["gh", "auth", "status"], check=False).stderr: # Crude check
                self.logger.info("GitHub token might not be pre-authenticated or gh has issues. Attempting login.")
                if not self.authenticate_github():
                    # self.result.error_message is set by authenticate_github
                    self.result.success = False
                    raise RuntimeError(self.result.error_message or "GitHub authentication failed critically.")

            # 2. Setup build environment (clone AUR repo, cd into it)
            self.setup_build_environment() # This changes CWD to self.build_dir (e.g., /home/builder/pkg_builds/build-pkg-xyz)

            # 3. Collect package files from workspace to build_dir
            self.collect_package_files() 

            # 4. Process dependencies (install them using paru)
            if not self.process_dependencies(): 
                self.result.success = False
                raise Exception(self.result.error_message or "Dependency processing failed")

            # 5. Check for version updates (nvchecker, updates PKGBUILD if new version)
            self.check_version_update() # Sets self.result.version, self.result.changes_detected
            if not self.result.version: # If version could not be determined at all
                self.logger.warning("Package version could not be determined. This might affect releases and commits.")
                # Not necessarily fatal, but good to note.

            # 6. Build package (makepkg, local install, create GH release)
            #    This also handles .SRCINFO generation if PKGBUILD changed.
            if not self.build_package(): 
                self.result.success = False
                raise Exception(self.result.error_message or "Package build failed")

            # 7. Process local source files listed in JSON (updates self.tracked_files for git commit)
            #    Run this *after* potential PKGBUILD changes (version, updpkgsums) and *before* commit.
            if not self.process_package_sources():
                self.result.success = False
                raise Exception(self.result.error_message or "Processing package sources from JSON failed")

            # 8. Commit and push to AUR, then sync back to source GitHub repo
            #    Only run if changes were detected (e.g. version update, .SRCINFO, updpkgsums)
            #    or if commit_and_push itself finds changes (e.g. manual file changes copied in)
            if self.result.changes_detected or self.config.build_mode in ["build", "test"]: # Force attempt if building/testing
                 if not self.commit_and_push():
                    # commit_and_push sets its own error message. If it returns False due to actual error:
                    if self.result.error_message and "failed" in self.result.error_message.lower():
                        self.result.success = False
                        raise Exception(self.result.error_message)
                    # If it returned False due to "no changes" but we expected changes, it's an anomaly.
                    # For now, trust its return for "no changes" vs actual failure.
            else:
                self.logger.info("No changes detected by earlier steps (like version update or PKGBUILD modification). Skipping AUR commit and push.")


            self.result.success = True # If all steps above passed or were handled.
            self.logger.info(f"Successfully processed package {self.config.package_name}")

        except Exception as e:
            self.logger.error(f"Exception during run for package {self.config.package_name}: {str(e)}", exc_info=self.config.debug)
            self.result.success = False # Mark as failure
            if not self.result.error_message: # Ensure an error message is present
                self.result.error_message = str(e)
        finally:
            # Change back to original CWD before cleanup, especially if cleanup needs relative paths from original CWD
            if Path.cwd() != original_cwd:
                os.chdir(original_cwd) 
                self.logger.debug(f"Restored current directory to {original_cwd}")
            self.cleanup() # Cleanup the package-specific build directory
            
        return asdict(self.result)


def main():
    parser = argparse.ArgumentParser(
        description="Build and publish Arch Linux packages"
    )
    parser.add_argument("--github-repo", required=True, help="GitHub repository (owner/repo)")
    parser.add_argument("--github-token", required=True, help="GitHub authentication token")
    parser.add_argument("--github-workspace", required=True, help="Path to GitHub workspace directory")
    parser.add_argument("--package-name", required=True, help="Name of the package to build")
    parser.add_argument("--depends-json", required=True, help="Path to JSON file containing package dependencies and sources metadata")
    parser.add_argument("--pkgbuild-path", required=True, help="Relative path within the workspace to the directory containing PKGBUILD and related files (e.g., category/packagename)")
    parser.add_argument("--commit-message", required=True, help="Base Git commit message for AUR updates")
    parser.add_argument(
        "--build-mode",
        choices=["nobuild", "build", "test"], 
        default="nobuild",
        help="Build mode: 'nobuild' (prepare, check version, commit AUR changes), 'build' (nobuild + build, create GH release), 'test' (nobuild + build, no GH release)",
    )
    parser.add_argument(
        "--artifacts-dir", default=None, help="Directory to store build artifacts for this package (e.g., logs). Path will be specific to the package."
    )
    parser.add_argument(
        "--base-build-dir", required=True, type=Path, help="Base directory where package-specific temporary build directories will be created."
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")

    args = parser.parse_args()

    # Configure logger based on debug flag from CLI (already set up to use stderr)
    if args.debug:
        logging.getLogger("arch_builder_script").setLevel(logging.DEBUG)
    else:
        logging.getLogger("arch_builder_script").setLevel(logging.INFO)
    
    # Convert string path to Path object for base_build_dir if not already
    config = BuildConfig(**vars(args))
    
    # Ensure HOME is correctly reported if script changes it internally (it shouldn't)
    logger.debug(f"Initial HOME from environment: {os.environ.get('HOME')}")


    builder = ArchPackageBuilder(config)
    result_dict = builder.run()

    # Print result as JSON to STDOUT
    try:
        print(json.dumps(result_dict, indent=2))
    except TypeError as e:
        # Fallback if result_dict is not serializable, print a basic error JSON
        err_json = json.dumps({
            "success": False,
            "package_name": args.package_name,
            "error_message": f"Failed to serialize result to JSON: {e}. Raw result: {result_dict}",
            "version": None,
            "built_packages": [],
            "changes_detected": False
        })
        print(err_json)
        logger.error(f"CRITICAL: Failed to serialize result_dict to JSON: {e}. Raw dict: {result_dict}")
    
    sys.exit(0 if result_dict.get("success", False) else 1)


if __name__ == "__main__":
    main()
