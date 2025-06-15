#!/usr/bin/env python3
import os
from pathlib import Path
from shutil import copy2, copytree, rmtree
from subprocess import CalledProcessError, check_call
from datetime import datetime
import sys
import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Confirm, Prompt
from rich.table import Table
from typing import Optional
import re
import json
import subprocess
import yaml
from dataclasses import dataclass, asdict

app = typer.Typer()
console = Console(stderr=True, style="bold")

@dataclass
class Config:
    maintainer_name: Optional[str] = None
    nvchecker_keyfile: Optional[Path] = None
    nvchecker_config: Optional[Path] = None

@dataclass
class VersionInfo:
    current_version: Optional[str] = None
    new_version: Optional[str] = None
    pkgrel_reset: bool = False
    version_updated: bool = False

class PKGBUILDError(Exception):
    """Custom exception for PKGBUILD-related errors."""
    pass

def get_config_dir() -> Path:
    try:
        from xdg.BaseDirectory import xdg_config_home
        config_home = xdg_config_home
    except ImportError:
        config_home = Path.home() / ".config"

    config_dir = Path(config_home) / "aurclone"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir

def get_config_file() -> Path:
    return get_config_dir() / "aurclone.yml"

def load_config() -> Config:
    config_file = get_config_file()
    if config_file.exists():
        with open(config_file, 'r') as f:
            data = yaml.safe_load(f)
            if data:
                return Config(**data)
    return Config()

def save_config(config: Config) -> None:
    config_file = get_config_file()
    config_dict = asdict(config)
    # Convert Path objects to strings for YAML serialization
    for key, value in config_dict.items():
        if isinstance(value, Path):
            config_dict[key] = str(value)
    with open(config_file, 'w') as f:
        yaml.safe_dump(config_dict, f)

def prompt_for_missing_config(config: Config) -> Config:
    updated = False
    if not config.maintainer_name:
        config.maintainer_name = Prompt.ask("[bold yellow]Enter your maintainer name[/bold yellow]")
        updated = True
    if not config.nvchecker_keyfile:
        config.nvchecker_keyfile = Path(Prompt.ask("[bold yellow]Enter path to nvchecker keyfile[/bold yellow]"))
        updated = True

    if updated:
        save_config(config)
        console.print("[green]Configuration saved![/green]")
    return config

def _read_pkgbuild_field(pkgbuild_path: Path, field: str) -> Optional[str]:
    """Extract a field value from PKGBUILD file."""
    try:
        with open(pkgbuild_path, 'r') as file:
            for line in file:
                if line.startswith(f"{field}="):
                    return line.split("=", 1)[1].strip()
    except FileNotFoundError:
        raise PKGBUILDError(f"PKGBUILD file not found: {pkgbuild_path}")
    return None

def _get_current_version(pkgbuild_path: Path) -> Optional[str]:
    """Extract the current pkgver from PKGBUILD file."""
    return _read_pkgbuild_field(pkgbuild_path, "pkgver")

def _get_current_pkgrel(pkgbuild_path: Path) -> Optional[str]:
    """Extract the current pkgrel from PKGBUILD file."""
    return _read_pkgbuild_field(pkgbuild_path, "pkgrel")

def _update_pkgbuild_field(pkgbuild_path: Path, field: str, value: str) -> None:
    """Updates the first occurrence of a field in the PKGBUILD file."""
    try:
        with open(pkgbuild_path, 'r') as file:
            content = file.readlines()
    except FileNotFoundError:
        raise PKGBUILDError(f"PKGBUILD file not found: {pkgbuild_path}")

    # Replace the first occurrence of the field
    for i, line in enumerate(content):
        if line.startswith(f"{field}="):
            content[i] = f"{field}={value}\n"
            break

    # Write changes back to PKGBUILD
    with open(pkgbuild_path, 'w') as file:
        file.writelines(content)

