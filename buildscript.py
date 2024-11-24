#!/usr/bin/env python3

import argparse
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Dict, Optional, Any, Union
import base64
import shutil
import tempfile

@dataclass
class BuildConfig:
    github_repo: str
    github_token: str
    github_workspace: str
    package_name: str
    pkgbuild_path: str
    commit_message: str
    build_mode: str = "none"  # Options: none, build, test
    debug: bool = False

@dataclass
class BuildResult:
    success: bool
    package_name: str
    version: Optional[str] = None
    built_packages: List[str] = None
    error_message: Optional[str] = None
    changes_detected: bool = False
    initial_commit: bool = False

class ArchPackageBuilder:
    RELEASE_BODY = "To install, run: sudo pacman -U PACKAGENAME.pkg.tar.zst"

    def __init__(self, config: BuildConfig):
        self.config = config
        self.logger = self._setup_logger()
        self.build_dir = Path(tempfile.mkdtemp(prefix=f"build-{config.package_name}-"))
        self.tracked_files: List[str] = []
        self.result = BuildResult(success=True, package_name=config.package_name)

    def _setup_logger(self) -> logging.Logger:
        logger = logging.getLogger("arch_builder")
        handler = logging.StreamHandler()
        formatter = logging.Formatter('[%(levelname)s] %(message)s')
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG if self.config.debug else logging.INFO)
        return logger

    def _run_command(self, cmd: List[str], check: bool = True, input_data: Optional[str] = None) -> subprocess.CompletedProcess:
        """Run a command with optional input data and stream output to the main thread."""
        self.logger.debug(f"Running command: {' '.join(cmd)}")
        try:
            with subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.PIPE if input_data else None,
                text=True,
            ) as proc:
                stdout, stderr = proc.communicate(input=input_data)
                if check and proc.returncode != 0:
                    raise subprocess.CalledProcessError(proc.returncode, cmd, output=stdout, stderr=stderr)
                return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
        except subprocess.CalledProcessError as e:
            self.logger.error(f"Command failed: {e.stderr}")
            raise

            
            # Send input_data to the subprocess via stdin
            if input_data:
                process.stdin.write(input_data)
                process.stdin.close()  # Close stdin after writing

            # Stream stdout and stderr to the main process's stdout and stderr
            for line in process.stdout:
                self.logger.debug(line.strip())  # Log stdout line

            for line in process.stderr:
                self.logger.error(line.strip())  # Log stderr line

            # Wait for the process to complete
            process.wait()

            # Handle the return code
            if process.returncode != 0 and check:
                raise subprocess.CalledProcessError(process.returncode, cmd)

            return process
    
        except subprocess.CalledProcessError as e:
            self.logger.error(f"Command failed with return code {e.returncode}")
            self.logger.error(f"stdout: {e.output}")
            self.logger.error(f"stderr: {e.stderr}")
            raise

    def authenticate_github(self) -> bool:
        """Authenticate with GitHub using provided token."""
        try:
            # Write token to a temporary file to avoid command line exposure
            token_file = self.build_dir / '.github_token'
            token_file.write_text(self.config.github_token)
            
            _token = open(token_file).read().strip()
            self._run_command(['gh', 'auth', 'login', '--with-token'], input_data=_token)
            token_file.unlink()  
            del _token
            
            self._run_command(['gh', 'auth', 'status'])
            return True
        except subprocess.CalledProcessError:
            self.logger.error("GitHub authentication failed")
            return False
        except Exception as e:
            self.logger.error(f"Authentication error: {str(e)}")
            return False

    def setup_build_environment(self):
        """Initialize build environment and clone AUR repository."""
        aur_repo = f"ssh://aur@aur.archlinux.org/{self.config.package_name}.git"
        try:
            self._run_command(['git', 'clone', aur_repo, str(self.build_dir)])
            os.chdir(self.build_dir)
        except subprocess.CalledProcessError as e:
            raise Exception(f"Failed to clone AUR repository: {e.stderr}")

    def collect_package_files(self):
        """Collect PKGBUILD and related files from workspace."""
        workspace_path = Path(self.config.github_workspace) / self.config.pkgbuild_path
        
        for file in ['PKGBUILD', '.nvchecker.toml']:
            source_file = workspace_path / file
            if source_file.is_file():
                try:
                    shutil.copy2(source_file, self.build_dir / file)
                    self.tracked_files.append(file)
                except Exception as e:
                    self.logger.warning(f"Failed to copy {file}: {e}")

    def parse_pkgbuild(self) -> Dict[str, List[str]]:
        """Parse PKGBUILD file to extract package information."""
        parse_script = '''
            source PKGBUILD
            printf "%s\n" "${depends[@]}"
            printf "===SEPARATOR===\n"
            printf "%s\n" "${makedepends[@]}"
            printf "===SEPARATOR===\n"
            printf "%s\n" "${checkdepends[@]}"
            printf "===SEPARATOR===\n"
            printf "%s\n" "${validpgpkeys[@]}"
            printf "===SEPARATOR===\n"
            printf "%s\n" "${pkgname[@]}"            
        '''

        try:
            # Run the bash script and capture stdout and stderr for debugging
            result = self._run_command(['bash', '-c', parse_script])

            # Log the raw PKGBUILD output and any error
            self.logger.debug(f"Raw PKGBUILD output: {result.stdout}")
            self.logger.debug(f"Raw PKGBUILD error: {result.stderr}")

            # Split by separator and process the sections
            sections = result.stdout.split("===SEPARATOR===")

            # Log the number of sections and the sections themselves for debugging
            self.logger.debug(f"Number of sections: {len(sections)}")
            self.logger.debug(f"Sections: {sections}")

            if len(sections) < 6:
                self.logger.warning(f"Unexpected number of sections: {len(sections)}")
                return {}

            # Return the parsed dependencies and other fields
            return {
                'depends': [s for s in sections[0].splitlines() if s],
                'makedepends': [s for s in sections[1].splitlines() if s],
                'checkdepends': [s for s in sections[2].splitlines() if s],
                'pgpkeys': [s for s in sections[3].splitlines() if s],
                'packages': [s for s in sections[4].splitlines() if s]
            }

        except (subprocess.CalledProcessError, IndexError) as e:
            self.logger.error(f"Failed to parse PKGBUILD: {str(e)}")
            return {}

    def check_version_update(self) -> Optional[str]:
        """Check for package version updates using nvchecker."""
        nvchecker_path = self.build_dir / '.nvchecker.toml'
        if not nvchecker_path.is_file():
            return None

        try:
            result = self._run_command(['nvchecker', '-c', '.nvchecker.toml', '--logger', 'json'])
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
                
        except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
            self.logger.error(f"Version check failed: {e}")
            return None

    def _update_pkgbuild_version(self, new_version: str):
        """Update PKGBUILD with new version."""
        pkgbuild_path = self.build_dir / 'PKGBUILD'
        try:
            content = pkgbuild_path.read_text()
            updated_content = content.replace(
                'pkgver=', f'pkgver={new_version}')
            pkgbuild_path.write_text(updated_content)
        except Exception as e:
            raise Exception(f"Failed to update PKGBUILD version: {str(e)}")

    def build_package(self, pkg_info: Dict[str, List[str]]) -> bool:
        """Build and optionally install the package."""
        if self.config.build_mode not in ['build', 'test']:
            return True
        
        self.logger.debug(f"pkg_info: {pkg_info}")  # Log the entire pkg_info dict

        # Install dependencies
        for dep_type in ['depends', 'makedepends', 'checkdepends']:
            if deps := pkg_info.get(dep_type):
                try:
                    # Log the command being run
                    self.logger.debug(f"Running command to install {dep_type}: paru -S --needed --norebuild --noconfirm --mflags \"--skipchecksums --skippgpcheck\" {', '.join(deps)}")
                    
                    # Run the command
                    result = self._run_command(['paru', '-S', '--needed', '--norebuild', '--noconfirm',
                                                '--mflags "--skipchecksums --skippgpcheck"', *deps])
                    
                    # Log the result of the command
                    self.logger.debug(f"Command result for {dep_type}: {result.stdout}")
                
                except subprocess.CalledProcessError as e:
                    # Log the error message from stderr if the command fails
                    self.logger.warning(f"Failed to install {dep_type}. Error: {e.stderr}")

        # Build package
        try:
            self._run_command(['makepkg', '-s', '--noconfirm'])
            
            # Install package
            packages = sorted(Path('.').glob('*.pkg.tar.zst'))
            if not packages:
                raise Exception("No packages built")
                
            self._run_command(['sudo', 'pacman', '--noconfirm', '-U', *[str(p) for p in packages]])
            self.result.built_packages = [p.name for p in packages]
            
            # Create release if not in test mode
            if self.config.build_mode == 'build':
                self._create_release(packages)
                
            return True
        except Exception as e:
            self.logger.error(f"Build failed: {e}")
            self.result.error_message = str(e)
            return False

    def _create_release(self, package_files: List[Path]):
        """Create GitHub release with built packages."""
        try:
            # Create release (ignore if already exists)
            self._run_command([
                'gh', 'release', 'create', self.config.package_name,
                '--title', f"Binary installers for {self.config.package_name}",
                '--notes', self.RELEASE_BODY,
                '-R', self.config.github_repo
            ], check=False)

            # Upload packages
            for pkg_file in package_files:
                self._run_command([
                    'gh', 'release', 'upload', self.config.package_name,
                    str(pkg_file), '--clobber',
                    '-R', self.config.github_repo
                ])
        except subprocess.CalledProcessError as e:
            self.logger.error(f"Failed to create release: {e}")

    def commit_and_push(self) -> bool:
        """Commit changes and push to AUR and GitHub."""
        try:
            if not self.tracked_files:
                self.logger.warning("No files to commit")
                return False

            self._run_command(['git', 'add', *self.tracked_files])
            
            if self._has_changes():
                commit_msg = f"{self.config.commit_message}: {self.result.version or ''}"
                self._run_command(['git', 'commit', '-m', commit_msg])
                self._run_command(['git', 'push', 'origin', 'master'])
                self.result.changes_detected = True
                
                # Update GitHub repository
                self._update_github_files()
                return True
            return False
        except subprocess.CalledProcessError as e:
            self.logger.error(f"Failed to commit and push: {e}")
            return False

    def _has_changes(self) -> bool:
        """Check if there are any changes to commit."""
        result = self._run_command(['git', 'status', '--porcelain'], check=False)
        return bool(result.stdout.strip())

    def _update_github_files(self):
        """Update files in GitHub repository."""
        for file in self.tracked_files:
            try:
                file_path = self.build_dir / file
                if not file_path.is_file():
                    continue

                with open(file_path, 'rb') as f:
                    content = base64.b64encode(f.read()).decode('utf-8')
                
                # Try to get existing file's SHA
                try:
                    response = self._run_command([
                        'gh', 'api',
                        f"/repos/{self.config.github_repo}/contents/{self.config.pkgbuild_path}/{file}",
                        '--jq', '.sha'
                    ])
                    sha = response.stdout.strip()
                except subprocess.CalledProcessError:
                    sha = None

                # Update or create file
                if sha:
                    self._run_command([
                        'gh', 'api', '-X', 'PUT',
                        f"/repos/{self.config.github_repo}/contents/{self.config.pkgbuild_path}/{file}",
                        '-f', f"message=Auto updated {file}",
                        '-f', f"content={content}",
                        '-f', f"sha={sha}"
                    ])
                else:
                    self._run_command([
                        'gh', 'api', '-X', 'PUT',
                        f"/repos/{self.config.github_repo}/contents/{self.config.pkgbuild_path}/{file}",
                        '-f', f"message=Added {file}",
                        '-f', f"content={content}"
                    ])
            except subprocess.CalledProcessError as e:
                self.logger.error(f"Failed to update {file} on GitHub: {e}")

    def cleanup(self):
        """Clean up temporary build directory."""
        try:
            shutil.rmtree(self.build_dir)
        except Exception as e:
            self.logger.warning(f"Failed to clean up build directory: {e}")

    def run(self) -> Dict[str, Any]:
        """Run the complete build process."""
        try:
            try:
                result = self._run_command(['gh', 'auth', 'status'])
                # If no exception was raised, the user is authenticated
                self.logger.info("User is authenticated.")
                is_authenticated = True
            except subprocess.CalledProcessError as e:
                # If the command fails, it means the user is not authenticated
                if e.returncode == 1:
                    self.logger.info("User is not authenticated.")
                    is_authenticated = False
                else:
                    self.logger.error(f"Unexpected error during authentication check: {e}")
                    is_authenticated = None

            if is_authenticated is not True:
                if not self.authenticate_github():
                    raise Exception("GitHub authentication failed")

            self.setup_build_environment()
            self.collect_package_files()
            
            pkg_info = self.parse_pkgbuild()
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
    parser.add_argument('--pkgbuild-path', required=True, help='Path to PKGBUILD directory')
    parser.add_argument('--commit-message', required=True, help='Git commit message')
    parser.add_argument('--build-mode', choices=['none', 'build', 'test'], default='none',
                       help='Build mode: none, build, or test')
    parser.add_argument('--debug', action='store_true', help='Enable debug logging')

    args = parser.parse_args()
    
    config = BuildConfig(
        github_repo=args.github_repo,
        github_token=args.github_token,
        github_workspace=args.github_workspace,
        package_name=args.package_name,
        pkgbuild_path=args.pkgbuild_path,
        commit_message=args.commit_message,
        build_mode=args.build_mode,
        debug=args.debug
    )
    
    builder = ArchPackageBuilder(config)
    result = builder.run()
    
    print(json.dumps(result, indent=2))
    sys.exit(0 if result['success'] else 1)

if __name__ == '__main__':
    main()