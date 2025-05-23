#!/usr/bin/env python3

import argparse
import json
import logging
import requests
import os
import subprocess
import sys
import re
import shlex # For command logging
from dataclasses import dataclass, asdict
from pathlib import Path
import base64
import shutil
import tempfile
from typing import List, Tuple, Dict, Optional, Any

# Setup logger early, but allow ArchPackageBuilder to customize further
logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
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
        self, cmd: List[str], check: bool = True, input_data: Optional[str] = None
    ) -> CommandResult:
        self.logger.debug(f"Running command: {shlex.join(cmd)}")

        try:
            process = subprocess.run(
                cmd, check=check, text=True, capture_output=True, input=input_data
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
    build_mode: str = ""  # Options: nobuild, build, test
    artifacts_dir: Optional[str] = None # Directory for build artifacts
    debug: bool = False


@dataclass
class BuildResult:
    success: bool
    package_name: str
    version: Optional[str] = None
    built_packages: List[str] = None # Filled with package file names
    error_message: Optional[str] = None
    changes_detected: bool = False


class ArchPackageBuilder:
    RELEASE_BODY = "To install, run: sudo pacman -U PACKAGENAME.pkg.tar.zst"
    TRACKED_FILES = ["PKGBUILD", ".SRCINFO", ".nvchecker.toml"]

    def __init__(self, config: BuildConfig):
        self.config = config
        self.logger = self._setup_logger() # Use instance logger
        self.build_dir = Path(tempfile.mkdtemp(prefix=f"build-{config.package_name}-"))
        self.tracked_files: List[str] = self.TRACKED_FILES.copy()
        self.result = BuildResult(success=True, package_name=config.package_name)
        self.subprocess_runner = SubprocessRunner(self.logger)
        
        self.artifacts_path: Optional[Path] = None
        if self.config.artifacts_dir:
            self.artifacts_path = Path(self.config.artifacts_dir)

    def _setup_logger(self) -> logging.Logger:
        # Use the global logger name, but configure its handler and level per instance
        instance_logger = logging.getLogger("arch_builder_script") # Same name as global
        # Remove existing handlers to avoid duplicate messages if script is run multiple times
        for handler in instance_logger.handlers[:]:
            instance_logger.removeHandler(handler)
            
        handler = logging.StreamHandler(sys.stderr) # MODIFIED LINE: Logs should go to stderr
        formatter = logging.Formatter("[%(levelname)s] %(name)s: %(message)s")
        handler.setFormatter(formatter)
        instance_logger.addHandler(handler)
        instance_logger.setLevel(logging.DEBUG if self.config.debug else logging.INFO)
        instance_logger.propagate = False # Avoid propagation to root logger if it has handlers
        return instance_logger

    def authenticate_github(self) -> bool:
        try:
            # Using input_data for token is generally safer than temp files
            # Ensure gh version supports this well.
            # Create a temporary file for the token as gh auth login --with-token expects it from stdin
            # but subprocess.run with input pipes it, which is fine.
            self.subprocess_runner.run_command(
                ["gh", "auth", "login", "--with-token"], input_data=self.config.github_token
            )
            self.subprocess_runner.run_command(["gh", "auth", "status"])
            return True
        except Exception as e:
            self.logger.error(f"GitHub authentication error: {str(e)}")
            return False

    def setup_build_environment(self):
        aur_repo = f"ssh://aur@aur.archlinux.org/{self.config.package_name}.git"
        self.subprocess_runner.run_command(
            ["git", "clone", aur_repo, str(self.build_dir)]
        )
        os.chdir(self.build_dir)
        self.logger.info(f"Changed directory to {self.build_dir}")


    def collect_package_files(self):
        workspace_source_path = Path(self.config.github_workspace) / self.config.pkgbuild_path
        self.logger.info(f"Collecting package files from {workspace_source_path} to {self.build_dir}")

        if not workspace_source_path.is_dir():
            self.logger.warning(f"Provided PKGBUILD path {workspace_source_path} is not a directory. Skipping file collection.")
            return

        for source_file_path in workspace_source_path.glob("**/*"):
            if source_file_path.is_file():
                relative_path = source_file_path.relative_to(workspace_source_path)
                destination_file_path = self.build_dir / relative_path
                try:
                    destination_file_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source_file_path, destination_file_path)
                    self.logger.debug(f"Copied {source_file_path} to {destination_file_path}")
                except Exception as e:
                    self.logger.warning(f"Failed to copy {source_file_path} to {destination_file_path}: {e}")


    def process_package_sources(self):
        workspace_path = Path(self.config.github_workspace) # Base workspace
        package_name = self.config.package_name
        json_file_path = Path(self.config.depends_json) # Should be absolute or resolvable

        if not json_file_path.is_file():
            self.logger.error(f"Depends JSON file not found: {json_file_path}")
            self.result.error_message = f"File not found: {json_file_path}"
            return False # Indicate failure

        try:
            with open(json_file_path, "r") as file:
                data = json.load(file)
        except json.JSONDecodeError as e:
            self.logger.error(f"Error decoding JSON from {json_file_path}: {e}")
            self.result.error_message = f"Error decoding JSON: {e}"
            return False

        self.logger.info(f"Loaded source/dependency JSON data for package '{package_name}'")

        if package_name not in data:
            self.logger.info(f"Package '{package_name}' not found in JSON data. No specific sources to process from JSON.")
            return True # Not an error, just no specific handling

        package_specific_data = data[package_name]
        sources_from_json = package_specific_data.get("sources", [])

        if not sources_from_json:
            self.logger.info(f"No 'sources' array found for package '{package_name}' in JSON. Only PKGBUILD defaults apply.")
            return True

        # Path to the directory containing the PKGBUILD and its associated files in the source repo
        pkgbuild_source_dir_in_workspace = Path(self.config.github_workspace) / self.config.pkgbuild_path

        for source_entry in sources_from_json:
            if self._is_url(source_entry):
                self.logger.debug(f"Source is a URL, handled by makepkg: {source_entry}")
            else:
                # This is a local file name, relative to the PKGBUILD's directory in the source repo
                source_file_in_workspace = pkgbuild_source_dir_in_workspace / source_entry
                if source_file_in_workspace.is_file():
                    # These files are copied by collect_package_files if they are under pkgbuild_path.
                    # Here, we just add them to tracked_files for git commit purposes.
                    if source_entry not in self.tracked_files:
                        self.tracked_files.append(source_entry)
                    self.logger.info(f"Local source file '{source_entry}' will be tracked.")
                else:
                    # This could be an issue if PKGBUILD expects it and it's not a URL
                    self.logger.warning(f"Local source file '{source_entry}' listed in JSON not found at {source_file_in_workspace}. PKGBUILD might fail.")
        return True


    def _is_url(self, source_string: str) -> bool:
        # Basic check for URL schemes or common Git source syntaxes
        return "://" in source_string or source_string.startswith("git+")


    def process_dependencies(self) -> bool:
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

        self.logger.debug(f"Loaded JSON data for dependencies: {data}")

        if package_name not in data:
            self.logger.info(f"Package '{package_name}' not found in JSON. Assuming no specific dependencies to install.")
            return True

        package_data = data[package_name]
        combined_dependencies = list(set(
            package_data.get("depends", []) +
            package_data.get("makedepends", []) +
            package_data.get("checkdepends", [])
        ))
        combined_dependencies = [dep for dep in combined_dependencies if dep and not dep.startswith((" হৃ", ">", "<", "="))] # Filter out version constraints for now
        
        if not combined_dependencies:
            self.logger.info("No dependencies listed for this package in JSON.")
            return True

        self.logger.info(f"Identified dependencies for {package_name}: {combined_dependencies}")
        
        resolved_packages_to_install = []
        for dep in combined_dependencies:
            # Remove version constraints for paru resolution (e.g., 'glibc>=2.32')
            dep_name_only = re.split(r'[<>=]', dep)[0].strip()
            
            check_cmd = ["paru", "-Ssx", f"^{dep_name_only}$"] # Exact match for package name
            result = self.subprocess_runner.run_command(check_cmd, check=False)

            if result.returncode == 0 and result.stdout.strip(): # Found in repos or AUR
                self.logger.debug(f"Dependency '{dep_name_only}' found by paru.")
                resolved_packages_to_install.append(dep_name_only)
            else: # Check if it's a provides
                self.logger.debug(f"Dependency '{dep_name_only}' not directly found. Checking providers via AUR RPC for '{dep}'.")
                try:
                    # Use original dep string (with potential version for provides search, though RPC might ignore it)
                    # Or better, use dep_name_only for provides search too, as RPC 'search' by 'provides' is name-based.
                    response = requests.get(
                        f"https://aur.archlinux.org/rpc/v5/search/{dep_name_only}?by=provides",
                        timeout=10, # Increased timeout
                    )
                    response.raise_for_status()
                    aur_data = response.json()
                    if aur_data.get("results"):
                        # Simplistic: take the first provider. Could be more sophisticated.
                        provider_name = aur_data["results"][0].get("Name")
                        if provider_name:
                            self.logger.info(f"Found AUR provider '{provider_name}' for '{dep_name_only}'.")
                            resolved_packages_to_install.append(provider_name)
                        else:
                            self.logger.warning(f"No AUR provider name found for '{dep_name_only}' despite results. Package might be missing.")
                    else:
                        self.logger.warning(f"No AUR provider found for dependency: {dep_name_only} (original: {dep}). It might be a repo package not yet synced or a typo.")
                except requests.RequestException as e:
                    self.logger.warning(f"Error querying AUR for provider of '{dep_name_only}': {e}")
                except json.JSONDecodeError as e:
                    self.logger.warning(f"Error decoding AUR RPC response for '{dep_name_only}': {e}")


        resolved_packages_to_install = sorted(list(set(resolved_packages_to_install)))
        if not resolved_packages_to_install:
            self.logger.info("No dependencies to install after resolution.")
            return True
            
        self.logger.info(f"Final list of dependencies to attempt installing: {resolved_packages_to_install}")
        install_cmd = [
            "paru", "-S", "--norebuild", "--noconfirm", "--needed",
        ] + resolved_packages_to_install
        
        install_result = self.subprocess_runner.run_command(install_cmd, check=False)
        if install_result.returncode == 0:
            self.logger.info(f"Dependencies for package '{package_name}' installed successfully.")
            return True
        else:
            self.logger.error(f"Dependency installation failed for {package_name}. Paru stderr:\n{install_result.stderr}")
            self.result.error_message = "Dependency installation failed."
            return False


    def check_version_update(self) -> Optional[str]:
        nvchecker_config_path = self.build_dir / ".nvchecker.toml"
        if not nvchecker_config_path.is_file():
            self.logger.info(".nvchecker.toml not found, skipping version check.")
            return None

        # Keyfile path assumes it's available in the builder's home, set up by the workflow
        # This path should align with what's in checkupdates.yml
        keyfile_path = Path.home() / "nvchecker/keyfile.toml" # Standardized path from workflow
        if not keyfile_path.is_file():
            self.logger.warning(f"NVChecker keyfile not found at {keyfile_path}. Version check might fail for sources requiring auth.")
        
        try:
            cmd = [
                "nvchecker",
                "-c", str(nvchecker_config_path), # Use the one in build_dir
                "--logger", "json",
            ]
            if keyfile_path.is_file(): # Only add -k if keyfile exists
                 cmd.extend(["-k", str(keyfile_path)])

            result = self.subprocess_runner.run_command(cmd)
            self.logger.debug(f"Raw NVChecker output: {result.stdout}")

            new_version = None
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line: continue
                try:
                    entry = json.loads(line)
                    if isinstance(entry, dict) and entry.get("event") == "updated": # nvchecker uses 'event':'updated'
                        new_version_candidate = entry.get("version")
                        # Additional check: ensure it's for the current package.
                        # nvchecker output format might not directly give package name per line
                        # in this context with single package .toml. So, first 'updated' version is taken.
                        if new_version_candidate:
                            new_version = new_version_candidate
                            self.logger.info(f"NVChecker found new version: {new_version}")
                            self._update_pkgbuild_version(new_version)
                            self.result.version = new_version
                            break 
                except json.JSONDecodeError as e:
                    self.logger.warning(f"Skipping invalid JSON line from NVChecker: {line}. Error: {e}")
            
            if not new_version:
                self.logger.info("NVChecker did not report any new version (no 'updated' event with version).")
            return new_version

        except subprocess.CalledProcessError as e:
            # nvchecker exits non-zero if no updates are found, or on error.
            # We need to distinguish. If stdout is empty, it's likely an error.
            # If stdout has "event":"error", it's an error.
            # If stdout only has "old_ver", "ver", but no "updated", it's no update.
            # The current logic relies on finding "event":"updated".
            self.logger.warning(f"NVChecker command finished. It might have found no updates or encountered an error. Check its output if issues persist.")
        except Exception as e:
            self.logger.error(f"Version check using NVChecker failed: {e}")
        return None


    def _update_pkgbuild_version(self, new_version: str):
        pkgbuild_path = self.build_dir / "PKGBUILD"
        if not pkgbuild_path.is_file():
            self.logger.error("PKGBUILD not found in build directory for version update.")
            raise FileNotFoundError("PKGBUILD not found for version update")

        content = pkgbuild_path.read_text()
        
        old_pkgver_match = re.search(r"^\s*pkgver=([^\s#]+)", content, re.MULTILINE)
        if not old_pkgver_match:
            self.logger.error("pkgver not found in PKGBUILD.")
            raise ValueError("pkgver not found in PKGBUILD")
        old_version = old_pkgver_match.group(1)

        if old_version == new_version:
            self.logger.info(f"PKGBUILD version ({old_version}) already matches new version ({new_version}). No update needed.")
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
            r"\g<1>1",
            content,
            count=1,
            flags=re.MULTILINE,
        )
        pkgbuild_path.write_text(content)
        self.result.changes_detected = True # Mark that PKGBUILD changed


    def build_package(self) -> bool:
        if self.config.build_mode not in ["build", "test"]:
            self.logger.info(f"Build mode is '{self.config.build_mode}', skipping actual package build steps.")
            return True

        try:
            self.logger.info("Updating checksums in PKGBUILD (updpkgsums)...")
            self.subprocess_runner.run_command(["updpkgsums"])
            
            self.logger.info("Regenerating .SRCINFO file...")
            srcinfo_result = self.subprocess_runner.run_command(["makepkg", "--printsrcinfo"])
            (self.build_dir / ".SRCINFO").write_text(srcinfo_result.stdout)
            self.result.changes_detected = True # .SRCINFO changed or updpkgsums changed PKGBUILD

            self.logger.info("Starting package build (makepkg -L -s --noconfirm)...")
            self.subprocess_runner.run_command(["makepkg", "-L", "-s", "--noconfirm"]) # Added -L

            built_package_files = sorted(self.build_dir.glob("*.pkg.tar.zst"))
            if not built_package_files:
                self.logger.error("No packages were built (no .pkg.tar.zst files found).")
                raise Exception("No packages built")
            
            self.result.built_packages = [p.name for p in built_package_files]
            self.logger.info(f"Successfully built: {', '.join(self.result.built_packages)}")

            # Install the built package(s) locally for testing or as a dependency for others
            self.logger.info(f"Installing built package(s) locally: {', '.join(map(str, built_package_files))}")
            self.subprocess_runner.run_command(
                ["sudo", "pacman", "--noconfirm", "-U"] + [str(p) for p in built_package_files]
            )

            # Copy build logs to artifacts directory if specified
            if self.artifacts_path:
                self.artifacts_path.mkdir(parents=True, exist_ok=True)
                for log_file in self.build_dir.glob("*.log"):
                    try:
                        # Prepend package name for uniqueness in a shared artifact dir
                        dest_log_file = self.artifacts_path / f"{self.config.package_name}-{log_file.name}"
                        shutil.copy2(log_file, dest_log_file)
                        self.logger.info(f"Copied log file {log_file.name} to {dest_log_file}")
                    except Exception as e:
                        self.logger.warning(f"Failed to copy log file {log_file.name}: {e}")
            
            if self.config.build_mode == "build":
                self._create_release(built_package_files)

            return True

        except Exception as e:
            self.logger.error(f"Build process failed: {e}", exc_info=self.config.debug)
            self.result.error_message = f"Build failed: {str(e)}"
            return False

    def _create_release(self, package_files: List[Path]):
        if not self.result.version:
            self.logger.warning("No version information available (self.result.version is None). Skipping GitHub release creation.")
            self.logger.warning("This usually means nvchecker did not find a new version or was not run.")
            # Decide if this is an error or acceptable. For now, just skip.
            return

        # Use package_name and version for the tag and title for clarity
        tag_name = f"{self.config.package_name}-{self.result.version}"
        release_title = f"{self.config.package_name} {self.result.version}"
        
        self.logger.info(f"Creating GitHub release with tag '{tag_name}' and title '{release_title}'.")

        try:
            # Create release
            # Check if release already exists for this tag
            check_release_cmd = ["gh", "release", "view", tag_name, "-R", self.config.github_repo]
            release_exists_result = self.subprocess_runner.run_command(check_release_cmd, check=False)

            if release_exists_result.returncode == 0:
                self.logger.info(f"Release for tag '{tag_name}' already exists. Will attempt to upload/clobber assets.")
            else:
                self.logger.info(f"Creating new release for tag '{tag_name}'.")
                create_release_cmd = [
                    "gh", "release", "create", tag_name,
                    "--title", release_title,
                    "--notes", self.RELEASE_BODY.replace("PACKAGENAME.pkg.tar.zst", self.result.built_packages[0] if self.result.built_packages else "PACKAGE.pkg.tar.zst"),
                    "-R", self.config.github_repo,
                ]
                self.subprocess_runner.run_command(create_release_cmd) # check=True by default

            # Upload packages
            for pkg_file in package_files:
                self.logger.info(f"Uploading {pkg_file.name} to release '{tag_name}'.")
                upload_cmd = [
                    "gh", "release", "upload", tag_name, str(pkg_file),
                    "--clobber", # Overwrite if asset already exists
                    "-R", self.config.github_repo,
                ]
                self.subprocess_runner.run_command(upload_cmd)
            self.logger.info("All built packages uploaded to GitHub release.")

        except subprocess.CalledProcessError as e:
            self.logger.error(f"Failed to create or upload to GitHub release '{tag_name}': {e}")
            # Optionally append to self.result.error_message or handle more gracefully
            self.result.error_message = (self.result.error_message or "") + f"; GitHub release failed for {tag_name}"
        except Exception as e: # Catch other potential errors
            self.logger.error(f"An unexpected error occurred during GitHub release for '{tag_name}': {e}", exc_info=self.config.debug)
            self.result.error_message = (self.result.error_message or "") + f"; Unexpected error during GitHub release for {tag_name}"


    def commit_and_push(self) -> bool:
        if not self.tracked_files:
            self.logger.error("There are no tracked files defined. This should not happen.")
            return False

        self.logger.info(f"Attempting to commit and push changes. Tracked files: {self.tracked_files}")

        # Ensure we are in the build directory (git repo)
        if Path.cwd() != self.build_dir:
            self.logger.warning(f"Current directory {Path.cwd()} is not build_dir {self.build_dir}. Changing to build_dir.")
            os.chdir(self.build_dir)

        # Verify tracked files exist before adding
        existing_tracked_files = []
        for file_name in self.tracked_files:
            file_path = self.build_dir / file_name
            if file_path.is_file():
                existing_tracked_files.append(file_name)
            else:
                self.logger.warning(f"Tracked file '{file_name}' not found at {file_path}. It will not be added to git.")
        
        if not existing_tracked_files:
            self.logger.info("No existing tracked files found to add to git.")
            # This might be okay if no changes were expected or made.
            # Check if changes were detected by other means (e.g. version update)
            # For now, proceed to check git status.
        else:
            self.subprocess_runner.run_command(["git", "add"] + existing_tracked_files)


        if not self._has_git_changes_to_commit():
            self.logger.info("No git changes to commit to AUR.")
            # If PKGBUILD was changed by nvchecker but git status is clean, it implies it was already committed.
            # self.result.changes_detected might still be true from version update or .SRCINFO.
            # This is fine.
            return True # No failure, just nothing to push to AUR.

        self.logger.info("Git changes detected. Proceeding with commit and push to AUR.")
        
        commit_version_suffix = f" (v{self.result.version})" if self.result.version else ""
        commit_msg = f"{self.config.commit_message}{commit_version_suffix}"
        
        try:
            self.subprocess_runner.run_command(["git", "commit", "-m", commit_msg])
            self.subprocess_runner.run_command(["git", "push", "origin", "master"]) # Assuming master is the target branch for AUR
            self.result.changes_detected = True # Explicitly confirm changes were pushed
            self.logger.info("Changes successfully committed and pushed to AUR.")

            # Update files in the source GitHub repository
            # This syncs changes made in build_dir (like version bump, .SRCINFO) back to the main repo.
            for file_name in existing_tracked_files: # Only update files that were actually part of the commit
                file_path_in_build_dir = self.build_dir / file_name
                # self.config.pkgbuild_path is path like "maintain/build/mypackage"
                # file_name is like "PKGBUILD" or ".SRCINFO"
                # So, target path in repo is config.pkgbuild_path / file_name
                path_in_repo = Path(self.config.pkgbuild_path) / file_name
                self._update_github_file(str(path_in_repo), file_path_in_build_dir)
            
            return True

        except subprocess.CalledProcessError as e:
            self.logger.error(f"Git operation (commit/push to AUR or update to source repo) failed: {e}")
            self.result.error_message = (self.result.error_message or "") + "; Git push to AUR or update to source repo failed."
            return False
        except Exception as e:
            self.logger.error(f"Unexpected error during git operations or source repo update: {e}", exc_info=self.config.debug)
            self.result.error_message = (self.result.error_message or "") + "; Unexpected error in git/source repo update."
            return False


    def _update_github_file(self, path_in_repo: str, local_file_path: Path):
        """Updates a file in the GitHub repository using the gh CLI."""
        self.logger.info(f"Updating '{path_in_repo}' in GitHub repo '{self.config.github_repo}' from local file '{local_file_path}'.")
        try:
            with open(local_file_path, "rb") as f:
                content_bytes = f.read()
            content_b64 = base64.b64encode(content_bytes).decode("utf-8")

            # Get current SHA of the file
            # Path in repo needs to be relative to repo root.
            get_sha_cmd = [
                "gh", "api", f"repos/{self.config.github_repo}/contents/{path_in_repo}",
                "--jq", ".sha", "-R", self.config.github_repo
            ]
            sha_result = self.subprocess_runner.run_command(get_sha_cmd, check=False)
            current_sha = sha_result.stdout.strip() if sha_result.returncode == 0 else None

            commit_message = f"Auto update: Sync {Path(path_in_repo).name}"
            if self.result.version:
                 commit_message += f" to v{self.result.version}"

            update_fields = [
                "-f", f"message={commit_message}",
                "-f", f"content={content_b64}",
            ]
            if current_sha and current_sha != "null": # "null" if file not found or other issues
                update_fields.extend(["-f", f"sha={current_sha}"])
            
            update_cmd = [
                "gh", "api", "--method", "PUT",
                f"repos/{self.config.github_repo}/contents/{path_in_repo}",
            ] + update_fields + ["-R", self.config.github_repo]
            
            self.subprocess_runner.run_command(update_cmd)
            self.logger.info(f"Successfully updated '{path_in_repo}' in GitHub repository.")

        except subprocess.CalledProcessError as e:
            # gh api might return non-zero for various reasons, stderr has details
            self.logger.error(f"Failed to update '{path_in_repo}' in GitHub repo. Error: {e.stderr}")
            # Propagate this as part of a larger failure if needed, or log and continue
            raise # Re-raise to be caught by commit_and_push
        except FileNotFoundError:
            self.logger.error(f"Local file '{local_file_path}' not found for GitHub update.")
            raise
        except Exception as e:
            self.logger.error(f"Unexpected error updating GitHub file '{path_in_repo}': {e}", exc_info=self.config.debug)
            raise

    def _has_git_changes_to_commit(self) -> bool:
        # Check git status --porcelain. If it has output, there are changes.
        result = self.subprocess_runner.run_command(["git", "status", "--porcelain"], check=False)
        return bool(result.stdout.strip())

    def cleanup(self):
        if self.build_dir.exists():
            self.logger.info(f"Cleaning up build directory: {self.build_dir}")
            try:
                # shutil.rmtree might fail if files are owned by root (e.g. after sudo pacman)
                # Using sudo rm -rf is more robust in a container context where builder has sudo
                subprocess.run(["sudo", "rm", "-rf", str(self.build_dir)], check=True, text=True, capture_output=True)
            except subprocess.CalledProcessError as e:
                self.logger.warning(f"Failed to clean up build directory using sudo rm -rf: {e.stderr}")
            except Exception as e:
                self.logger.warning(f"Unexpected error during cleanup of {self.build_dir}: {e}")


    def run(self) -> Dict[str, Any]:
        original_cwd = Path.cwd()
        try:
            try:
                self.subprocess_runner.run_command(["gh", "auth", "status"])
            except subprocess.CalledProcessError:
                self.logger.info("GitHub token not pre-authenticated or expired. Attempting login...")
                if not self.authenticate_github():
                    raise RuntimeError("GitHub authentication failed") # Critical failure

            self.setup_build_environment() # This changes cwd to self.build_dir
            self.collect_package_files() # Copies files from workspace/pkgbuild_path to build_dir

            if not self.process_dependencies(): # Installs dependencies using paru
                # Error message should be set by process_dependencies
                raise Exception(self.result.error_message or "Dependency processing failed")

            self.check_version_update() # Updates PKGBUILD if new version found, sets self.result.version

            if not self.build_package(): # Builds package, installs locally, creates release
                # Error message set by build_package
                raise Exception(self.result.error_message or "Package build failed")

            # process_package_sources updates self.tracked_files based on 'sources' in JSON
            # It should run *before* commit_and_push to ensure all local files are tracked.
            # It should run *after* collect_package_files ensures those files are in build_dir.
            if not self.process_package_sources():
                 raise Exception(self.result.error_message or "Processing package sources from JSON failed")

            if not self.commit_and_push(): # Commits to AUR, updates source GitHub repo
                # Error message might be set by commit_and_push
                # If no changes, it returns True. If actual error, False.
                if not self.result.error_message: # If commit_and_push failed but didn't set a specific error
                    self.result.error_message = "Commit and push operations failed."
                # Do not raise here if it was just "no changes".
                # commit_and_push failing with an actual error should be the problem.
                # The current logic of commit_and_push returning False on error is fine.
                # Let's check if an error message was set to distinguish.
                if self.result.error_message and "failed" in self.result.error_message.lower(): # Heuristic
                     raise Exception(self.result.error_message)


            self.result.success = True # If we reached here, it's a success.
            self.logger.info(f"Successfully processed package {self.config.package_name}")

        except Exception as e:
            self.logger.error(f"Unhandled exception in run: {str(e)}", exc_info=self.config.debug)
            self.result.success = False
            if not self.result.error_message: # Ensure an error message is present
                self.result.error_message = str(e)
        finally:
            if Path.cwd() != original_cwd:
                os.chdir(original_cwd) # Change back to original CWD
                self.logger.debug(f"Restored current directory to {original_cwd}")
            self.cleanup()
            
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
        choices=["nobuild", "build", "test"], # Changed "none" to "nobuild" for clarity
        default="nobuild",
        help="Build mode: 'nobuild' (prepare, check version, commit AUR changes), 'build' (nobuild + build, create GH release), 'test' (nobuild + build, no GH release)",
    )
    parser.add_argument(
        "--artifacts-dir", default=None, help="Directory to store build artifacts (e.g., logs). Relative to current CWD or absolute."
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")

    args = parser.parse_args()

    # Configure logger based on debug flag from CLI
    if args.debug:
        logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.INFO)

    config = BuildConfig(**vars(args))
    builder = ArchPackageBuilder(config)
    result_dict = builder.run()

    # Print result as JSON to stdout
    print(json.dumps(result_dict, indent=2))
    
    sys.exit(0 if result_dict["success"] else 1)


if __name__ == "__main__":
    main()