def _update_pkgbuild_maintainer_info(pkgbuild_path: Path, maintainerstring: str) -> None:
    """
    Updates the PKGBUILD file:
    - Ensures "# Maintainer: {maintainerstring}" is present at the top.
    - Converts other "# Maintainer:" lines to "# Contributor:".
    - Replaces "#vim:set" lines with "# vim:set ts=2 sw=2 et:".
    """
    shellcheck_line = "# shellcheck shell=bash disable=SC2034,SC2154"

    try:
        with open(pkgbuild_path, 'r') as file:
            content = file.readlines()
    except FileNotFoundError:
        raise PKGBUILDError(f"PKGBUILD file not found: {pkgbuild_path}")

    # Remove any existing # shellcheck lines
    content = [line for line in content if not line.startswith("# shellcheck")]

    # Ensure "# Maintainer: {maintainerstring}" is at the top
    if not any(line.startswith(f"# Maintainer: {maintainerstring}") for line in content):
        content.insert(0, f"# Maintainer: {maintainerstring}\n")

    # Replace other "# Maintainer:" with "# Contributor:"
    for i, line in enumerate(content):
        if line.startswith("# Maintainer:") and not line.startswith(f"# Maintainer: {maintainerstring}"):
            content[i] = line.replace("# Maintainer:", "# Contributor:", 1)

    # Find the last occurrence of a Maintainer/Contributor line to insert the shellcheck line
    last_maintainer_contributor_index = -1
    for i, line in enumerate(content):
        if line.startswith(("# Maintainer:", "# Contributor:")):
            last_maintainer_contributor_index = i

    if last_maintainer_contributor_index >= 0:
        content.insert(last_maintainer_contributor_index + 1, shellcheck_line + "\n")

    # Replace "#vim:set" lines with corrected format
    content = [re.sub(r"^#vim:set.*", "# vim:set ts=2 sw=2 et:", line) for line in content]
    if not any(re.match(r"^# vim:set ts=2 sw=2 et:", line) for line in content):
        content.append("# vim:set ts=2 sw=2 et:\n")

    # Write changes back to PKGBUILD
    with open(pkgbuild_path, 'w') as file:
        file.writelines(content)

def _has_nvchecker_config(pkg_dir: Path) -> bool:
    """Check if .nvchecker.toml exists in the package directory."""
    return (pkg_dir / ".nvchecker.toml").exists()

def _get_nvchecker_version(pkg_name: str, pkg_dir: Path, config: Config) -> str:
    """
    Runs nvchecker to fetch the latest version of the package from stdout.
    Parses the JSON output to extract the version for the specified package.
    """
    nvchecker_command = ["nvchecker", "-c", ".nvchecker.toml"]
    if config.nvchecker_keyfile:
        nvchecker_command.extend(["-k", str(Path(config.nvchecker_keyfile).expanduser())])
    nvchecker_command.extend(["--logger", "json"])

    try:
        result = subprocess.run(
            nvchecker_command,
            capture_output=True,
            text=True,
            check=True,
            cwd=pkg_dir,
        )
    except subprocess.CalledProcessError as e:
        raise PKGBUILDError(f"nvchecker failed: {e.stderr.strip()}")
    except FileNotFoundError:
        raise PKGBUILDError("nvchecker not found. Please install nvchecker.")

    # Parse JSON output to find the version
    for line in result.stdout.splitlines():
        try:
            log_entry = json.loads(line.strip())
            if log_entry.get("logger_name") == "nvchecker.core" and log_entry.get("name") == pkg_name:
                return log_entry.get("version")
        except json.JSONDecodeError:
            continue

    raise PKGBUILDError(f"Version for package '{pkg_name}' not found in nvchecker output.")

