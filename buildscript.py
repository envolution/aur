#!/usr/bin/env python3

import argparse
import json
import logging
import os
import subprocess
import sys
import re
from dataclasses import dataclass, asdict
from pathlib import Path
import base64
import shutil
import tempfile
from typing import List, Tuple, Dict, Optional, Any, Union

logging.basicConfig(level=logging.INFO)
@dataclass
class CommandResult:
    command: List[str]
    returncode: int
    stdout: str
    stderr: str

class SubprocessRunner:
    def __init__(self, logger: logging.Logger):
        self.logger = logger

    def run_command(
        self,
        cmd: List[str],
        check: bool = True,
        input_data: Optional[str] = None
    ) -> CommandResult:
        self.logger.debug(f"Running command: {' '.join(cmd)}")
        
        try:
            process = subprocess.run(
                cmd,
                check=check,
                text=True,
                capture_output=True,
                input=input_data
            )
            return CommandResult(
                command=cmd,
                returncode=process.returncode,
                stdout=process.stdout,
                stderr=process.stderr
            )
        except subprocess.CalledProcessError as e:
            self.logger.error(f"{e.stderr}")
            if check:
                raise
            return CommandResult(
                command=cmd,
                returncode=e.returncode,
                stdout=e.stdout or "",
                stderr=e.stderr or ""
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
    debug: bool = False

@dataclass
class BuildResult:
    success: bool
    package_name: str
    version: Optional[str] = None
    built_packages: List[str] = None
    error_message: Optional[str] = None
    changes_detected: bool = False

class ArchPackageBuilder:
    RELEASE_BODY = "To install, run: sudo pacman -U PACKAGENAME.pkg.tar.zst"
    TRACKED_FILES = ['PKGBUILD', '.SRCINFO', '.nvchecker.toml']

    def __init__(self, config: BuildConfig):
        self.config = config
        self.logger = self._setup_logger()
        self.build_dir = Path(tempfile.mkdtemp(prefix=f"build-{config.package_name}-"))
        self.tracked_files: List[str] = self.TRACKED_FILES.copy()
        self.result = BuildResult(success=True, package_name=config.package_name)
        self.subprocess_runner = SubprocessRunner(self.logger)

    def _setup_logger(self) -> logging.Logger:
        logger = logging.getLogger("arch_builder")
        handler = logging.StreamHandler()
        formatter = logging.Formatter('[%(levelname)s] %(message)s')
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG if self.config.debug else logging.INFO)
        return logger

    def authenticate_github(self) -> bool:
        try:
            token_file = self.build_dir / '.github_token'
            token_file.write_text(self.config.github_token)
            token = token_file.read_text().strip()
            
            self.subprocess_runner.run_command(
                ['gh', 'auth', 'login', '--with-token'],
                input_data=token
            )
            token_file.unlink()
            
            self.subprocess_runner.run_command(['gh', 'auth', 'status'])
            return True
        except Exception as e:
            self.logger.error(f"Authentication error: {str(e)}")
            return False

    def setup_build_environment(self):
        aur_repo = f"ssh://aur@aur.archlinux.org/{self.config.package_name}.git"
        self.subprocess_runner.run_command(['git', 'clone', aur_repo, str(self.build_dir)])
        os.chdir(self.build_dir)

    def collect_package_files(self):
        workspace_path = Path(self.config.github_workspace) / self.config.pkgbuild_path

        # Iterate over all files (including hidden) in the workspace
        for source_file in workspace_path.glob('**/*'):  # **/* to include all files recursively
            if source_file.is_file():  # Ensure it's a file (not a directory)
                try:
                    destination_file = self.build_dir / source_file.relative_to(workspace_path)
                    destination_file.parent.mkdir(parents=True, exist_ok=True)  # Create parent directories if needed
                    shutil.copy2(source_file, destination_file)  # Copy file with metadata
                except Exception as e:
                    self.logger.warning(f"Failed to copy {source_file}: {e}")

    def process_package_sources(self):
        workspace_path = Path(self.config.github_workspace) / self.config.pkgbuild_path
        package_name = self.config.package_name
        json_file_path = self.config.depends_json

        # Load the JSON data
        try:
            with open(json_file_path, 'r') as file:
                data = json.load(file)
        except json.JSONDecodeError as e:
            self.logger.error(f"Error decoding JSON from {json_file_path}: {e}")
            return False, {"package_name": package_name, "error": f"Error decoding JSON: {e}"}
        except FileNotFoundError:
            self.logger.error(f"File not found: {json_file_path}")
            return False, {"package_name": package_name, "error": f"File not found: {json_file_path}"}

        self.logger.info(f"Loaded JSON data for package '{package_name}'")

        # Check if the package exists in the data
        if package_name not in data:
            self.logger.warning(f"Package '{package_name}' not found in JSON data.")
            return True, {"package_name": package_name, "message": f"Package '{package_name}' has no sources."}

        # Get package data and sources
        packagedata = data[package_name]
        sources = packagedata.get('sources', [])

        if not sources:
            self.logger.warning(f"No sources found for package '{package_name}'.")
            return True, {"package_name": package_name, "message": f"Package '{package_name}' has no sources."}

        # Process each source
        processed_sources = []
        for source in sources:
            source_info = {}
            if self._is_non_file_type(source):
                # Handle non-file types (likely URLs)
                source_info['url'] = source
                self.logger.info(f"Source is a non-file type (URL): {source}")
            else:
                # Check if it's a file in the workspace
                source_file_path = workspace_path / source
                if source_file_path.is_file():
                    source_info['file'] = str(source_file_path)
                    self.tracked_files.append(source)  # Add to tracked files
                    self.logger.info(f"Source file exists: {source_file_path}")
                else:
                    source_info['file'] = None
                    self.logger.info(f"Skipping over non file source: {source}")

            processed_sources.append(source_info)

        return True, {"package_name": package_name, "sources": processed_sources}

    def _is_non_file_type(self, source):
        # If the source contains ':' or '/' it's not a file (e.g., URL or path)
        if ':' in source or '/' in source:
            return True  # It's a non-file type (e.g., URL or path)

    def process_dependencies(self) -> Tuple[bool, Dict[str, List[str]]]:
        try:
            # Load the JSON data
            package_name = self.config.package_name
            json_file_path = self.config.depends_json
            try:
                with open(json_file_path, 'r') as file:
                    data = json.load(file)
            except json.JSONDecodeError as e:
                self.logger.error(f"Error decoding JSON: {e}")
            except FileNotFoundError:
                self.logger.error(f"File not found: {json_file_path}")                    

            self.logger.debug(f"Loaded JSON data: {data}") 
            # Check if the package exists in the data
            if package_name not in data:
                return True, {"package_name": package_name, "message": f"Package '{package_name}' has no dependencies."}

            # Get package data and prepare dependencies
            package_data = data[package_name]
            combined_dependencies = (
                package_data.get("depends", []) +
                package_data.get("makedepends", []) +
                package_data.get("checkdepends", [])
            )

            # Filter out empty or invalid dependencies
            combined_dependencies = [dep for dep in combined_dependencies if dep]

            # Prepare and run the subprocess command
            command = [
                'paru', '-S', '--needed', '--norebuild', '--noconfirm',
                '--mflags', '--skipchecksums', '--mflags', '--skippgpcheck'
            ] + combined_dependencies
            
            if self.subprocess_runner.run_command(command):
                return True, {"package_name": package_name, "message": f"Dependencies for package '{package_name}' installed successfully."}
            
            return False, {"package_name": package_name, "error": "Subprocess failed during dependency installation."}
        
        except Exception as e:
            self.logger.error(f"Error processing package '{package_name}': {str(e)}")
            return False, {"package_name": package_name, "error": str(e)}

    def check_version_update(self) -> Optional[str]:
        nvchecker_path = self.build_dir / '.nvchecker.toml'
        if not nvchecker_path.is_file():
            return None

        try:
            result = self.subprocess_runner.run_command(
                ['nvchecker', '-c', '.nvchecker.toml', '--logger', 'json']
            )

            # Log the raw stdout for debugging purposes
            self.logger.debug(f"Raw NVChecker output: {result.stdout}")

            # Process each line in stdout
            new_version = None
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue  # Skip empty lines

                try:
                    # Parse each line as JSON
                    entry = json.loads(line)

                    # Check if the entry matches our criteria
                    if isinstance(entry, dict) and entry.get('logger_name') == 'nvchecker.core':
                        new_version = entry.get('version')
                        if new_version:
                            self._update_pkgbuild_version(new_version)
                            self.result.version = new_version
                            break  # Stop processing further lines once version is found
                except json.JSONDecodeError as e:
                    self.logger.warning(f"Skipping invalid JSON line: {line}. Error: {e}")
                    continue  # Ignore lines that are not valid JSON

            return new_version

        except subprocess.CalledProcessError as e:
            self.logger.error(f"Command failed: {e}")
        except Exception as e:
            self.logger.error(f"Version check failed: {e}")

        return None    

    def _update_pkgbuild_version(self, new_version: str):
        pkgbuild_path = self.build_dir / 'PKGBUILD'
        content = pkgbuild_path.read_text()
        updated_content = re.sub(r'^\s*pkgver=\s*.*$', f'pkgver={new_version}', content, flags=re.MULTILINE, count=1)
        pkgbuild_path.write_text(updated_content)

    def build_package(self, pkg_info: Dict[str, List[str]]) -> bool:
        if self.config.build_mode not in ['build', 'test']:
            return True

        try:
            # Build package
            self.logger.info("updating PKGINFO pkgsums")
            self.subprocess_runner.run_command(['updpkgsums'])
            self.logger.info("beginning makebuild")
            self.subprocess_runner.run_command(['makepkg', '-s', '--noconfirm'])
            self.logger.info("updating .SRCINFO")
            result = self.subprocess_runner.run_command(['makepkg', '--printsrcinfo'])
            with open('.SRCINFO', 'w') as srcinfo_file:
                srcinfo_file.write(result.stdout)
            
            # Install package
            packages = sorted(Path('.').glob('*.pkg.tar.zst'))
            if not packages:
                raise Exception("No packages built")
                
            self.subprocess_runner.run_command([
                'sudo', 'pacman', '--noconfirm', '-U',
                *[str(p) for p in packages]
            ])
            
            self.result.built_packages = [p.name for p in packages]
            
            if self.config.build_mode == 'build':
                self._create_release(packages)
                
            return True
            
        except Exception as e:
            self.logger.error(f"Build failed: {e}")
            self.result.error_message = str(e)
            return False

    def _create_release(self, package_files: List[Path]):
        try:
            # Create release
            self.subprocess_runner.run_command([
                'gh', 'release', 'create', self.config.package_name,
                '--title', f"Binary installers for {self.config.package_name}",
                '--notes', self.RELEASE_BODY,
                '-R', self.config.github_repo
            ], check=False)

            # Upload packages
            for pkg_file in package_files:
                self.subprocess_runner.run_command([
                    'gh', 'release', 'upload', self.config.package_name,
                    str(pkg_file), '--clobber',
                    '-R', self.config.github_repo
                ])
        except subprocess.CalledProcessError as e:
            self.logger.error(f"Failed to create release: {e}")

    def _update_github_file(self, file: str, file_path: Path):
        with open(file_path, 'rb') as f:
            content = base64.b64encode(f.read()).decode('utf-8')
        
        try:
            # Try to get existing file's SHA
            response = self.subprocess_runner.run_command([
                'gh', 'api',
                f"/repos/{self.config.github_repo}/contents/{self.config.pkgbuild_path}/{file}",
                '--jq', '.sha'
            ])
            sha = response.stdout.strip()
            
            # Update existing file
            self.subprocess_runner.run_command([
                'gh', 'api', '-X', 'PUT',
                f"/repos/{self.config.github_repo}/contents/{self.config.pkgbuild_path}/{file}",
                '-f', f"message=Auto updated {file}",
                '-f', f"content={content}",
                '-f', f"sha={sha}"
            ])
        except subprocess.CalledProcessError:
            # Create new file
            self.subprocess_runner.run_command([
                'gh', 'api', '-X', 'PUT',
                f"/repos/{self.config.github_repo}/contents/{self.config.pkgbuild_path}/{file}",
                '-f', f"message=Added {file}",
                '-f', f"content={content}"
            ])

    def commit_and_push(self) -> bool:
        if not self.tracked_files:
            self.logger.error("There are no tracked files")
            return False

        try:
            self.logger.info(f"sources currently tracked: {self.tracked_files}")
            # Iterate over self.tracked_files and check if each file exists
            self.tracked_files = [file for file in self.tracked_files if Path(file).is_file()]
            self.logger.info(f"after trimming files we can't find locally: {self.tracked_files}")
            self.subprocess_runner.run_command(['git', 'add', *self.tracked_files])
            
            if self._has_changes():
                commit_msg = f"{self.config.commit_message}: {self.result.version or ''}"
                self.subprocess_runner.run_command(['git', 'commit', '-m', commit_msg])
                self.subprocess_runner.run_command(['git', 'push', 'origin', 'master'])
                self.result.changes_detected = True
                
                # Update GitHub files
                for file in self.tracked_files:
                    file_path = self.build_dir / file
                    if file_path.is_file():
                        self._update_github_file(file, file_path)
                        
                return True
            return False
            
        except subprocess.CalledProcessError as e:
            self.logger.error(f"Failed to commit and push: {e}")
            return False

    def _has_changes(self) -> bool:
        result = self.subprocess_runner.run_command(
            ['git', 'status', '--porcelain'],
            check=False
        )
        return bool(result.stdout.strip())

    def cleanup(self):
        try:
            shutil.rmtree(self.build_dir)
        except Exception as e:
            self.logger.warning(f"Failed to clean up build directory: {e}")

    def run(self) -> Dict[str, Any]:
        try:
            # Check authentication status
            try:
                self.subprocess_runner.run_command(['gh', 'auth', 'status'])
            except subprocess.CalledProcessError:
                self.logger.info("Logging into github...")
                if not self.authenticate_github():
                    raise Exception("GitHub authentication failed")

            self.setup_build_environment()
            self.collect_package_files()
            
            pkg_info = self.process_dependencies()
            self.logger.info(f"[debug] PACKAGE INFO--> {pkg_info}")
            self.check_version_update()
            
            if not self.build_package(pkg_info):
                raise Exception("Package build failed")

            self.process_package_sources() 
            self.commit_and_push()
            
            return asdict(self.result)
            
        except Exception as e:
            self.result.success = False
            self.result.error_message = str(e)
            return asdict(self.result)
        finally:
            self.cleanup()

def main():
    parser = argparse.ArgumentParser(description='Build and publish Arch Linux packages')
    parser.add_argument('--github-repo', required=True, help='GitHub repository name')
    parser.add_argument('--github-token', required=True, help='GitHub authentication token')
    parser.add_argument('--github-workspace', required=True, help='GitHub workspace directory')
    parser.add_argument('--package-name', required=True, help='Package name')
    parser.add_argument('--depends-json', required=True, help='Path to JSON containing package names and their dependencies')
    parser.add_argument('--pkgbuild-path', required=True, help='Path to PKGBUILD directory')
    parser.add_argument('--commit-message', required=True, help='Git commit message')
    parser.add_argument('--build-mode', choices=['none', 'build', 'test'],
                       default='none', help='Build mode: nobuild, build, or test')
    parser.add_argument('--debug', action='store_true', help='Enable debug logging')

    args = parser.parse_args()
    config = BuildConfig(**vars(args))
    builder = ArchPackageBuilder(config)
    result = builder.run()
    
    print(json.dumps(result, indent=2))
    sys.exit(0 if result['success'] else 1)

if __name__ == '__main__':
    main()