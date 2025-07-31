#!/usr/bin/env python3

import argparse
import json
import logging
import os
import subprocess
import sys
import re
import shlex
from dataclasses import dataclass, asdict, field
from pathlib import Path
import base64
import shutil
from typing import List, Dict, Optional, Any
import pyalpm
from requests.auth import parse_dict_header

# Setup logger early, but allow ArchPackageBuilder to customize further
# Logs will be configured to go to stderr by ArchPackageBuilder instance
logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
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
        self,
        cmd: List[str],
        check: bool = True,
        input_data: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        shell: bool = False,
    ) -> CommandResult:
        if shell and len(cmd) == 1:
            # For shell commands, use the single string as the command
            cmd_for_logging = cmd[0]
            cmd_for_subprocess = cmd[0]
        else:
            cmd_for_logging = shlex.join(cmd)
            cmd_for_subprocess = cmd

        self.logger.debug(f"Running command: {cmd_for_logging}")
        # Merge with current environment if new env is provided
        current_env = os.environ.copy()
        if env:
            current_env.update(env)

        try:
            process = subprocess.run(
                cmd_for_subprocess,
                check=check,
                text=True,
                capture_output=True,
                input=input_data,
                env=current_env,
                shell=shell,
            )
            return CommandResult(
                command=cmd,
                returncode=process.returncode,
                stdout=process.stdout,
                stderr=process.stderr,
            )
        except subprocess.CalledProcessError as e:
            if shell and len(cmd) == 1:
                error_msg = (
                    f"Shell command '{cmd[0]}' failed with return code {e.returncode}\n"
                )
            else:
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
    package_name: str  # This is pkgbase
    package_update_info_json: str  # JSON string of PackageUpdateInfo
    pkgbuild_path: str
    commit_message: str
    base_build_dir: Path
    build_mode: str = "nobuild"
    artifacts_dir: Optional[str] = None
    debug: bool = False


@dataclass
class PackageUpdateInfo:
    pkgbase: str
    pkgname: str
    pkgver: Optional[str] = None
    pkgrel: Optional[str] = None
    aur_pkgver: Optional[str] = None
    aur_pkgrel: Optional[str] = None
    nvchecker_pkgver: Optional[str] = None
    sources: List[str] = field(default_factory=list)
    depends: List[str] = field(default_factory=list)
    makedepends: List[str] = field(default_factory=list)
    checkdepends: List[str] = field(default_factory=list)
    is_update: bool = False
    is_update_candidate: bool = False
    update_source: Optional[str] = None
    new_version_for_update: Optional[str] = None
    local_is_ahead: bool = False
    validpgpkeys: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    pkgfile: Optional[str] = None
    nvchecker_event: Optional[str] = None
    nvchecker_raw_log: Optional[Dict[str, Any]] = None
    comparison_details: Optional[Dict[str, str]] = field(default_factory=dict)


@dataclass
class BuildResult:
    success: bool
    package_name: str
    version: Optional[str] = None
    built_packages: List[str] = field(default_factory=list)
    error_message: Optional[str] = None
    changes_detected: bool = False
    action_taken: Optional[str] = (
        None  # e.g., "synced_down_from_aur", "updated_via_nvchecker_and_pushed"
    )