def _update_checksums(pkg_dir: Path) -> None:
    """Updates all checksums in the PKGBUILD using updpkgsums."""
    try:
        subprocess.run(["updpkgsums"], check=True, capture_output=True, cwd=pkg_dir)
    except subprocess.CalledProcessError as e:
        raise PKGBUILDError(f"updpkgsums failed: {e.stderr.decode().strip()}")
    except FileNotFoundError:
        raise PKGBUILDError("updpkgsums not found. Please install pacman-contrib package.")

def _validate_pkgbuild(pkgbuild_path: Path) -> None:
    """
    Validates the PKGBUILD file:
    - Formats with shfmt.
    - Creates a backup of the original file.
    - Runs shellcheck and namcap for syntax and linting.
    """
    try:
        # Format with shfmt
        formatted_path = pkgbuild_path.with_suffix('.new')
        with open(formatted_path, 'w') as f:
            subprocess.run(["shfmt", "-i", "2", pkgbuild_path], stdout=f, check=True)

        # Backup original PKGBUILD
        backup_path = Path(f"{pkgbuild_path}.bak")
        count = 1
        while backup_path.exists():
            backup_path = Path(f"{pkgbuild_path}.bak_{count}")
            count += 1

        pkgbuild_path.rename(backup_path)
        formatted_path.rename(pkgbuild_path)

        console.print(f"[green]✓[/green] Backup created: {backup_path.name}")

        # Run shellcheck
        subprocess.run(["shellcheck", "-S", "error", pkgbuild_path], check=True)
        console.print("[green]✓[/green] Shellcheck validation passed")

        # Run namcap
        subprocess.run(["namcap", pkgbuild_path], check=True)
        console.print("[green]✓[/green] Namcap validation passed")

    except subprocess.CalledProcessError as e:
        raise PKGBUILDError(f"Validation failed: {e}")
    except FileNotFoundError as e:
        raise PKGBUILDError(f"Required tool not found: {e}")

def _process_version_update(pkgname: str, pkgbuild_path: Path, pkg_dir: Path, config: Config, progress: Progress) -> VersionInfo:
    """Handle version checking and updating logic."""
    version_info = VersionInfo()
    has_nvchecker = _has_nvchecker_config(pkg_dir)

    if not has_nvchecker:
        console.print("[yellow]ℹ[/yellow] No .nvchecker.toml found - skipping version update")
        version_info.current_version = _get_current_version(pkgbuild_path)
        return version_info

    # Fetch and update version
    task_version = progress.add_task(f"[cyan]Fetching latest version for {pkgname}...", total=None)
    version_info.current_version = _get_current_version(pkgbuild_path)
    version_info.new_version = _get_nvchecker_version(pkgname, pkg_dir, config)

    if version_info.current_version and version_info.current_version == version_info.new_version:
        progress.update(task_version, description=f"[yellow]ℹ[/yellow] Package is already at latest version: {version_info.new_version}")
    elif version_info.current_version and version_info.current_version != version_info.new_version:
        progress.update(task_version, description=f"[green]✓[/green] Version updated from {version_info.current_version} to {version_info.new_version}")
        _update_pkgbuild_field(pkgbuild_path, "pkgrel", "1")
        version_info.pkgrel_reset = True
        version_info.version_updated = True
        console.print("[green]✓[/green] Package release reset to 1")
    else:
        progress.update(task_version, description=f"[green]✓[/green] Version set to: {version_info.new_version}")
        version_info.version_updated = True

    if version_info.new_version:
        _update_pkgbuild_field(pkgbuild_path, "pkgver", version_info.new_version)
        console.print("[green]✓[/green] PKGBUILD version updated")

    progress.remove_task(task_version)
    return version_info

