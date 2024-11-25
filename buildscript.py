#!/usr/bin/env python3

import argparse
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
import base64
import shutil
import tempfile
from typing import List, Tuple, Dict, Optional, Any, Union

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
    TRACKED_FILES = ['PKGBUILD', '.nvchecker.toml']

    def __init__(self, config: BuildConfig):
        self.config = config
        self.logger = self._setup_logger()
        self.build_dir = Path(tempfile.mkdtemp(prefix=f"build-{config.package_name}-"))
        self.tracked_files: List[str] = []
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
        
        for file in self.TRACKED_FILES:
            source_file = workspace_path / file
            if source_file.is_file():
                try:
                    shutil.copy2(source_file, self.build_dir / file)
                    self.tracked_files.append(file)
                except Exception as e:
                    self.logger.warning(f"Failed to copy {file}: {e}")

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

            self.logger.warning(f"Loaded JSON data: {data}") 
            # Check if the package exists in the data
            if package_name not in data:
                return True, {"package_name": package_name, "message": f"Package '{package_name}' has no dependencies."}

            # Get package data and prepare dependencies
            package_data = data[package_name]
            combined_dependencies = self.prepare_dependencies(package_data)
            
            # Prepare and run the subprocess command
            command = [
                'paru', '-S', '--needed', '--norebuild', '--noconfirm',
                '--mflags', '--skipchecksums', '--mflags', '--skippgpcheck'
            ] + combined_dependencies
            
            if self.run_subprocess(command):
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
            version_info = json.loads(result.stdout)
            new_version = next(
                (item['version'] for item in version_info 
                if item.get('logger_name') == 'nvchecker.core'),
                None
            )
            
            if new_version:
                self._update_pkgbuild_version(new_version)
                self.result.version = new_version
                return new_version
                
        except Exception as e:
            self.logger.error(f"Version check failed: {e}")
            return None

    def _update_pkgbuild_version(self, new_version: str):
        pkgbuild_path = self.build_dir / 'PKGBUILD'
        content = pkgbuild_path.read_text()
        updated_content = content.replace('pkgver=', f'pkgver={new_version}')
        pkgbuild_path.write_text(updated_content)

    def build_package(self, pkg_info: Dict[str, List[str]]) -> bool:
        if self.config.build_mode not in ['build', 'test']:
            return True

        try:
            # Build package
            self.subprocess_runner.run_command(['makepkg', '-s', '--noconfirm'])
            
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
            return False

        try:
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
                if not self.authenticate_github():
                    raise Exception("GitHub authentication failed")

            self.setup_build_environment()
            self.collect_package_files()
            
            pkg_info = self.process_dependencies()
            print(f"[debug] PACKAGE INFO--> {pkg_info}")
            self.check_version_update()
            
            if not self.build_package(pkg_info):
                raise Exception("Package build failed")
                
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