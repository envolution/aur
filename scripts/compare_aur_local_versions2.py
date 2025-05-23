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
    unset pkgname pkgver pkgrel pkgbase

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

# In compare_aur_local_versions.py

# ... (keep existing imports and functions up to analyze_packages) ...

def main():
    """Main execution function."""
    args = parse_args()
    
    # Setup logging with appropriate level
    # Ensure this basicConfig is called only once, ideally at the start if not already.
    # If setup_logging() is preferred, ensure it's called.
    # For simplicity, let's ensure it's configured here.
    log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        # force=True # Add if re-configuring logging is problematic
    )
    
    try:
        repo_root = get_absolute_path(args.repo_root)
        
        logging.info(f"Fetching AUR packages for maintainer: {args.maintainer}")
        aur_packages_raw = fetch_aur_packages(args.maintainer) # This is {pkgbase: {version: X, release: Y}}
        
        logging.info(f"Scanning workspace PKGBUILDs in: {repo_root}")
        workspace_packages_raw = get_workspace_packages(repo_root) # This is {name: {pkgbuildversion: A, pkgbuildrel: B}}
        
        # --- Prepare data for nvchecker compatible JSON files ---
        aur_json_data = {}
        for pkg_base, data in aur_packages_raw.items():
            aur_json_data[pkg_base] = f"{data['version']}-{data['release']}"

        local_json_data = {}
        for pkg_name, data in workspace_packages_raw.items():
            local_json_data[pkg_name] = f"{data['pkgbuildversion']}-{data['pkgbuildrel']}"

        # --- Perform analysis for the changes.json report ---
        # This part remains useful for a human-readable summary of changes.
        # We need to adapt analyze_packages if its input format assumption changed,
        # or create a new analysis function for this specific report.
        # For now, let's assume analyze_packages can work with these raw structures
        # or be slightly adapted.
        # The original analyze_packages expects:
        # aur_packages: {pkg_base: {'version': V, 'release': R}}
        # workspace_packages: {pkg_name: {'pkgbuildversion': PV, 'pkgbuildrel': PR}}
        # This matches aur_packages_raw and workspace_packages_raw.

        aur_only, workspace_only, version_differences_report = analyze_packages(
            aur_packages_raw, workspace_packages_raw
        )
        
        # --- Output human-readable analysis results to console ---
        print("\n=== Package Analysis Results ===")
        
        if aur_only:
            print("\nPackages only in AUR:")
            for pkg in sorted(aur_only):
                print(f"  - {pkg} ({aur_packages_raw[pkg]['version']}-{aur_packages_raw[pkg]['release']})")
        else:
            print("\nNo packages found only in AUR.")

        if workspace_only:
            print("\nPackages only in local workspace:")
            for pkg in sorted(workspace_only):
                # Ensure the key exists before accessing, though it should
                if pkg in workspace_packages_raw:
                    print(f"  - {pkg} ({workspace_packages_raw[pkg]['pkgbuildversion']}-{workspace_packages_raw[pkg]['pkgbuildrel']})")
                else:
                    print(f"  - {pkg} (data not found in workspace_packages_raw - check logic)")
        else:
            print("\nNo packages found only in local workspace.")

        if version_differences_report:
            print("\nPackages with version differences:")
            for pkg, diff in sorted(version_differences_report.items()):
                print(f"  - {pkg}:")
                print(f"    AUR Version:       {diff['aur_version']}")
                print(f"    Workspace Version: {diff['workspace_version']}")
                print(f"    Comparison Status: {diff['status'].upper()}") # 'status' from compare_versions
        else:
            print("\nNo version differences found for common packages.")
        
        # --- Write JSON files ---

        # aur.json (for nvchecker "oldver")
        # Format: {"package_name": "version-release", ...}
        logging.info("Writing aur.json...")
        with open('aur.json', 'w') as f:
            json.dump(aur_json_data, f, indent=2, sort_keys=True)
        logging.debug(f"aur.json content: {json.dumps(aur_json_data, indent=2, sort_keys=True)}")

        # local.json (for nvchecker "newver")
        # Format: {"package_name": "version-release", ...}
        logging.info("Writing local.json...")
        with open('local.json', 'w') as f:
            json.dump(local_json_data, f, indent=2, sort_keys=True)
        logging.debug(f"local.json content: {json.dumps(local_json_data, indent=2, sort_keys=True)}")
        
        # changes.json (detailed report of differences for human/other script consumption)
        # This uses the `version_differences_report` from `analyze_packages`.
        logging.info("Writing changes.json (detailed version differences report)...")
        changes_output_data = {
            "aur_only": sorted(list(aur_only)),
            "workspace_only": sorted(list(workspace_only)),
            "version_differences": version_differences_report 
        }
        with open('changes.json', 'w') as f:
            json.dump(changes_output_data, f, indent=2, sort_keys=True)
        logging.debug(f"changes.json content: {json.dumps(changes_output_data, indent=2, sort_keys=True)}")

        # Removing combined.json as its purpose is now split between aur.json and local.json for nvchecker,
        # and changes.json for a detailed report.

        logging.info("Analysis complete. JSON files (aur.json, local.json, changes.json) generated.")
        
    except Exception as e:
        logging.error(f"Error during execution: {str(e)}", exc_info=args.debug) # Show traceback if debug
        # Consider exiting with non-zero status on error for scripting
        sys.exit(1) 

if __name__ == "__main__":
    # It's good practice to call setup_logging once at the very beginning if it's a separate function.
    # However, since basicConfig is called in main based on args, this is fine.
    # setup_logging() # If you had a separate setup_logging() function.
    main()