def _process_validation_and_checksums(pkg_dir: Path, pkgbuild_path: Path, has_nvchecker: bool, is_interactive: bool) -> None:
    """Handle validation and checksum update logic."""
    should_update_checksums = has_nvchecker and (
        not is_interactive or
        Confirm.ask("[yellow]Confirm PKGBUILD and update shasums?[/yellow]", console=console)
    )

    if not should_update_checksums:
        if has_nvchecker:
            console.print("[yellow]Validation and checksum update skipped by user.[/yellow]")
        else:
            console.print("[yellow]Validation and checksum update skipped - no nvchecker config found.[/yellow]")
        return

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        task_checksums = progress.add_task("[cyan]Updating package checksums...", total=None)
        _update_checksums(pkg_dir)
        progress.update(task_checksums, description="[green]✓[/green] Package checksums updated")
        progress.remove_task(task_checksums)

        task_validate = progress.add_task("[cyan]Validating PKGBUILD...", total=None)
        _validate_pkgbuild(pkgbuild_path)
        progress.update(task_validate, description="[green]✓[/green] PKGBUILD validation completed")
        progress.remove_task(task_validate)

def _display_summary_table(maintainer: str, package: str, version_info: VersionInfo) -> None:
    """Display a summary table of the operation."""
    table = Table(title="PKGBUILD Update Summary", show_header=True, header_style="bold bright_blue")
    table.add_column("Property", style="bright_cyan", no_wrap=True)
    table.add_column("Value", style="green")

    table.add_row("Maintainer", maintainer)
    table.add_row("Package", package)

    if version_info.current_version and version_info.new_version:
        if version_info.current_version != version_info.new_version:
            table.add_row("Version Update", f"{version_info.current_version} → {version_info.new_version}")
            if version_info.pkgrel_reset:
                table.add_row("Package Release", "Reset to 1")
        else:
            table.add_row("Version", f"{version_info.new_version} (no change)")
    elif version_info.new_version:
        table.add_row("Version", version_info.new_version)
    elif version_info.current_version:
        table.add_row("Version", f"{version_info.current_version} (no nvchecker config)")

    console.print(table)

def get_temp_dir(pkgname: str) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return Path(f"/Downloads/{timestamp}_{pkgname}")

def check_existing_tmp(pkgname: str) -> bool:
    return any(p.name.endswith(f"_{pkgname}") for p in Path("/Downloads").iterdir())

def remove_existing_tmp(pkgname: str):
    for p in Path("/Downloads").iterdir():
        if p.name.endswith(f"_{pkgname}"):
            rmtree(p)
            console.log(f"Removed existing directory: {p}")
            return

def clone_repo(pkgname: str, target_dir: Path) -> bool:
    try:
        repo_url = f"ssh://aur@aur.archlinux.org/{pkgname}.git"
        console.log(f"Cloning from {repo_url}")
        check_call(["git", "clone", repo_url], cwd=target_dir)
        console.log(f"[green]Repository cloned into {target_dir}[/green]")
        return True
    except CalledProcessError as e:
        console.print(f"[bold red]Error cloning repository:[/bold red] {e}")
        return False

def copy_cached_files(pkgname: str, dest_dir: Path):
    cache_dir = Path.home() / "github/envolution/aur/maintain/build" / pkgname
    if not cache_dir.is_dir():
        return

    console.print(f"[bold yellow]Cached GitHub version found at {cache_dir}[/bold yellow]")

    for item in cache_dir.iterdir():
        dest = dest_dir / item.name
        if item.is_dir():
            copytree(item, dest, dirs_exist_ok=True)
        else:
            copy2(item, dest)
    console.log("Cached files copied to cloned directory")

def _handle_existing_temp_directory(pkgname: str, force: bool, is_interactive: bool) -> None:
    """Handle existing temporary directories with proper interactive/non-interactive logic."""
    if not check_existing_tmp(pkgname):
        return

    if force:
        remove_existing_tmp(pkgname)
        return

    if not is_interactive:
        console.print(f"[bold red]Temporary clone for '{pkgname}' already exists. Aborting clone as not in interactive mode.[/bold red]")
        raise typer.Exit(1)

    if not Confirm.ask(f"[yellow]Temporary clone for '{pkgname}' already exists. Overwrite?[/yellow]"):
        console.print("[bold red]Aborting clone.[/bold red]")
        raise typer.Exit(1)
    else:
        remove_existing_tmp(pkgname)

