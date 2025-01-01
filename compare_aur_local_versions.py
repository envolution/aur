#!/usr/bin/env python3
import argparse
import json
import logging
import os
import re
import tempfile
import subprocess
from pathlib import Path
from typing import Dict, Set, Tuple, List
import requests
from packaging import version

def setup_logging() -> None:
    """Configure logging with appropriate format and level."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description='Compare AUR packages with local workspace versions')
    parser.add_argument('--maintainer', required=True, help='AUR maintainer username')
    parser.add_argument('--repo-root', required=True, help='GitHub workspace root directory')
    parser.add_argument('--debug', action='store_true', help='Enable debug logging')
    return parser.parse_args()

def get_absolute_path(path: str) -> Path:
    """Convert path to absolute path and verify it exists."""
    abs_path = Path(path).resolve()
    if not abs_path.exists():
        raise FileNotFoundError(f"Repository path does not exist: {abs_path}")
    return abs_path

def fetch_aur_packages(maintainer: str) -> Dict:
    """Fetch package information from AUR API."""
    url = f"https://aur.archlinux.org/rpc/v5/search/{maintainer}?by=maintainer"
    logging.info(f"Fetching AUR packages for maintainer: {maintainer}")
    
    response = requests.get(url)
    response.raise_for_status()
    
    results = response.json()['results']
    return {
        pkg['PackageBase']: {
            'version': re.sub(r'^[0-9]+:', '', re.sub(r'-[0-9]+$', '', pkg['Version'])),
            'release': re.search(r'-([0-9]+)$', pkg['Version']).group(1)
        }
        for pkg in results
    }

import tempfile

def source_pkgbuilds(pkgbuild_paths: List[Path]) -> Dict:
    """Source multiple PKGBUILDs in a single bash subprocess and return evaluated variables."""
    bash_command = """
#!/usr/bin/env bash
shopt -s lastpipe

declare -A processed_pkgbuilds

process_pkgbuild() {
    local pkgbuild=$1
    unset pkgname pkgver pkgrel

    if ! source "$pkgbuild" 2>/dev/null; then
        echo "Error sourcing $pkgbuild" >&2
        return 1
    fi

    if  [[ -n $pkgbase ]]; then
        local name=$pkgbase
    elif [[ "$(declare -p pkgname 2>/dev/null)" =~ "declare -a" ]]; then
        local name=${pkgname[0]}
    else
        local name=$pkgname
    fi

    if [[ -n $name && -n $pkgver && -n $pkgrel ]]; then
        processed_pkgbuilds["$name"]=$(printf '{"pkgbuildversion": "%s", "pkgbuildrel": "%s"}' "$pkgver" "$pkgrel")
    else
        echo "Warning: Missing variables in $pkgbuild" >&2
    fi
}

# Process each PKGBUILD from the file list
while read -r pkgbuild; do
    process_pkgbuild "$pkgbuild"
done

# Output JSON
echo "{"
first=true
for key in "${!processed_pkgbuilds[@]}"; do
    if [ "$first" = true ]; then
        first=false
    else
        echo ","
    fi
    printf '  "%s": %s' "$key" "${processed_pkgbuilds[$key]}"