class ArchPackageBuilder:
    RELEASE_BODY = (
        "To install - first load GPG keys to the pacman keyring:\n"
        "```bash\n"
        "  sudo pacman-key --recv-keys E6CA536875E45798 --keyserver keyserver.ubuntu.com\n"
        "  sudo pacman-key --lsign-key E6CA536875E45798\n"
        "```\n"
        "And then:\n"
        "```bash\n"
    )
    URL = "https://github.com/envolution/aur/releases/download"
    TRACKED_FILES = ["PKGBUILD", ".SRCINFO", ".nvchecker.toml"]

    def __init__(self, config: BuildConfig):
        self.config = config
        self.logger = self._setup_logger()
        self.package_update_info: PackageUpdateInfo = self._parse_package_update_info()

        self.config.base_build_dir.mkdir(parents=True, exist_ok=True)
        unique_suffix = os.urandom(4).hex()
        self.build_dir = (
            self.config.base_build_dir / f"build-{config.package_name}-{unique_suffix}"
        )
        try:
            self.build_dir.mkdir(parents=True, exist_ok=True)
            self.logger.info(f"Created package build directory: {self.build_dir}")
        except Exception as e:
            self.logger.error(
                f"Failed to create package build directory {self.build_dir}: {e}"
            )
            raise RuntimeError(f"Failed to create package build directory: {e}") from e

        self.tracked_files: List[str] = self.TRACKED_FILES.copy()
        # Initialize result with package_name from config, as package_update_info might not have it if JSON is malformed.
        self.result = BuildResult(success=True, package_name=config.package_name)
        self.subprocess_runner = SubprocessRunner(self.logger)

        self.artifacts_path: Optional[Path] = None
        if self.config.artifacts_dir:
            self.artifacts_path = Path(self.config.artifacts_dir)
            try:
                self.artifacts_path.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                self.logger.warning(
                    f"Could not create artifacts directory {self.artifacts_path}: {e}. Artifacts may not be saved."
                )
                self.artifacts_path = None

    def _setup_logger(self) -> logging.Logger:
        instance_logger = logging.getLogger("arch_builder_script")
        for handler in instance_logger.handlers[:]:
            instance_logger.removeHandler(handler)
        handler = logging.StreamHandler(sys.stderr)
        formatter = logging.Formatter("[%(levelname)s] %(name)s: %(message)s")
        handler.setFormatter(formatter)
        instance_logger.addHandler(handler)
        instance_logger.setLevel(logging.DEBUG if self.config.debug else logging.INFO)
        instance_logger.propagate = False
        return instance_logger

    def _parse_package_update_info(self) -> PackageUpdateInfo:
        try:
            data = json.loads(self.config.package_update_info_json)

            # Ensure pkgbase is set if missing, using config.package_name as a fallback.
            if "pkgbase" not in data or not data.get(
                "pkgbase"
            ):  # Use data, and data.get for safety
                data["pkgbase"] = self.config.package_name
            return PackageUpdateInfo(**data)  # Use data

        except (json.JSONDecodeError, TypeError) as e:
            self.logger.error(
                f"Failed to parse --package-update-info-json: {e}. JSON string: '{self.config.package_update_info_json[:200]}...'"
            )
            # Return a default/empty PackageUpdateInfo if parsing fails, to allow __init__ to complete.
            # The run() method should then check for essential fields.
            # Or, re-raise to make it a critical failure immediately. For now, let's re-raise.
            raise ValueError(f"Invalid package_update_info_json: {e}") from e

    def _sanitize_filename(self, filename: str) -> str:
        """
        Removes characters that are invalid in GitHub Actions artifact names.
        Invalid characters: " : < > | * ? \r \n
        """
        # Define the set of invalid characters in a regex character class
        invalid_chars = r'["\:<>|*?\r\n]'
        # Replace any found invalid character with an underscore
        sanitized_name = re.sub(invalid_chars, "_", filename)
        return sanitized_name

    def _import_pgp_keys(self):
        """Imports PGP keys listed in the PKGBUILD's validpgpkeys array."""
        keys_to_import = self.package_update_info.validpgpkeys
        if not keys_to_import:
            self.logger.info(
                "No 'validpgpkeys' found for this package. Skipping PGP key import."
            )
            return

        self.logger.info(
            f"Found {len(keys_to_import)} PGP key(s) to import: {', '.join(keys_to_import)}"
        )
        for key in keys_to_import:
            # Using a keyserver pool for better reliability
            cmd = [
                "gpg",
                "--keyserver",
                "hkps://keyserver.ubuntu.com",
                "--recv-keys",
                key,
            ]
            self.logger.debug(f"Importing PGP key: {key}")
            result = self.subprocess_runner.run_command(cmd, check=False)
            if result.returncode != 0:
                self.logger.warning(
                    f"Failed to import PGP key '{key}'. Return code: {result.returncode}. Stderr: {result.stderr.strip()}"
                )
                # This is a warning, not a critical failure. makepkg will fail later if the key is truly needed and absent.
            else:
                self.logger.info(f"Successfully received PGP key '{key}'.")

    def authenticate_github(self) -> bool:
        try:
            self.subprocess_runner.run_command(
                ["gh", "auth", "login", "--with-token"],
                input_data=self.config.github_token,
            )
            self.subprocess_runner.run_command(["gh", "auth", "status"])
            return True
        except Exception as e:
            self.logger.error(f"GitHub authentication error: {str(e)}")
            self.result.error_message = f"GitHub authentication failed: {str(e)}"
            return False

    def setup_build_environment(self):
        aur_repo = f"ssh://aur@aur.archlinux.org/{self.config.package_name}.git"
        self.subprocess_runner.run_command(
            ["git", "clone", aur_repo, str(self.build_dir)]
        )
        os.chdir(self.build_dir)
        self.logger.info(f"Changed directory to {self.build_dir}")

    def collect_package_files(self):
        workspace_source_path = (
            Path(self.config.github_workspace) / self.config.pkgbuild_path
        )
        self.logger.info(
            f"Collecting package files from {workspace_source_path} to {self.build_dir}"
        )

        if not workspace_source_path.is_dir():
            self.logger.warning(
                f"Provided PKGBUILD path {workspace_source_path} is not a directory. Skipping file collection."
            )
            return
        try:
            shutil.copytree(
                workspace_source_path, self.build_dir, dirs_exist_ok=True, symlinks=True
            )
            self.logger.info(
                f"Copied directory tree from {workspace_source_path} to {self.build_dir}"
            )
        except Exception as e:
            self.logger.error(
                f"Error copying files from {workspace_source_path} to {self.build_dir}: {e}"
            )
            self.result.error_message = f"Failed to copy package files: {e}"
            raise

    def process_package_sources(self):
        # This function primarily ensures local files mentioned in PKGBUILD's 'source' array are tracked by git
        # It assumes files are already in self.build_dir (copied by collect_package_files)
        # Sources are taken from self.package_update_info.sources

        self.logger.info(
            f"Processing package sources for {self.config.package_name} based on PackageUpdateInfo."
        )

        sources_from_info = self.package_update_info.sources
        if not sources_from_info:
            self.logger.info(
                f"No 'sources' array found in PackageUpdateInfo for '{self.config.package_name}'. Only PKGBUILD defaults apply for source tracking."
            )
            return True  # Nothing to process from sources array

        for source_entry_raw in sources_from_info:
            # source entry can be "filename" or "filename::url"
            source_filename = source_entry_raw.split("::")[0]
            if self._is_url(source_entry_raw):  # Handles "filename::url" and "url"
                self.logger.debug(
                    f"Source is a URL, downloaded by makepkg: {source_entry_raw}"
                )
            else:
                # This is a local file name, should exist in self.build_dir
                source_file_in_build_dir = self.build_dir / source_filename
                if source_file_in_build_dir.is_file():
                    if source_filename not in self.tracked_files:
                        self.tracked_files.append(source_filename)
                    self.logger.info(
                        f"Local source file '{source_filename}' will be tracked by git."
                    )
                else:
                    self.logger.warning(
                        f"Local source file '{source_filename}' (from PackageUpdateInfo) not found at {source_file_in_build_dir}. PKGBUILD might fail or it might be generated."
                    )
        return True

    def _is_url(self, source_string: str) -> bool:
        parts = source_string.split("::")
        url_candidate = parts[-1]
        return "://" in url_candidate or url_candidate.startswith("git+")

    def _get_current_pkgbuild_version(self) -> Optional[str]:
        pkgbuild_path = self.build_dir / "PKGBUILD"
        if not pkgbuild_path.is_file():
            self.logger.error(
                "PKGBUILD not found in build directory for version retrieval."
            )
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
            self.logger.error(
                "PKGBUILD not found in build directory for version update."
            )
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
            self.logger.info(
                f"PKGBUILD version ({old_version}) already matches new version ({new_version}). No update needed for pkgver."
            )
            return

        self.logger.info(
            f"Updating PKGBUILD: pkgver {old_version} -> {new_version}, pkgrel -> 1"
        )
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
        self.result.changes_detected = True

    # In class ArchPackageBuilder:

    def _get_installed_packages(self) -> set:
        """Returns a set of all currently installed package names using pyalpm."""
        try:
            # Open handle to the pacman DB
            handle = pyalpm.Handle("/", "/var/lib/pacman")
            local_db = handle.get_localdb()
            # Return a set of package names for efficient comparison
            return {pkg.name for pkg in local_db.pkgcache}
        except Exception as e:
            self.logger.warning(
                f"Could not get list of installed packages via pyalpm: {e}. Package cleanup may not work correctly."
            )
            return set()

    def _cleanup_installed_packages(self, initial_packages: set):
        """Removes packages that were installed during the build process."""
        if not initial_packages:
            self.logger.warning(
                "Initial package list was empty, skipping cleanup of installed packages."
            )
            return

        self.logger.info("Starting cleanup of packages installed during this build.")
        current_packages = self._get_installed_packages()

        # Use set difference to find newly installed packages
        newly_installed = list(current_packages - initial_packages)

        if not newly_installed:
            self.logger.info(
                "No new packages were installed during the build. Nothing to clean up."
            )
            return

        self.logger.info(
            f"Found {len(newly_installed)} packages to remove: {', '.join(newly_installed)}"
        )

        try:
            # This command requires sudo privileges. It is assumed the script
            # runs in an environment (like a Docker container) where this is possible without a password.
            # Using -Rns to remove dependencies that are no longer required.
            # Using --noconfirm is essential for automation.
            cleanup_cmd = ["sudo", "pacman", "-Rns", "--noconfirm"] + newly_installed
            result = self.subprocess_runner.run_command(cleanup_cmd, check=True)
            self.logger.info(
                f"Successfully removed newly installed packages. Stdout: {result.stdout.strip()}"
            )
        except subprocess.CalledProcessError as e:
            self.logger.error(
                f"Failed to remove newly installed packages. Command failed with code {e.returncode}."
            )
            self.logger.error(f"Stderr: {e.stderr.strip()}")
        except Exception as e:
            # Catch other potential errors from the subprocess runner
            self.logger.error(
                f"An unexpected error occurred during package cleanup: {e}"
            )

    def _collect_build_artifacts(self):
        if not self.artifacts_path:
            self.logger.debug(
                "No artifacts directory configured, skipping artifact collection."
            )
            return

        original_cwd_for_artifact_collection = Path.cwd()
        if self.build_dir.exists() and self.build_dir.is_dir():
            os.chdir(self.build_dir)
            self.logger.debug(
                f"Temporarily changed CWD to {self.build_dir} for artifact collection."
            )
        else:
            self.logger.warning(
                f"Build directory {self.build_dir} does not exist or is not a directory. Cannot collect artifacts from it."
            )
            # No need to change back if we never changed in the first place.
            return

        self.logger.info(
            f"Collecting build artifacts from {self.build_dir} to {self.artifacts_path}..."
        )
        root_artifacts = ["PKGBUILD", ".SRCINFO", ".nvchecker.toml"]
        pkg_subdir_relative = Path("pkg") / self.config.package_name
        pkg_artifacts = [".BUILDINFO", ".MTREE", ".PKGINFO"]

        # Helper function to handle copying and sanitizing
        def copy_sanitized(src_path: Path, dest_dir: Path):
            if not src_path.exists() or not src_path.is_file():
                self.logger.debug(f"Artifact '{src_path}' not found or not a file.")
                return

            original_name = src_path.name
            sanitized_name = self._sanitize_filename(original_name)
            dest_file = dest_dir / sanitized_name

            if original_name != sanitized_name:
                self.logger.info(
                    f"Sanitizing artifact filename for upload: '{original_name}' -> '{sanitized_name}'"
                )

            try:
                shutil.copy2(src_path, dest_file)
                self.logger.debug(f"Copied artifact '{original_name}' to '{dest_file}'")
            except Exception as e:
                self.logger.warning(f"Failed to copy artifact '{original_name}': {e}")

        # --- Process all artifact types using the helper ---

        for file_name in root_artifacts:
            copy_sanitized(Path(file_name), self.artifacts_path)

        if pkg_subdir_relative.exists() and pkg_subdir_relative.is_dir():
            for file_name in pkg_artifacts:
                copy_sanitized(pkg_subdir_relative / file_name, self.artifacts_path)
        else:
            self.logger.debug(
                f"Package subdirectory '{pkg_subdir_relative}' does not exist in {self.build_dir}."
            )

        log_patterns = [f"{self.config.package_name}*.log", "*.log"]
        copied_logs = set()
        for pattern in log_patterns:
            self.logger.debug(
                f"Searching for log files with pattern: '{pattern}' in {self.build_dir}"
            )
            for log_file in Path(".").glob(pattern):
                if log_file.is_file() and log_file.name not in copied_logs:
                    # The helper handles the copy, sanitization, and logging
                    copy_sanitized(log_file, self.artifacts_path)
                    copied_logs.add(log_file.name)
                elif log_file.name in copied_logs:
                    self.logger.debug(
                        f"Log file '{log_file.name}' already copied, skipping."
                    )

        self.logger.debug(
            "Skipping package file collection - package files are too large for artifacts"
        )
        os.chdir(original_cwd_for_artifact_collection)
        self.logger.debug(
            f"Restored CWD to {original_cwd_for_artifact_collection} after artifact collection."
        )

    def _parse_ci_flags_from_pkgbuild(
        self,
    ) -> Dict[str, List[str] | Optional[str] | Dict[str, str]]:
        """
        Parses a special # ci|...| comment in a PKGBUILD for runner flags.
        Example: # ci|skipcheck,forcedep=package1 package2,anotherflag,prebuild=prebuild.sh,envset_VARIABLE=true,envset_DEBUG|
        Returns a dictionary:
            {
                "flags": ["skipcheck", "anotherflag"],
                "forced_dependencies": ["package1", "package2"],
                "prebuild_script": "prebuild.sh",  # or None if not present
                "environment_variables": {"VARIABLE": "true", "DEBUG": "true"}
            }
        """
        pkgbuild_path = self.build_dir / "PKGBUILD"
        default_return = {
            "flags": [],
            "forced_dependencies": [],
            "prebuild_script": None,
            "environment_variables": {},
        }

        if not pkgbuild_path.is_file():
            return default_return

        try:
            content = pkgbuild_path.read_text()
            match = re.search(r"^\s*#\s*ci\|([^|]+)\|", content, re.MULTILINE)
            if match:
                flags_str = match.group(1).strip()
                raw_flag_items = [f.strip() for f in flags_str.split(",") if f.strip()]

                parsed_simple_flags = []
                parsed_forced_deps = []
                environment_variables = {}
                prebuild_script = None

                for item in raw_flag_items:
                    if item.startswith("forcedep="):
                        deps_value_str = item.split("=", 1)[1].strip()
                        deps_list = [dep for dep in deps_value_str.split() if dep]
                        if deps_list:
                            parsed_forced_deps.extend(deps_list)
                    elif item.startswith("prebuild="):
                        prebuild_script = item.split("=", 1)[1].strip()
                    elif item.startswith("envset_"):
                        key_value = item[len("envset_") :]
                        if "=" in key_value:
                            var, value = key_value.split("=", 1)
                            environment_variables[var.strip()] = value.strip()
                        else:
                            # No value specified, default to "true"
                            environment_variables[key_value.strip()] = "true"
                    elif item:
                        parsed_simple_flags.append(item)

                if parsed_simple_flags:
                    self.logger.info(
                        f"Found CI simple flags in PKGBUILD: {parsed_simple_flags}"
                    )
                if parsed_forced_deps:
                    self.logger.info(
                        f"Found CI forced dependencies in PKGBUILD: {parsed_forced_deps}"
                    )
                if prebuild_script:
                    self.logger.info(
                        f"Found CI prebuild script in PKGBUILD: {prebuild_script}"
                    )
                if environment_variables:
                    self.logger.info(
                        f"Found CI environment variables in PKGBUILD: {environment_variables}"
                    )

                return {
                    "flags": parsed_simple_flags,
                    "forced_dependencies": parsed_forced_deps,
                    "prebuild_script": prebuild_script,
                    "environment_variables": environment_variables,
                }
        except Exception as e:
            self.logger.warning(f"Could not parse CI flags from PKGBUILD: {e}")

        return default_return

    def _execute_prebuild_script(self, script_name: str) -> bool:
        """
        Executes the given prebuild script from self.build_dir.
        Returns True if execution succeeds, False otherwise.
        """
        script_path = self.build_dir / script_name
        if not script_path.is_file():
            self.logger.error(f"Prebuild script not found: {script_path}")
            return False

        try:
            subprocess.run(["bash", str(script_path)], cwd=self.build_dir, check=True)
            self.logger.info(f"Prebuild script executed successfully: {script_name}")
            return True
        except subprocess.CalledProcessError as e:
            self.logger.error(
                f"Prebuild script failed with exit code {e.returncode}: {script_name}"
            )
        except Exception as e:
            self.logger.error(f"Error running prebuild script {script_name}: {e}")
        return False

    def build_package(self) -> bool:
        if self.config.build_mode not in ["build", "test"]:
            self.logger.info(
                f"Build mode is '{self.config.build_mode}', skipping actual package build steps (makepkg, release)."
            )
            if self.result.changes_detected:
                try:
                    self.logger.info(
                        "PKGBUILD changed, updating checksums in PKGBUILD (updpkgsums)..."
                    )
                    self.subprocess_runner.run_command(["updpkgsums"])
                    self.logger.info("Regenerating .SRCINFO for nobuild mode...")
                    srcinfo_result = self.subprocess_runner.run_command(
                        ["makepkg", "--printsrcinfo"]
                    )
                    (self.build_dir / ".SRCINFO").write_text(srcinfo_result.stdout)
                    self.logger.info(".SRCINFO regenerated.")
                except Exception as e:
                    self.logger.warning(
                        f"Failed to update shasums/regenerate .SRCINFO in '{self.config.build_mode}' mode: {e}"
                    )
            return True

        build_process_successful = False
        try:
            # Parse CI flags from PKGBUILD
            parsed_ci_options = self._parse_ci_flags_from_pkgbuild()
            ci_simple_flags = parsed_ci_options.get("flags", [])
            forced_dependencies = parsed_ci_options.get("forced_dependencies", [])
            additional_environment_variables = parsed_ci_options.get(
                "environment_variables", {}
            )

            if parsed_ci_options["prebuild_script"]:
                self._execute_prebuild_script(parsed_ci_options["prebuild_script"])

            # Install forced dependencies if specified
            if forced_dependencies:
                self.logger.info(
                    f"Attempting to install forced dependencies specified in PKGBUILD: {', '.join(forced_dependencies)}"
                )
                # Re-use _install_missing_packages as it handles individual package installation
                # with provider lookups and appropriate paru flags.
                if not self._install_missing_packages(forced_dependencies):
                    self.logger.error(
                        "Failed to install one or more forced dependencies. Aborting build process."
                    )
                    self.result.error_message = "Failed to install forced dependencies listed in PKGBUILD CI flags."
                    return False  # Critical failure, stop the build
                self.logger.info(
                    "Successfully installed all specified forced dependencies."
                )

            # Prepare makepkg flags based on simple CI flags (e.g., skipcheck)
            extra_mflags = []
            if "skipcheck" in ci_simple_flags:  # Use ci_simple_flags here
                self.logger.info(
                    "CI flag 'skipcheck' found. Adding --nocheck to makepkg flags."
                )
                extra_mflags.append("--nocheck")
            # Add other simple flag processing here if needed in the future

            self.logger.info("Updating checksums in PKGBUILD (updpkgsums)...")
            self.subprocess_runner.run_command(["updpkgsums"])
            self.result.changes_detected = True

            self.logger.info("Regenerating .SRCINFO file...")
            srcinfo_result = self.subprocess_runner.run_command(
                ["makepkg", "--printsrcinfo"]
            )
            (self.build_dir / ".SRCINFO").write_text(srcinfo_result.stdout)
            self.result.changes_detected = True

            # Import any PGP keys declared in the PKGBUILD
            self._import_pgp_keys()

            # Attempt the build with retry logic for missing packages
            build_process_successful = self._attempt_build_with_retry(
                extra_mflags=extra_mflags, add_env=additional_environment_variables
            )

            if not build_process_successful:
                return False

            if self.config.build_mode == "build":
                if self.result.version:
                    built_package_files = self._get_built_package_files()
                    if built_package_files:
                        self._create_release(built_package_files)
                    else:
                        self.logger.warning(
                            "No built packages found for release creation."
                        )
                else:
                    self.logger.warning(
                        "Build mode is 'build' but no version determined. Skipping GitHub Release creation."
                    )
            return True

        except subprocess.CalledProcessError as e:
            self.logger.error(
                f"Build process failed during command: {shlex.join(e.cmd)} (Return code: {e.returncode})",
                exc_info=self.config.debug,
            )
            self.logger.error(f"Stdout: {e.stdout}")
            self.logger.error(f"Stderr: {e.stderr}")
            self.result.error_message = (
                f"Build failed: {e.cmd} exited {e.returncode}. Stderr: {e.stderr[:200]}"
            )
            return False
        except Exception as e:
            self.logger.error(
                f"Build process failed with Python exception: {e}",
                exc_info=self.config.debug,
            )
            self.result.error_message = f"Build failed due to Python error: {str(e)}"
            return False

    def _attempt_build_with_retry(
        self,
        max_retries: int = 3,
        extra_mflags: Optional[List[str]] = None,
        add_env: Optional[Dict[str, str]] = None,
    ) -> bool:
        """Attempt to build the package, retrying with missing package installation if needed."""
        if extra_mflags is None:
            extra_mflags = []
        if add_env is None:
            add_env = {}

        # Dynamically construct the --mflags argument string
        mflags_parts = [
            "-Lfs",
            "--check",
            "--noconfirm",
            "--noprogressbar",
        ] + extra_mflags
        mflags_arg_str = " ".join(mflags_parts)

        for attempt in range(max_retries):
            build_failed = False

            try:
                self.logger.info(
                    f'Starting package build attempt {attempt + 1} (paru -Ui --noconfirm --pgpfetch --mflags "{mflags_arg_str}") in {self.build_dir}...'
                )
                build_cmd = f'paru -Ui --noconfirm --pgpfetch --mflags "{mflags_arg_str}" 2>&1 | tee "paru.log"'

                # Merge environment variables
                env = os.environ.copy()
                env.update(add_env)

                result = self.subprocess_runner.run_command(
                    [build_cmd], shell=True, env=env
                )

                # Check if paru failed even if the shell command succeeded
                if result.returncode != 0:
                    build_failed = True
                    self.logger.warning(
                        f"paru command failed with return code: {result.returncode}"
                    )

            except subprocess.CalledProcessError as e:
                build_failed = True
                self.logger.warning(
                    f"Build attempt {attempt + 1} failed (Return code: {e.returncode})"
                )

            # Check for built packages regardless of command success/failure
            built_package_files = self._get_built_package_files()

            if built_package_files and not build_failed:
                # Build succeeded
                self.result.built_packages = [p.name for p in built_package_files]
                self.logger.info(
                    f"Successfully built: {', '.join(self.result.built_packages)}"
                )
                self.logger.info(
                    f"Package(s) automatically installed by paru: {', '.join(self.result.built_packages)}"
                )
                return True

            # Build failed or no packages found
            if not built_package_files:
                self.logger.warning(
                    "No packages were built (no .pkg.tar.zst files found)."
                )

            # Check if this was the last attempt
            if attempt == max_retries - 1:
                self.logger.error("All build attempts failed")
                self.result.error_message = "No .pkg.tar.zst files found after makepkg."
                return False

            # Try to identify and install missing packages for retry
            missing_packages = self._parse_missing_packages_from_log()
            if not missing_packages:
                self.logger.error(
                    "Build failed but no missing packages detected in paru.log"
                )
                self.result.error_message = (
                    "Build failed with no identifiable missing packages."
                )
                return False

            self.logger.info(
                f"Detected missing packages: {', '.join(missing_packages)}"
            )

            # Try to install missing packages
            if not self._install_missing_packages(missing_packages):
                self.logger.error(
                    "Failed to install missing packages, aborting retries"
                )
                self.result.error_message = "Failed to install missing dependencies."
                return False

            self.logger.info(
                "Missing packages installed successfully, retrying build..."
            )

        return False

    def _get_built_package_files(self) -> list:
        """Get list of built package files."""
        built_package_files = sorted(self.build_dir.glob("*.pkg.tar.zst"))
        return built_package_files

    def _parse_missing_packages_from_log(self) -> list:
        """Parse the paru.log file to extract missing package names."""
        paru_log_path = self.build_dir / "paru.log"
        if not paru_log_path.exists():
            self.logger.warning("paru.log not found, cannot parse missing packages")
            return []

        missing_packages = []
        try:
            log_content = paru_log_path.read_text()

            # Pattern to match "error: could not find all required packages:" followed by package names
            # This handles the format: "    python-aifc (wanted by: ...)"
            in_error_section = False
            for line in log_content.split("\n"):
                original_line = line
                line = line.strip()

                if "error: could not find all required packages:" in line:
                    in_error_section = True
                    continue

                if in_error_section:
                    # Look for indented lines with package names followed by "(wanted by:"
                    # Match pattern like "    python-aifc (wanted by: ...)"
                    if original_line.startswith("    ") and "(wanted by:" in line:
                        # Extract package name before "(wanted by:"
                        match = re.match(r"\s*([a-zA-Z0-9._+-]+)\s+\(wanted by:", line)
                        if match:
                            package_name = match.group(1)
                            missing_packages.append(package_name)
                            self.logger.debug(f"Found missing package: {package_name}")
                    elif line == "" or (
                        not original_line.startswith(" ") and line != ""
                    ):
                        # Empty line or non-indented line indicates end of error section
                        in_error_section = False

            # Remove duplicates while preserving order
            missing_packages = list(dict.fromkeys(missing_packages))

            if not missing_packages:
                self.logger.warning(
                    "No missing packages found in paru.log despite build failure"
                )
                self.logger.debug(f"Log content:\n{log_content}")

        except Exception as e:
            self.logger.error(f"Error parsing paru.log: {e}")
            return []

        return missing_packages

    def _install_missing_packages(self, packages: list) -> bool:
        """Install missing packages using paru, finding provider packages if needed."""
        if not packages:
            return True

        for package in packages:
            if not self._install_single_package(package):
                self.logger.error(
                    f"Failed to install package or any of its providers: {package}"
                )
                return False

        return True

    def _install_single_package(self, package: str) -> bool:
        """Install a single package, trying providers if the exact name fails."""
        # First try installing the package directly
        self.logger.info(f"Installing missing package: {package}")
        if self._try_install_package(package):
            return True

        # If direct install fails, try to find provider packages
        self.logger.info(
            f"Direct install of '{package}' failed, searching for provider packages..."
        )
        providers = self._find_package_providers(package)

        if not providers:
            self.logger.warning(f"No provider packages found for: {package}")
            return False

        self.logger.info(
            f"Found provider packages for '{package}': {', '.join(providers)}"
        )

        # Try each provider in order until one succeeds
        for provider in providers:
            self.logger.info(f"Trying to install provider package: {provider}")
            if self._try_install_package(provider):
                self.logger.info(
                    f"Successfully installed provider '{provider}' for missing package '{package}'"
                )
                return True
            else:
                self.logger.warning(f"Failed to install provider package: {provider}")

        self.logger.error(f"All provider packages failed for: {package}")
        return False

    def _try_install_package(self, package: str) -> bool:
        """Try to install a specific package, returning True on success."""
        try:
            install_cmd = [
                "paru",
                "-S",
                "--noconfirm",
                "--pgpfetch",
                "--nocheck",
                package,
            ]
            self.subprocess_runner.run_command(install_cmd)
            self.logger.info(f"Successfully installed: {package}")
            return True
        except subprocess.CalledProcessError as e:
            self.logger.debug(f"Failed to install '{package}': {e}")
            return False
        except Exception as e:
            self.logger.debug(f"Exception while installing '{package}': {e}")
            return False

    def _find_package_providers(self, package: str) -> list:
        """Find packages that provide the given package name using AUR API."""
        try:
            import requests

            # Search AUR for packages that provide this package
            url = f"https://aur.archlinux.org/rpc/v5/search/{package}?by=provides"
            self.logger.debug(f"Querying AUR API: {url}")

            response = requests.get(url, timeout=10)
            response.raise_for_status()

            data = response.json()
            package_names = [pkg["Name"] for pkg in data.get("results", [])]

            self.logger.debug(
                f"AUR API returned providers for '{package}': {package_names}"
            )
            return package_names

        except ImportError:
            self.logger.warning(
                "requests module not available, cannot query AUR API for providers"
            )
            return []
        except Exception as e:
            self.logger.warning(f"Failed to query AUR API for package providers: {e}")
            return []

    def _sign_package_files(self, package_files: List[Path]) -> List[Path]:
        """Sign each package file with GPG and return list of original + .sig files."""
        gpg_key = os.environ.get("GPG_SIGNATURE")
        if not gpg_key:
            self.logger.info("No GPG_SIGNATURE found; skipping signing.")
            return package_files  # Return only original files

        signed_files = []
        for pkg_path in package_files:
            self.logger.info(f"Signing {pkg_path.name}...")
            try:
                subprocess.run(
                    [
                        "gpg",
                        "--batch",
                        "--yes",
                        "--detach-sign",
                        "-u",
                        gpg_key,
                        str(pkg_path),
                    ],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                self.logger.info(f"✔ Signed: {pkg_path.name}")
                signed_files.append(pkg_path)
                signed_files.append(pkg_path.with_suffix(pkg_path.suffix + ".sig"))
            except subprocess.CalledProcessError as e:
                self.logger.error(
                    f"✖ Failed to sign {pkg_path.name}:\n{e.stderr.decode().strip()}"
                )
        return signed_files

    def _create_release(self, package_files: List[Path]):
        if not self.result.version:
            self.logger.error(
                "Cannot create GitHub release: package version is not determined."
            )
            self.result.error_message = (
                self.result.error_message or ""
            ) + "; GitHub release skipped: version unknown."
            return

        tag_name = self.config.package_name
        release_title = f"{self.config.package_name} {self.result.version}"

        self.logger.info(
            f"Preparing GitHub release with tag '{tag_name}' and title '{release_title}'."
        )

        try:
            self.logger.info(
                f"Attempting to delete existing release and tag '{tag_name}' to ensure a clean slate."
            )
            delete_cmd = [
                "gh",
                "release",
                "delete",
                tag_name,
                "-R",
                self.config.github_repo,
                "--yes",
            ]
            delete_result = self.subprocess_runner.run_command(delete_cmd, check=False)

            if delete_result.returncode == 0:
                self.logger.info(
                    f"Successfully deleted existing release for tag '{tag_name}'."
                )
            elif (
                delete_result.stderr
                and "release not found" in delete_result.stderr.lower()
            ):
                self.logger.info(
                    f"No existing release found for tag '{tag_name}'. Proceeding to create a new one."
                )
            else:
                self.logger.warning(
                    f"Could not delete release '{tag_name}'. It might not exist or another error occurred. Stderr: {delete_result.stderr.strip() if delete_result.stderr else 'N/A'}"
                )
            release_notes = self.RELEASE_BODY
            for package in self.result.built_packages:
                if "-debug-" not in package:
                    release_notes += (
                        f"  sudo pacman -U {self.URL}/{tag_name}/{package}\n"
                    )
            release_notes += "```\n"

            # sign our releases
            signed_package_files = self._sign_package_files(package_files)
            signed_package_file_paths_str = [str(p) for p in signed_package_files]
            create_release_cmd = [
                "gh",
                "release",
                "create",
                tag_name,
                "--title",
                release_title,
                "--notes",
                release_notes,
                "-R",
                self.config.github_repo,
            ] + signed_package_file_paths_str

            self.logger.info(
                f"Creating new release for tag '{tag_name}' and uploading {len(signed_package_file_paths_str)} asset(s)."
            )
            self.subprocess_runner.run_command(create_release_cmd)
            self.logger.info(
                f"Successfully created release '{tag_name}' and uploaded assets."
            )

        except subprocess.CalledProcessError as e:
            self.logger.error(
                f"Failed to create GitHub release '{tag_name}': CMD: {e.cmd}, RC: {e.returncode}, Stderr: {e.stderr}"
            )
            self.result.error_message = (
                self.result.error_message or ""
            ) + f"; GitHub release failed for {tag_name}: {e.stderr[:100]}"
        except Exception as e:
            self.logger.error(
                f"An unexpected error occurred during GitHub release for '{tag_name}': {e}",
                exc_info=self.config.debug,
            )
            self.result.error_message = (
                self.result.error_message or ""
            ) + f"; Unexpected error during GitHub release for {tag_name}"

    def commit_and_push(self) -> bool:
        if (self.build_dir / "PKGBUILD").is_file() and not (
            self.build_dir / ".SRCINFO"
        ).is_file():
            try:
                self.logger.info(".SRCINFO missing, attempting to generate it...")
                srcinfo_result = self.subprocess_runner.run_command(
                    ["makepkg", "--printsrcinfo"]
                )
                (self.build_dir / ".SRCINFO").write_text(srcinfo_result.stdout)
                self.result.changes_detected = True
            except Exception as e:
                self.logger.error(f"Failed to generate .SRCINFO before commit: {e}")
                self.result.error_message = "Failed to generate .SRCINFO"
                return False

        self.logger.info(
            f"Preparing to commit to AUR. Tracked files: {self.tracked_files}"
        )

        existing_tracked_files_for_add = []
        for file_name in self.tracked_files:
            if (self.build_dir / file_name).is_file():
                existing_tracked_files_for_add.append(file_name)
            else:
                self.logger.warning(
                    f"Tracked file '{file_name}' not found in {self.build_dir}. It will not be added to git."
                )

        if not existing_tracked_files_for_add:
            self.logger.info(
                "No existing tracked files to 'git add'. Checking for other changes..."
            )
        else:
            self.subprocess_runner.run_command(
                ["git", "add"] + existing_tracked_files_for_add
            )

        if not self._has_git_changes_to_commit():
            self.logger.info("No git changes to commit to AUR.")
            return True

        self.logger.info(
            "Git changes detected. Proceeding with commit and push to AUR."
        )
        self.result.changes_detected = True

        commit_version_suffix = (
            f" (v{self.result.version})" if self.result.version else ""
        )
        final_commit_msg = f"{self.config.commit_message}{commit_version_suffix}"

        try:
            env = os.environ.copy()
            env["GIT_AUTHOR_NAME"] = os.environ.get(
                "GIT_COMMIT_USER_NAME", "Github Actions"
            )
            env["GIT_AUTHOR_EMAIL"] = os.environ.get(
                "GIT_COMMIT_USER_EMAIL", "default@example.com"
            )
            env["GIT_COMMITTER_NAME"] = env["GIT_AUTHOR_NAME"]
            env["GIT_COMMITTER_EMAIL"] = env["GIT_AUTHOR_EMAIL"]

            self.subprocess_runner.run_command(
                ["git", "commit", "-m", final_commit_msg], env=env
            )
            self.subprocess_runner.run_command(
                ["git", "push", "origin", "master"], env=env
            )
            self.logger.info("Changes successfully committed and pushed to AUR.")

            self.logger.info(
                "Syncing committed files back to source GitHub repository..."
            )
            for file_name_in_aur_commit in existing_tracked_files_for_add:
                file_path_in_aur_clone = self.build_dir / file_name_in_aur_commit
                path_in_source_repo = (
                    Path(self.config.pkgbuild_path) / file_name_in_aur_commit
                )
                self._update_github_file(
                    str(path_in_source_repo), file_path_in_aur_clone, final_commit_msg
                )
            return True
        except subprocess.CalledProcessError as e:
            self.logger.error(
                f"Git operation (commit/push to AUR or update to source repo) failed: CMD: {e.cmd}, RC: {e.returncode}"
            )
            self.logger.error(f"Stdout: {e.stdout}")
            self.logger.error(f"Stderr: {e.stderr}")
            self.result.error_message = (
                self.result.error_message or ""
            ) + f"; Git op failed: {e.cmd} -> {e.stderr[:100]}"
            return False
        except Exception as e:
            self.logger.error(
                f"Unexpected error during git operations or source repo update: {e}",
                exc_info=self.config.debug,
            )
            self.result.error_message = (
                self.result.error_message or ""
            ) + f"; Unexpected error in git/source repo update: {str(e)}"
            return False

    def _update_github_file(
        self, path_in_repo: str, local_file_path: Path, commit_msg: str
    ):
        self.logger.info(
            f"Updating '{path_in_repo}' in GitHub repo '{self.config.github_repo}' from local file '{local_file_path}'."
        )
        try:
            with open(local_file_path, "rb") as f:
                content_bytes = f.read()
            content_b64 = base64.b64encode(content_bytes).decode("utf-8")
            get_sha_cmd = [
                "gh",
                "api",
                f"repos/{self.config.github_repo}/contents/{path_in_repo}",
                "--jq",
                ".sha",
            ]
            sha_result = self.subprocess_runner.run_command(get_sha_cmd, check=False)
            current_sha = (
                sha_result.stdout.strip()
                if sha_result.returncode == 0 and sha_result.stdout.strip() != "null"
                else None
            )

            source_repo_commit_message = (
                f"Sync {Path(path_in_repo).name} from AUR build"
            )
            if self.result.version:
                source_repo_commit_message += f" (v{self.result.version})"
            elif commit_msg:
                source_repo_commit_message = commit_msg

            update_fields = [
                "-f",
                f"message={source_repo_commit_message}",
                "-f",
                f"content={content_b64}",
            ]
            if current_sha:
                update_fields.extend(["-f", f"sha={current_sha}"])

            update_cmd = [
                "gh",
                "api",
                "--method",
                "PUT",
                f"repos/{self.config.github_repo}/contents/{path_in_repo}",
            ] + update_fields

            self.subprocess_runner.run_command(update_cmd)
            self.logger.info(
                f"Successfully updated '{path_in_repo}' in source GitHub repository."
            )

        except subprocess.CalledProcessError as e:
            self.logger.error(
                f"Failed to update '{path_in_repo}' in GitHub repo. CMD: {e.cmd}, RC: {e.returncode}, Stderr: {e.stderr}"
            )
            detailed_error = f"gh api update for {path_in_repo} failed. CMD: {shlex.join(e.cmd)}. Stderr: {e.stderr}"
            self.result.error_message = (
                self.result.error_message or ""
            ) + f"; {detailed_error}"
            raise
        except FileNotFoundError:
            self.logger.error(
                f"Local file '{local_file_path}' not found for GitHub source update."
            )
            raise
        except Exception as e:
            self.logger.error(
                f"Unexpected error updating GitHub source file '{path_in_repo}': {e}",
                exc_info=self.config.debug,
            )
            raise

    def _has_git_changes_to_commit(self) -> bool:
        if Path.cwd() != self.build_dir:
            self.logger.warning(
                f"Git check called from {Path.cwd()}, expected {self.build_dir}. This might be an issue."
            )

        diff_result = self.subprocess_runner.run_command(
            ["git", "diff", "--cached", "--quiet"], check=False
        )

        # A non-zero return code means there are staged changes to be committed.
        has_changes = diff_result.returncode != 0

        if has_changes:
            # For better logging, let's see what those changes are.
            status_result = self.subprocess_runner.run_command(
                ["git", "status", "--porcelain"], check=False
            )
            self.logger.debug(
                f"Git porcelain status (changes detected):\n{status_result.stdout.strip()}"
            )

        return has_changes

    def cleanup(self):
        if (
            self.build_dir
            and self.build_dir.exists()
            and self.build_dir.is_relative_to(self.config.base_build_dir)
        ):
            self.logger.info(f"Cleaning up package build directory: {self.build_dir}")
            try:
                shutil.rmtree(self.build_dir)
            except Exception as e:
                self.logger.warning(
                    f"Failed to clean up package build directory {self.build_dir}: {e}. Manual cleanup might be needed."
                )
        elif self.build_dir and self.build_dir.exists():
            self.logger.warning(
                f"Not cleaning up {self.build_dir} as it's not relative to base build dir {self.config.base_build_dir} or some other issue."
            )

    def run(self) -> Dict[str, Any]:
        original_cwd = Path.cwd()
        initial_packages = self._get_installed_packages()
        self.logger.info(
            f"Tracking {len(initial_packages)} initially installed packages."
        )
        self.logger.info(
            f"buildscript.py started. Original CWD: {original_cwd}. HOME env: {os.environ.get('HOME')}"
        )
        self.logger.info(
            f"Package: {self.config.package_name}, Build Mode: {self.config.build_mode}"
        )
        self.logger.info(
            f"PackageUpdateInfo: {self.package_update_info}"
        )  # Log parsed info
        self.logger.info(f"Using base build directory: {self.config.base_build_dir}")
        self.logger.info(f"Package specific build dir will be: {self.build_dir}")

        self.result.success = True

        try:
            # 1. Authenticate with GitHub
            try:
                self.subprocess_runner.run_command(
                    ["gh", "auth", "status"], check=False
                )
            except Exception:
                pass

            if (
                "gh auth status"
                in self.subprocess_runner.run_command(
                    ["gh", "auth", "status"], check=False
                ).stderr
            ):
                self.logger.info(
                    "GitHub token might not be pre-authenticated or gh has issues. Attempting login."
                )
                if not self.authenticate_github():
                    self.result.success = False
                    raise RuntimeError(
                        self.result.error_message
                        or "GitHub authentication failed critically."
                    )

            # 2. Setup build environment (clone AUR repo, cd into it)
            self.setup_build_environment()

            # 3. Check for "Sync Down from AUR" scenario
            # Check if AUR version is newer AND (NVChecker is not newer than AUR OR NVChecker has no info)
            # This logic relies on aur_package_updater_cli.py correctly setting `is_update` and `update_source`
            if (
                self.package_update_info.is_update
                and self.package_update_info.update_source
                and self.package_update_info.update_source.startswith("aur")
            ):  # e.g. 'aur' or 'aur (new pkg)'
                self.logger.info(
                    f"SYNC_DOWN_FROM_AUR: AUR version {self.package_update_info.aur_pkgver}-{self.package_update_info.aur_pkgrel} for {self.config.package_name} is the target. "
                    f"Updating source GitHub repository from live AUR files."
                )

                sync_commit_msg_base = f"Sync from AUR: {self.config.package_name} v{self.package_update_info.aur_pkgver}-{self.package_update_info.aur_pkgrel}"

                for file_name_to_sync in self.tracked_files:
                    aur_file_path_in_clone = self.build_dir / file_name_to_sync
                    if aur_file_path_in_clone.is_file():
                        target_repo_path_str = str(
                            Path(self.config.pkgbuild_path) / file_name_to_sync
                        )
                        self.logger.info(
                            f"Syncing '{aur_file_path_in_clone}' to source repo at '{target_repo_path_str}'"
                        )
                        self._update_github_file(
                            target_repo_path_str,
                            aur_file_path_in_clone,
                            f"{sync_commit_msg_base} ({file_name_to_sync})",
                        )
                    else:
                        self.logger.warning(
                            f"File '{file_name_to_sync}' not found in AUR clone '{self.build_dir}' for sync down. Skipping this file."
                        )

                self.result.success = True
                self.result.version = f"{self.package_update_info.aur_pkgver}-{self.package_update_info.aur_pkgrel}"
                self.result.changes_detected = True
                self.result.action_taken = "synced_down_from_aur"
                self.logger.info(
                    f"Successfully synced down {self.config.package_name} from AUR to source repository."
                )
                return asdict(self.result)  # Early exit

            # --- If not "Sync Down", proceed with normal build/update flow ---
            # 4. Collect package files from workspace to build_dir (overlays AUR clone's files with source repo files)
            self.collect_package_files()

            # 5. Determine version and update PKGBUILD if necessary (based on nvchecker or local_is_ahead)
            if (
                self.package_update_info.is_update
                and self.package_update_info.update_source == "nvchecker"
            ):
                if self.package_update_info.new_version_for_update:
                    self.logger.info(
                        f"NVChecker indicates update for {self.config.package_name} to version {self.package_update_info.new_version_for_update}."
                    )
                    self._update_pkgbuild_version(
                        self.package_update_info.new_version_for_update
                    )
                    self.result.version = (
                        self.package_update_info.new_version_for_update
                    )
                    self.result.action_taken = "updated_via_nvchecker"
                else:
                    self.logger.warning(
                        f"NVChecker update indicated for {self.config.package_name}, but new_version_for_update is missing. Using local PKGBUILD version."
                    )
                    self.result.version = self._get_current_pkgbuild_version()
            elif self.package_update_info.local_is_ahead:
                self.logger.info(
                    f"Local version {self.package_update_info.pkgver}-{self.package_update_info.pkgrel} for {self.config.package_name} is ahead. Will push to AUR."
                )
                self.result.version = (
                    f"{self.package_update_info.pkgver}-{self.package_update_info.pkgrel}"
                    if self.package_update_info.pkgver
                    else self._get_current_pkgbuild_version()
                )
                self.result.changes_detected = True  # Local files are different
                self.result.action_taken = "pushed_local_ahead_to_aur"
            else:  # No explicit update by nvchecker, and not local_is_ahead. Use current PKGBUILD version from source.
                self.result.version = self._get_current_pkgbuild_version()  # Read from PKGBUILD in self.build_dir (which now has source repo files)
                self.logger.info(
                    f"No direct update trigger. Current version for {self.config.package_name} from PKGBUILD is {self.result.version}."
                )
                # changes_detected might be set by updpkgsums or if local files differ from AUR git history later

            # 6. Process local source files (uses self.package_update_info.sources, which came from the PKGBUILD from the source repo)
            if (
                not self.process_package_sources()
            ):  # This adds local files from source array to self.tracked_files
                self.result.success = False
                raise Exception(
                    self.result.error_message or "Processing package sources failed"
                )

            # 7. Build package (makepkg, local install, create GH release)
            # This step also handles .SRCINFO generation.
            if not self.build_package():  # This will run or skip based on build_mode
                self.result.success = False
                raise Exception(
                    self.result.error_message or "Package build/preparation failed"
                )

            # 8. Commit and push to AUR, then sync back to source GitHub repo
            # Only commit/push if not a "sync down" operation (already handled)
            # AND (changes_detected OR build_mode forces it)
            if self.result.action_taken != "synced_down_from_aur" and (
                self.result.changes_detected
                or self.config.build_mode in ["build", "test"]
            ):
                if not self.commit_and_push():
                    if (
                        self.result.error_message
                        and "failed" in self.result.error_message.lower()
                    ):
                        self.result.success = False
                        raise Exception(self.result.error_message)
            else:
                if self.result.action_taken == "synced_down_from_aur":
                    self.logger.info(
                        "Skipping AUR commit and push because action was 'synced_down_from_aur'."
                    )
                else:  # No changes detected or not a build/test mode
                    self.logger.info(
                        "No changes detected or not in build/test mode. Skipping AUR commit and push."
                    )

            if self.result.success:
                self.logger.info(
                    f"Successfully processed package {self.config.package_name}"
                )

        except Exception as e:
            self.logger.error(
                f"Exception during run for package {self.config.package_name}: {str(e)}",
                exc_info=self.config.debug,
            )
            self.result.success = False
            if not self.result.error_message:
                self.result.error_message = str(e)
        finally:
            self._cleanup_installed_packages(initial_packages)
            self._collect_build_artifacts()
            if Path.cwd() != original_cwd:
                os.chdir(original_cwd)
                self.logger.debug(
                    f"Restored current directory to {original_cwd} before cleanup."
                )
            self.cleanup()

        return asdict(self.result)


def main():
    parser = argparse.ArgumentParser(
        description="Build and publish Arch Linux packages"
    )
    parser.add_argument(
        "--github-repo", required=True, help="GitHub repository (owner/repo)"
    )
    parser.add_argument(
        "--github-token", required=True, help="GitHub authentication token"
    )
    parser.add_argument(
        "--github-workspace", required=True, help="Path to GitHub workspace directory"
    )
    parser.add_argument(
        "--package-name", required=True, help="Name of the package to build (pkgbase)"
    )
    parser.add_argument(
        "--package-update-info-json",
        required=True,
        help="JSON string containing comprehensive update info for the package.",
    )
    parser.add_argument(
        "--pkgbuild-path",
        required=True,
        help="Relative path within the workspace to the directory containing PKGBUILD and related files (e.g., category/packagename)",
    )
    parser.add_argument(
        "--commit-message",
        required=True,
        help="Base Git commit message for AUR updates",
    )
    parser.add_argument(
        "--build-mode",
        choices=["nobuild", "build", "test"],
        default="nobuild",
        help="Build mode: 'nobuild' (prepare, check version, commit AUR changes), 'build' (nobuild + build, create GH release), 'test' (nobuild + build, no GH release)",
    )
    parser.add_argument(
        "--artifacts-dir",
        default=None,
        help="Directory to store build artifacts for this package (e.g., logs). Path will be specific to the package.",
    )
    parser.add_argument(
        "--base-build-dir",
        required=True,
        type=Path,
        help="Base directory where package-specific temporary build directories will be created.",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")

    args = parser.parse_args()

    if args.debug:
        logging.getLogger("arch_builder_script").setLevel(logging.DEBUG)
    else:
        logging.getLogger("arch_builder_script").setLevel(logging.INFO)

    config = BuildConfig(**vars(args))

    logger.debug(f"Initial HOME from environment: {os.environ.get('HOME')}")

    builder = ArchPackageBuilder(config)
    result_dict = builder.run()

    try:
        print(json.dumps(result_dict, indent=2))
    except TypeError as e:
        err_json = json.dumps(
            {
                "success": False,
                "package_name": args.package_name,
                "error_message": f"Failed to serialize result to JSON: {e}. Raw result: {result_dict}",
                "version": None,
                "built_packages": [],
                "changes_detected": False,
                "action_taken": None,
            }
        )
        print(err_json)
        logger.error(
            f"CRITICAL: Failed to serialize result_dict to JSON: {e}. Raw dict: {result_dict}"
        )

    sys.exit(0 if result_dict.get("success", False) else 1)


if __name__ == "__main__":
    main()