@app.command()
def aurclone(pkgname: str, force: bool = False,
             maintainer_name: Optional[str] = typer.Option(None, "--maintainer-name", "-m", help="Override maintainer name from config."),
             nvchecker_keyfile: Optional[Path] = typer.Option(None, "--nvchecker-keyfile", help="Override nvchecker keyfile path from config."),
             nvchecker_config: Optional[Path] = typer.Option(None, "--nvchecker-config", help="Override nvchecker config path from config.")):
    """Clone an AUR package into a temporary directory, restoring from cache if available."""

    # Load and prompt for config
    config = load_config()
    config = prompt_for_missing_config(config)

    # Apply CLI overrides
    if maintainer_name:
        config.maintainer_name = maintainer_name
    if nvchecker_keyfile:
        config.nvchecker_keyfile = nvchecker_keyfile
    if nvchecker_config:
        config.nvchecker_config = nvchecker_config

    if not config.maintainer_name:
        console.print("[bold red]Error: Maintainer name is not set. Please configure it via prompt or --maintainer-name flag.[/bold red]")
        raise typer.Exit(1)

    is_interactive = sys.stdin.isatty()

    # Handle existing temporary directories
    _handle_existing_temp_directory(pkgname, force, is_interactive)

    temp_dir = get_temp_dir(pkgname)
    pkg_dir = temp_dir / pkgname
    temp_dir.mkdir(parents=True, exist_ok=True)

    if not clone_repo(pkgname, temp_dir):
        raise typer.Exit(1)

    if not pkg_dir.is_dir():
        console.print(f"[bold red]Error:[/bold red] Expected directory '{pkg_dir}' not found after cloning.")
        raise typer.Exit(1)

    copy_cached_files(pkgname, pkg_dir)
    console.log(f"[green]Changed into directory:[/green] {pkg_dir}")

    # PKGBUILD update process starts here
    pkgbuild_path = Path("PKGBUILD")
    maintainerstring = config.maintainer_name
    original_cwd = os.getcwd()

    try:
        os.chdir(pkg_dir)
        has_nvchecker = _has_nvchecker_config(pkg_dir)

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            transient=True,
        ) as progress:
            # Update PKGBUILD maintainer info
            task_maintainer = progress.add_task("[cyan]Updating PKGBUILD maintainer info...", total=None)
            _update_pkgbuild_maintainer_info(pkgbuild_path, maintainerstring)
            progress.update(task_maintainer, description="[green]✓[/green] PKGBUILD maintainer info updated")
            progress.remove_task(task_maintainer)

            # Process version updates (only if nvchecker config exists)
            version_info = _process_version_update(pkgname, pkgbuild_path, pkg_dir, config, progress)

        # Validation and checksum update (only if nvchecker config exists)
        _process_validation_and_checksums(pkg_dir, pkgbuild_path, has_nvchecker, is_interactive)

        _display_summary_table(maintainerstring, pkgname, version_info)

        console.print(Panel.fit(
            "[bold green]✨ AUR clone and PKGBUILD update completed successfully! ✨[/bold green]",
            border_style="green"
        ))

    except PKGBUILDError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(1)
    except KeyboardInterrupt:
        console.print("\n[yellow]Operation cancelled by user.[/yellow]")
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"[bold red]Unexpected error:[/bold red] {e}")
        raise typer.Exit(1)
    finally:
        os.chdir(original_cwd)

    # Write the directory to ~/.config/aurclone/tmppath.txt
    tmppath_file = get_config_dir() / "tmppath.txt"
    with open(tmppath_file, "w") as f:
        f.write(str(pkg_dir))
    console.log(f"[green]Temporary path written to:[/green] {tmppath_file}")

if __name__ == "__main__":
    app()