done
echo
echo "}"
"""

    try:
        # Write the script to a temporary file
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as tmp_script:
            tmp_script.write(bash_command)
            tmp_script_path = tmp_script.name

        # Create file list input
        pkgbuild_list = '\n'.join(str(path) for path in pkgbuild_paths)

        # Run the temporary script
        result = subprocess.run(
            ['bash', tmp_script_path],
            input=pkgbuild_list,
            text=True,
            capture_output=True
        )

        # Log stderr for debugging
        if result.stderr:
            logging.debug(f"Bash stderr: {result.stderr}")

        # Parse the output
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as e:
            logging.error(f"Failed to parse JSON output: {e}")
            logging.debug(f"Raw output: {result.stdout}")
            return {}

    finally:
        # Clean up the temporary file
        os.remove(tmp_script_path)


def get_workspace_packages(repo_root: Path) -> Dict:
    """Get package information from workspace PKGBUILDs."""
    logging.info(f"Scanning workspace PKGBUILDs in: {repo_root}")
    pkgbuild_paths = list(repo_root.rglob('PKGBUILD'))
    logging.debug(f"Found {len(pkgbuild_paths)} PKGBUILDs")
    
    return source_pkgbuilds(pkgbuild_paths)

def compare_versions(ver1: str, ver2: str) -> str:
    """Compare two version strings and return relationship."""
    try:
        v1 = version.parse(ver1)
        v2 = version.parse(ver2)
        if v1 > v2:
            return "downgrade"
        elif v1 < v2:
            return "upgrade"
        return "same"
    except Exception as e:
        logging.error(f"Error comparing versions {ver1} vs {ver2}: {str(e)}")
        return "unknown"

def analyze_packages(aur_packages: Dict, workspace_packages: Dict) -> Tuple[Set, Set, Dict]:
    """Analyze package differences between AUR and workspace."""
    aur_only = set(aur_packages.keys()) - set(workspace_packages.keys())
    workspace_only = set(workspace_packages.keys()) - set(aur_packages.keys())
    
    version_differences = {}
    common_packages = set(aur_packages.keys()) & set(workspace_packages.keys())
    
    for pkg in common_packages:
        aur_ver = aur_packages[pkg]['version']
        aur_rel = aur_packages[pkg]['release']
        workspace_ver = workspace_packages[pkg]['pkgbuildversion']
        workspace_rel = workspace_packages[pkg]['pkgbuildrel']
        #
        # Concatenate version and release to form full version strings
        aur_full_version = f"{aur_ver}-{aur_rel}"
        workspace_full_version = f"{workspace_ver}-{workspace_rel}" 

        if aur_full_version != workspace_full_version:
            version_differences[pkg] = {
                'aur_version': aur_full_version,
                'workspace_version': workspace_full_version,
                'status': compare_versions(aur_full_version, workspace_full_version)
            }
    
    return aur_only, workspace_only, version_differences

def main():
    """Main execution function."""
    args = parse_args()
    
    # Setup logging with appropriate level
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    
    try:
        repo_root = get_absolute_path(args.repo_root)
        
        # Fetch and parse package information
        aur_packages = fetch_aur_packages(args.maintainer)
        workspace_packages = get_workspace_packages(repo_root)
        
        # Analyze differences
        aur_only, workspace_only, version_differences = analyze_packages(
            aur_packages, workspace_packages
        )
        
        # Output results
        print("\n=== Package Analysis Results ===")
        
        print("\nPackages only in AUR:")
        for pkg in sorted(aur_only):
            print(f"  - {pkg} ({aur_packages[pkg]['version']}-{aur_packages[pkg]['release']})")
        
        print("\nPackages only in workspace:")
        for pkg in sorted(workspace_only):
            print(f"  - {pkg} ({workspace_packages[pkg]['pkgbuildversion']}-{workspace_packages[pkg]['pkgbuildrel']})")
        
        print("\nPackages with version differences:")
        for pkg, diff in sorted(version_differences.items()):
            print(f"  - {pkg}:")
            print(f"    AUR: {diff['aur_version']}")
            print(f"    Workspace: {diff['workspace_version']}")
            print(f"    Status: {diff['status'].upper()}")
        
        # Initialize all the data dictionaries with the same starting value
        combined_data = {"version": 2, "data": {}}
        aur_data = {"version": 2, "data": {}}
        local_data = {"version": 2, "data": {}}

        # Iterate through AUR packages and check if they exist in the workspace
        for pkg, aur_pkg_data in aur_packages.items():
            if pkg in workspace_packages:
                # If the package exists in both AUR and the workspace
                workspace_pkg_data = workspace_packages[pkg]
                combined_data["data"][pkg] = {
                    "version": aur_pkg_data.get("version"),
                    "release": aur_pkg_data.get("release"),
                    "pkgbuildversion": workspace_pkg_data.get("pkgbuildversion"),
                    "pkgbuildrel": workspace_pkg_data.get("pkgbuildrel"),
                }
                
                # Store AUR-specific data in aur_data
                aur_data["data"][pkg] = {
                    "version": aur_pkg_data.get("version"),
                }
                
                # Store workspace-specific data in local_data
                local_data["data"][pkg] = {
                    "version": workspace_pkg_data.get("pkgbuildversion"),
                }

        # Write the combined data (AUR + workspace) to combined.json
        with open('combined.json', 'w') as f:
            json.dump(combined_data, f, indent=2)

        # Write the AUR data to aur.json (packages only in AUR)
        with open('aur.json', 'w') as f:
            json.dump(aur_data, f, indent=2)

        # Write the workspace data to local.json (packages only in the workspace)
        with open('local.json', 'w') as f:
            json.dump(local_data, f, indent=2)   
        
        # Write the original version_differences (with full details) to 'changes.json'
        with open('changes.json', 'w') as f:
            json.dump(version_differences, f, indent=2)

        logging.info("Analysis complete. Results saved to combined.json")
        
    except Exception as e:
        logging.error(f"Error during execution: {str(e)}")
        raise

if __name__ == "__main__":
    main()
