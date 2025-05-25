#!/usr/bin/env python3

import argparse
import json
import os
import subprocess
import sys
import tempfile
import glob
import logging
from packaging import version as packaging_version

# --- Global Mock Flags ---
# SET THESE TO False TO USE REAL DATA FETCHING FOR EACH SOURCE
USE_MOCK_AUR_DATA = False
USE_MOCK_LOCAL_DATA = False
USE_MOCK_NVCHECKER_OUTPUT = False

# --- Logging Setup ---
# BasicConfig is a fallback. The AurPackageUpdater class will fine-tune levels.
logging.basicConfig(format='%(levelname)s: %(name)s: %(message)s', level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("aur_updater_cli") # Main application logger

# --- Version Comparison Function ---
def compare_package_versions(base_ver1_str: str, rel1_str: str,
                             base_ver2_str: str, rel2_str: str) -> str:
    comp_logger = logging.getLogger("aur_updater_cli.compare")
    # Handle None for base versions (None is "older" than an actual version)
    if base_ver1_str is None and base_ver2_str is None: return "same"
    if base_ver1_str is None: # base_ver2_str is not None
        comp_logger.debug(f"Comparing (None, {rel1_str}) vs ({base_ver2_str}, {rel2_str}) -> upgrade (v1 base is None)")
        return "upgrade"
    if base_ver2_str is None: # base_ver1_str is not None
        comp_logger.debug(f"Comparing ({base_ver1_str}, {rel1_str}) vs (None, {rel2_str}) -> downgrade (v2 base is None)")
        return "downgrade"
    
    try: # Parse base versions
        parsed_base1 = packaging_version.parse(base_ver1_str)
        parsed_base2 = packaging_version.parse(base_ver2_str)
    except packaging_version.InvalidVersion as e:
        comp_logger.error(f"Error parsing base versions for comparison: '{base_ver1_str}' or '{base_ver2_str}': {e}")
        return "unknown"
    except Exception as e: # Should not happen with packaging.version
        comp_logger.error(f"Unexpected error parsing base versions '{base_ver1_str}', '{base_ver2_str}': {e}")
        return "unknown"

    # Compare parsed base versions
    if parsed_base1 < parsed_base2: return "upgrade"
    if parsed_base1 > parsed_base2: return "downgrade"

    # Base versions are equal, compare release numbers
    try:
        num_rel1 = int(rel1_str) if rel1_str and rel1_str.strip() and rel1_str.isdigit() else 0
        num_rel2 = int(rel2_str) if rel2_str and rel2_str.strip() and rel2_str.isdigit() else 0
    except ValueError: # Should not happen if pkgrel is always numeric or None/empty
        comp_logger.error(f"Invalid non-integer release string encountered: '{rel1_str}' or '{rel2_str}' with equal base '{base_ver1_str}'. Treating as 'unknown'.")
        return "unknown"
    
    if num_rel1 < num_rel2: return "upgrade"
    if num_rel1 > num_rel2: return "downgrade"
    
    return "same" # Bases and releases are equal

# --- Helper to construct full version string for display ---
def _get_full_version_string(v_str, r_str):
    if not v_str: return None # No base version, no full version
    r_str_val = str(r_str) if r_str is not None else ""
    # Only append release if it's meaningful (not empty, not "0")
    if r_str_val and r_str_val.strip() and r_str_val != "0":
        return f"{v_str}-{r_str_val}"
    return v_str # Return base version if no meaningful release

# --- Data Fetching Functions (Revised for pkgbase keying) ---
def fetch_aur_data(maintainer):
    aur_logger = logging.getLogger("aur_updater_cli.aur")
    aur_data_by_pkgbase = {} # Keyed by PackageBase
    
    if USE_MOCK_AUR_DATA:
        aur_logger.warning("Using MOCK AUR data.")
        if maintainer == "envolution": # Example for openfoam
            aur_data_by_pkgbase["openfoam"] = {
                "aur_actual_pkgname": "openfoam-org", "aur_pkgbase_reported": "openfoam",
                "aur-pkgver": "12.20250206", "aur-pkgrel": "7" # From your example
            }
        aur_logger.info(f"Mock AUR data for '{maintainer}': {list(aur_data_by_pkgbase.keys())}")
        return aur_data_by_pkgbase
    
    url = f"https://aur.archlinux.org/rpc/v5/search/{maintainer}?by=maintainer"
    aur_logger.info(f"Querying AUR for maintainer '{maintainer}' at: {url}")
    try:
        process = subprocess.run(['curl', '-s', url], capture_output=True, text=True, check=True, timeout=20)
        aur_logger.debug(f"AUR raw response for '{maintainer}': {process.stdout[:300]}...")
        data = json.loads(process.stdout)

        if data.get("type") == "error": 
            aur_logger.error(f"AUR API error for '{maintainer}': {data.get('error')}")
            return aur_data_by_pkgbase
        if data.get("resultcount", 0) == 0: 
            aur_logger.info(f"No packages found on AUR for maintainer '{maintainer}'.")
            return aur_data_by_pkgbase

        pkgbases_processed_count = 0
        for result in data.get("results", []):
            aur_pkgname_from_rpc = result["Name"] # This is the $pkgname from AUR
            aur_pkgbase_from_rpc = result["PackageBase"] # This is the $pkgbase from AUR
            full_aur_version_str = result["Version"]
            
            version_no_epoch = full_aur_version_str.split(':', 1)[-1]
            parts = version_no_epoch.rsplit('-', 1)
            aur_base_ver = parts[0]
            aur_rel_ver = parts[1] if len(parts) > 1 and parts[1].isdigit() else "0"
            
            # Use aur_pkgbase_from_rpc as the key
            if aur_pkgbase_from_rpc not in aur_data_by_pkgbase:
                pkgbases_processed_count +=1
                aur_data_by_pkgbase[aur_pkgbase_from_rpc] = {
                    "aur_actual_pkgname": aur_pkgname_from_rpc, 
                    "aur_pkgbase_reported": aur_pkgbase_from_rpc, # Store for reference
                    "aur-pkgver": aur_base_ver,
                    "aur-pkgrel": aur_rel_ver
                }
                aur_logger.debug(f"  Stored AUR for PkgBase='{aur_pkgbase_from_rpc}' (from PkgName='{aur_pkgname_from_rpc}'): Base='{aur_base_ver}', Rel='{aur_rel_ver}'")
            else:
                # This case handles if multiple pkgnames from a maintainer map to the same pkgbase.
                # For now, we take the first one. A more complex handling might be needed if that's common.
                aur_logger.debug(f"  PkgBase '{aur_pkgbase_from_rpc}' already processed from AUR. PkgName '{aur_pkgname_from_rpc}' info not merged for simplicity.")

        aur_logger.info(f"Fetched and consolidated info for {pkgbases_processed_count} unique PkgBase(s) from AUR for '{maintainer}'.")
    except subprocess.TimeoutExpired:
        aur_logger.error(f"AUR query timed out for URL: {url}")
    except subprocess.CalledProcessError as e:
        aur_logger.error(f"Failed to query AUR (curl error, exit code {e.returncode}) for '{maintainer}': {e.stderr}")
    except json.JSONDecodeError as e:
        aur_logger.error(f"Failed to parse AUR JSON response for '{maintainer}': {e}")
    except Exception as e:
        aur_logger.error(f"Unexpected error during AUR fetch for '{maintainer}': {e}", exc_info=aur_logger.isEnabledFor(logging.DEBUG))
    return aur_data_by_pkgbase

def fetch_local_pkgbuild_data(path_root, pkgbuild_script_path):
    local_logger = logging.getLogger("aur_updater_cli.local")
    local_data_by_pkgbase = {} # Keyed by derived pkgbase
    
    if USE_MOCK_LOCAL_DATA:
        local_logger.warning("Using MOCK local PKGBUILD data.")
        # Example for openfoam, assuming pkgbuild_to_json.py output format
        local_data_by_pkgbase["openfoam"] = { 
            "local_actual_pkgname": "openfoam-org", "local_pkgbase_derived": "openfoam",
            "pkgver": "12.20250206", "pkgrel": "6", # Example
            "pkgfile": "/mock/openfoam/PKGBUILD", "depends": [], "makedepends": [], "checkdepends": []
        }
        return local_data_by_pkgbase

    actual_script_path = os.path.abspath(pkgbuild_script_path)
    if not os.path.exists(actual_script_path):
        local_logger.critical(f"PKGBUILD parsing script '{actual_script_path}' not found. Cannot fetch local data.")
        return local_data_by_pkgbase

    abs_path_root = os.path.abspath(path_root)
    pkgbuild_glob_pattern = os.path.join(abs_path_root, '**', 'PKGBUILD')
    local_logger.info(f"Searching for local PKGBUILDs in '{abs_path_root}' using pattern: '{pkgbuild_glob_pattern}'")
    
    pkgbuild_files = glob.glob(pkgbuild_glob_pattern, recursive=True)
    if not pkgbuild_files:
        local_logger.warning(f"No PKGBUILD files found under '{abs_path_root}'.")
        return local_data_by_pkgbase
    local_logger.info(f"Found {len(pkgbuild_files)} PKGBUILD file(s) to process.")

    cmd = [sys.executable, actual_script_path] + pkgbuild_files
    local_logger.info(f"Calling pkgbuild_to_json.py with {len(pkgbuild_files)} PKGBUILD(s).")
    local_logger.debug(f"Full command to pkgbuild_to_json.py: {' '.join(cmd)}")

    try:
        process = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=90) # Increased timeout slightly

        if process.stderr:
            local_logger.warning(f"pkgbuild_to_json.py STDERR:\n{process.stderr.strip()}")
        if process.returncode != 0:
            local_logger.error(f"pkgbuild_to_json.py script failed with exit code {process.returncode}.")
            return local_data_by_pkgbase
        
        result_json_str = process.stdout
        if not result_json_str.strip():
            local_logger.warning("pkgbuild_to_json.py produced no output on STDOUT.")
            return local_data_by_pkgbase
            
        local_logger.debug(f"pkgbuild_to_json.py STDOUT (first 500 chars): {result_json_str.strip()[:500]}...")
        parsed_items_list = json.loads(result_json_str) # Expects a list of objects
        
        parsed_count = 0
        for item in parsed_items_list:
            # Get pkgbase from JSON. If empty, derive from PKGBUILD file path.
            local_pkgbase_from_json = item.get("pkgbase")
            pkgfile_path = item.get("pkgfile")
            
            derived_pkgbase_key = local_pkgbase_from_json # Prioritize from JSON
            if not derived_pkgbase_key and pkgfile_path: # Fallback to dir name if pkgbase is empty
                # Parent directory of the PKGBUILD file often represents the pkgbase
                derived_pkgbase_key = os.path.basename(os.path.dirname(pkgfile_path))
                local_logger.debug(f"  Empty 'pkgbase' from JSON for PKGBUILD '{pkgfile_path}', derived pkgbase key as '{derived_pkgbase_key}' from dir name.")
            elif not derived_pkgbase_key: # No pkgbase from JSON and no pkgfile path to derive from
                local_logger.warning(f"  Could not determine pkgbase for item: {item}. Skipping.")
                continue

            actual_pkgname_from_json = item.get("pkgname")
            if not actual_pkgname_from_json: # Should not happen if pkgbuild_to_json.py is robust
                actual_pkgname_from_json = derived_pkgbase_key # Fallback display name
                local_logger.warning(f"  Item (base: {derived_pkgbase_key}) missing 'pkgname' from JSON, using pkgbase as display name.")

            local_data_by_pkgbase[derived_pkgbase_key] = {
                "local_actual_pkgname": actual_pkgname_from_json, 
                "local_pkgbase_derived": derived_pkgbase_key, # For reference
                "pkgver": item.get("pkgver"), 
                "pkgrel": item.get("pkgrel"),
                "depends": item.get("depends", []), 
                "makedepends": item.get("makedepends", []),
                "checkdepends": item.get("checkdepends", []), 
                "pkgfile": pkgfile_path
            }
            parsed_count += 1
            local_logger.debug(f"  Parsed Local PkgBaseKey='{derived_pkgbase_key}' (ActualPkgName='{actual_pkgname_from_json}'): Base='{item.get('pkgver')}', Rel='{item.get('pkgrel')}'")
        local_logger.info(f"Successfully parsed local data for {parsed_count} unique PkgBase entries.")

    except subprocess.TimeoutExpired:
        local_logger.error(f"Call to '{actual_script_path}' timed out.")
    except json.JSONDecodeError as e:
        local_logger.error(f"Failed to parse JSON output from '{actual_script_path}': {e}")
        local_logger.debug(f"Problematic JSON string (first 500 chars): {result_json_str.strip()[:500]}...")
    except Exception as e:
        local_logger.error(f"Unexpected error during local PKGBUILD data fetch: {e}", exc_info=local_logger.isEnabledFor(logging.DEBUG))
    return local_data_by_pkgbase

def run_nvchecker(path_root, oldver_data_for_nvchecker, key_toml_path_arg):
    nv_logger = logging.getLogger("aur_updater_cli.nvchecker")
    nvchecker_results_by_pkgbase = {} # Keyed by pkgbase (name from nvchecker)
    
    if USE_MOCK_NVCHECKER_OUTPUT:
        nv_logger.warning("Using MOCK NVCHECKER data.")
        if "openfoam" in oldver_data_for_nvchecker:
            nvchecker_results_by_pkgbase["openfoam"] = {
                "nvchecker_name_reported": "openfoam", "nvchecker-pkgver": "12.20250206", 
                "nvchecker-event": "updated", "nvchecker-raw-log": {"name": "openfoam", "version": "12.20250206", "event": "updated"}
            }
        return nvchecker_results_by_pkgbase

    abs_path_root = os.path.abspath(path_root)
    glob_pattern = os.path.join(abs_path_root, '**', '.nvchecker.toml') 
    toml_files = glob.glob(glob_pattern, recursive=True)
    if not toml_files: nv_logger.warning(f"No .nvchecker.toml files found in '{abs_path_root}'. Skipping NVChecker."); return nvchecker_results_by_pkgbase
    nv_logger.info(f"Found {len(toml_files)} .nvchecker.toml file(s).")

    with tempfile.TemporaryDirectory(prefix="aurupdater_nv_") as tmpdir:
        all_nv_tomls_path = os.path.join(tmpdir, "all_nv.toml")
        oldver_json_path = os.path.join(tmpdir, "oldver.json")
        newver_json_path = os.path.join(tmpdir, "newver.json")
        
        effective_key_toml_path = None
        if key_toml_path_arg:
            abs_user_key_path = os.path.abspath(key_toml_path_arg)
            if os.path.exists(abs_user_key_path): effective_key_toml_path = abs_user_key_path
            else: nv_logger.warning(f"User key file not found: {abs_user_key_path}")
        else:
            github_token = os.environ.get("GITHUB_TOKEN")
            if github_token:
                temp_key_path = os.path.join(tmpdir, "env_github_key.toml")
                try:
                    with open(temp_key_path, "w") as f: f.write(f"[keys]\ngithub = '{github_token}'\n")
                    effective_key_toml_path = temp_key_path
                    nv_logger.info(f"Using temp key file from GITHUB_TOKEN: {effective_key_toml_path}")
                except Exception as e: nv_logger.error(f"Failed to create temp key file from GITHUB_TOKEN: {e}")
            else: nv_logger.info("No --key-toml and no GITHUB_TOKEN. NVChecker proceeds without GitHub keys.")

        concatenated_content_lines = ["[__config__]\n",
                                     f"oldver = '{os.path.basename(oldver_json_path)}'\n",
                                     f"newver = '{os.path.basename(newver_json_path)}'\n\n"]
        for toml_file in toml_files:
            try:
                with open(toml_file, "r") as f: concatenated_content_lines.extend([f"# Source: {os.path.relpath(toml_file, abs_path_root)}\n", f.read(), "\n\n"])
            except Exception as e: nv_logger.error(f"Error reading {toml_file}: {e}"); continue
        with open(all_nv_tomls_path, "w") as f: f.write("".join(concatenated_content_lines))
        if nv_logger.isEnabledFor(logging.DEBUG): nv_logger.debug(f"Concatenated .nvchecker.toml (first 500c):\n{''.join(concatenated_content_lines)[:500]}...")
        
        with open(oldver_json_path, "w") as f: json.dump({"version": 2, "data": oldver_data_for_nvchecker}, f) # oldver_data is keyed by pkgbase
        if nv_logger.isEnabledFor(logging.DEBUG): nv_logger.debug(f"Oldver JSON for NVChecker: {json.dumps({'version': 2, 'data': oldver_data_for_nvchecker}, indent=2)}")
        with open(newver_json_path, "w") as f: json.dump({}, f)

        cmd = ['nvchecker', '-c', os.path.basename(all_nv_tomls_path), '--logger=json']
        if effective_key_toml_path: cmd.extend(['-k', effective_key_toml_path])
        
        nv_logger.info(f"Running NVChecker: \"{' '.join(cmd)}\" (in {tmpdir})")
        stdout_data, stderr_data, return_code = "", "", 1
        try:
            process = subprocess.run(cmd, capture_output=True, text=True, cwd=tmpdir, check=False, timeout=180)
            stdout_data, stderr_data, return_code = process.stdout, process.stderr, process.returncode
            if return_code != 0: nv_logger.error(f"NVChecker exited with code {return_code}.")
            if stderr_data: nv_logger.warning(f"NVChecker STDERR:\n{stderr_data.strip()}")
            if not stdout_data.strip() and return_code == 0: nv_logger.info("NVChecker ran successfully, no JSON output on STDOUT.")
        except FileNotFoundError: nv_logger.critical("nvchecker command not found."); return nvchecker_results_by_pkgbase
        except subprocess.TimeoutExpired: nv_logger.error("NVChecker command timed out."); return nvchecker_results_by_pkgbase
        except Exception as e: nv_logger.error(f"NVChecker execution error: {e}", exc_info=True); return nvchecker_results_by_pkgbase

        if stdout_data.strip(): nv_logger.debug(f"NVChecker STDOUT (first 500c): {stdout_data.strip()[:500]}...")
        for line in stdout_data.strip().split('\n'):
            if not line.strip(): continue
            try:
                log_entry = json.loads(line)
                if log_entry.get("logger_name") != "nvchecker.core":
                    nv_logger.debug(f"Skipping NVCR log from '{log_entry.get('logger_name')}': {line[:100]}...")
                    continue
                
                # 'name' from nvchecker.core log is the pkgbase (section name from .toml)
                pkgbase_from_nvchecker = log_entry.get("name")
                if not pkgbase_from_nvchecker: 
                    nv_logger.warning(f"NVCR JSON line from nvchecker.core missing 'name': {line}")
                    continue
                
                nvchecker_results_by_pkgbase[pkgbase_from_nvchecker] = {
                    "nvchecker_name_reported": pkgbase_from_nvchecker, # For reference/display name fallback
                    "nvchecker-pkgver": log_entry.get("version"), 
                    "nvchecker-event": log_entry.get("event"), 
                    "nvchecker-raw-log": log_entry
                }
                # Logging results
                event, ver, old_ver = log_entry.get("event"), log_entry.get('version','N/A'), log_entry.get('old_version','N/A')
                if event == "updated": nv_logger.info(f"NVCR: {pkgbase_from_nvchecker} UPDATED {old_ver} -> {ver}")
                elif event == "up-to-date": nv_logger.info(f"NVCR: {pkgbase_from_nvchecker} UP-TO-DATE at {ver}")
                # ... other event logging ...
            except json.JSONDecodeError: nv_logger.warning(f"Failed to parse NVCR JSON: {line}")
    return nvchecker_results_by_pkgbase

# --- Data Processing and Comparison ---
def process_and_compare_data(all_data_by_pkgbase):
    proc_logger = logging.getLogger("aur_updater_cli.processing")
    output_list = [] 

    for pkgbase_key, combined_data_for_pkgbase in all_data_by_pkgbase.items():
        display_pkgname = combined_data_for_pkgbase.get("aur_actual_pkgname") or \
                          combined_data_for_pkgbase.get("local_actual_pkgname") or \
                          combined_data_for_pkgbase.get("nvchecker_name_reported") or \
                          pkgbase_key 

        pkg_entry = {
            "pkgbase": pkgbase_key, "pkgname": display_pkgname,
            "pkgver": combined_data_for_pkgbase.get("pkgver"), 
            "pkgrel": combined_data_for_pkgbase.get("pkgrel"),
            "aur-pkgver": combined_data_for_pkgbase.get("aur-pkgver"), 
            "aur-pkgrel": combined_data_for_pkgbase.get("aur-pkgrel"),
            "nvchecker-pkgver": combined_data_for_pkgbase.get("nvchecker-pkgver"),
            "depends": combined_data_for_pkgbase.get("depends", []), 
            "makedepends": combined_data_for_pkgbase.get("makedepends", []),
            "checkdepends": combined_data_for_pkgbase.get("checkdepends", []), 
            "pkgfile": combined_data_for_pkgbase.get("pkgfile"),
            "nvchecker-event": combined_data_for_pkgbase.get("nvchecker-event"), 
            "nvchecker-raw-log": combined_data_for_pkgbase.get("nvchecker-raw-log"),
            "errors": [], "is_update_candidate": True, "is_update": False,
            "update_source": None, "new_version_for_update": None, 
            "comparison_details": {}
        }

        nv_base = pkg_entry['nvchecker-pkgver']
        aur_base = pkg_entry['aur-pkgver']
        aur_rel = pkg_entry['aur-pkgrel']
        local_base = pkg_entry['pkgver']
        local_rel = pkg_entry['pkgrel']

        # Error conditions (comparing base versions)
        if aur_base and nv_base:
            comp = compare_package_versions(aur_base, None, nv_base, None)
            pkg_entry['comparison_details']['aur_base_vs_nv_base'] = f"AURBase({aur_base}) vs NVBase({nv_base}) -> {comp}"
            if comp == "downgrade": # AUR base > NV base
                pkg_entry['errors'].append(f"AUR base ver {aur_base} > NVChecker base ver {nv_base}.")
                pkg_entry['is_update_candidate'] = False
        
        if local_base and nv_base:
            comp = compare_package_versions(local_base, None, nv_base, None)
            pkg_entry['comparison_details']['local_base_vs_nv_base'] = f"LocalBase({local_base}) vs NVBase({nv_base}) -> {comp}"
            if comp == "downgrade": # Local base > NV base
                pkg_entry['errors'].append(f"Local base ver {local_base} > NVChecker base ver {nv_base}.")
                pkg_entry['is_update_candidate'] = False
        
        if not pkg_entry['is_update_candidate']:
            proc_logger.debug(f"PkgBase {pkgbase_key} (Name {display_pkgname}) not update candidate: {pkg_entry['errors']}")
            output_list.append(pkg_entry); continue

        # Update determination
        update_found = False
        if nv_base:
            comp_local_to_nv = compare_package_versions(local_base, local_rel, nv_base, None)
            comp_aur_to_nv = compare_package_versions(aur_base, aur_rel, nv_base, None)
            pkg_entry['comparison_details']['local_full_to_nv_base'] = f"Local({_get_full_version_string(local_base, local_rel)}) to NV({nv_base}) -> {comp_local_to_nv}"
            pkg_entry['comparison_details']['aur_full_to_nv_base'] = f"AUR({_get_full_version_string(aur_base, aur_rel)}) to NV({nv_base}) -> {comp_aur_to_nv}"
            if comp_local_to_nv == "upgrade" and comp_aur_to_nv == "upgrade":
                pkg_entry.update({'is_update': True, 'update_source': 'nvchecker (new pkg)' if not local_base and not aur_base else 'nvchecker', 'new_version_for_update': nv_base })
                update_found = True
        
        if not update_found and aur_base:
            comp_local_to_aur = compare_package_versions(local_base, local_rel, aur_base, aur_rel)
            pkg_entry['comparison_details']['local_full_to_aur_full'] = f"Local({_get_full_version_string(local_base, local_rel)}) to AUR({_get_full_version_string(aur_base, aur_rel)}) -> {comp_local_to_aur}"
            if comp_local_to_aur == "upgrade": 
                pkg_entry.update({'is_update': True, 'update_source': 'aur (new pkg)' if not local_base else 'aur', 'new_version_for_update': _get_full_version_string(aur_base, aur_rel)})
        
        if pkg_entry['is_update']: proc_logger.info(f"Update for {pkgbase_key} (Name {display_pkgname}): to {pkg_entry['new_version_for_update']} via {pkg_entry['update_source']}")
        elif not pkg_entry['errors']: proc_logger.info(f"No update for {pkgbase_key} (Name {display_pkgname}).")
        output_list.append(pkg_entry)
    return output_list

# --- Summary Function ---
def generate_summary(processed_output_list, stream=sys.stderr): # Takes the final list of package dicts
    summary_logger = logging.getLogger("aur_updater_cli.summary")
    updates_nv, updates_aur, errors_list, nv_issues, up_to_date_count = [], [], [], [], 0

    for pkg_data in processed_output_list: # Iterate through the list
        # Use pkgbase for identification in summary, or pkgname if preferred
        id_name = pkg_data.get('pkgbase', 'UnknownBase') 
        display_name_info = f" (Name: {pkg_data.get('pkgname', id_name)})" if pkg_data.get('pkgname') != id_name else ""


        if pkg_data['is_update']:
            src, new_ver = pkg_data['update_source'], pkg_data['new_version_for_update']
            (updates_nv if 'nvchecker' in src else updates_aur).append(f"{id_name}{display_name_info} (to {new_ver} via {src})")
        elif not pkg_data['errors']: 
            up_to_date_count +=1
        
        if pkg_data['errors']: 
            errors_list.extend([f"{id_name}{display_name_info}: {err}" for err in pkg_data['errors']])
        
        if not pkg_data['is_update'] and not pkg_data['errors']:
            nv_raw_log = pkg_data.get('nvchecker-raw-log')
            if nv_raw_log:
                nv_event, nv_level = nv_raw_log.get('event'), nv_raw_log.get('level')
                if nv_event == 'no-result' or nv_level == 'error' or nv_raw_log.get('exc_info'):
                    details = nv_raw_log.get('exc_info', nv_raw_log.get('msg', f"Event: {nv_event}, Level: {nv_level}"))
                    nv_issues.append(f"{id_name}{display_name_info}: NVChecker issue - {details}")

    lines = ["--- Package Update Summary ---"]
    if updates_nv: lines.extend([f"\nNVChecker Driven Updates ({len(updates_nv)}):"] + [f"  - {s}" for s in updates_nv])
    if updates_aur: lines.extend([f"\nAUR Driven Updates ({len(updates_aur)}):"] + [f"  - {s}" for s in updates_aur])
    if not updates_nv and not updates_aur and not errors_list: lines.append("\nNo package updates identified and no versioning errors.")
    elif not updates_nv and not updates_aur: lines.append("\nNo package updates identified (check errors below).")
    if up_to_date_count > 0 : lines.append(f"\nPackages considered up-to-date or no action needed: {up_to_date_count}")
    if errors_list: lines.extend([f"\nVersion Comparison Discrepancies or Rule Violations ({len(errors_list)}):"] + [f"  - {s}" for s in errors_list])
    if nv_issues: lines.extend([f"\nNVChecker Operational Issues Reported ({len(nv_issues)}):"] + [f"  - {s}" for s in nv_issues])
    
    summary_output_str = "\n".join(lines)
    print(summary_output_str, file=stream)

# --- Main Application Class ---
class AurPackageUpdater:
    def __init__(self, args):
        self.args = args
        self.all_package_data_by_pkgbase = {} # Keyed by pkgbase
        self._configure_logging()

    def _configure_logging(self):
        root_logger = logging.getLogger() # Get root logger
        app_log_level = logging.DEBUG if self.args.debug else logging.INFO
        logger.setLevel(app_log_level) # Set level for our application's logger instance

        # Ensure root logger has a handler and its level is set appropriately
        if not root_logger.hasHandlers():
            default_handler = logging.StreamHandler(sys.stderr)
            default_formatter = logging.Formatter('%(levelname)s: %(name)s: %(message)s')
            default_handler.setFormatter(default_formatter)
            root_logger.addHandler(default_handler)
        
        # Set root logger level. If our app is debug, root should also be debug.
        # Otherwise, root can be INFO. This allows libraries to log at INFO if not overridden.
        root_logger.setLevel(logging.DEBUG if self.args.debug else logging.INFO)
        
        logger.info(f"Application logger '{logger.name}' effective level: {logging.getLevelName(logger.getEffectiveLevel())}")
        # logger.info(f"Root logger effective level: {logging.getLevelName(root_logger.getEffectiveLevel())}")


    def run(self):
        # 1. Fetch AUR Data (keyed by pkgbase)
        aur_data = fetch_aur_data(self.args.maintainer)
        for pkgbase, data in aur_data.items():
            self.all_package_data_by_pkgbase.setdefault(pkgbase, {"key_is_pkgbase": pkgbase}).update(data)

        # 2. Fetch Local PKGBUILD Data (keyed by derived pkgbase)
        if not os.path.isdir(self.args.path_root):
            logger.critical(f"Path root '{self.args.path_root}' is not a valid directory. Exiting.")
            sys.exit(1)
        local_data = fetch_local_pkgbuild_data(self.args.path_root, self.args.pkgbuild_script)
        for pkgbase, data in local_data.items():
            self.all_package_data_by_pkgbase.setdefault(pkgbase, {"key_is_pkgbase": pkgbase}).update(data)
        
        # 3. Run NVChecker (oldver input keyed by pkgbase, output keyed by pkgbase)
        nvchecker_oldver_input = {}
        for pkgbase, data_dict in self.all_package_data_by_pkgbase.items():
            aur_base_ver_for_oldver = data_dict.get('aur-pkgver')
            if aur_base_ver_for_oldver:
                 nvchecker_oldver_input[pkgbase] = {'version': aur_base_ver_for_oldver}
        
        nvchecker_data = run_nvchecker(self.args.path_root, nvchecker_oldver_input, self.args.key_toml)
        for pkgbase, data in nvchecker_data.items(): # pkgbase here is nvchecker's 'name'
            self.all_package_data_by_pkgbase.setdefault(pkgbase, {"key_is_pkgbase": pkgbase}).update(data)

        # 4. Process and Compare Data (input is pkgbase-keyed dict, output is list of dicts)
        final_output_list = process_and_compare_data(self.all_package_data_by_pkgbase)

        # 5. Output JSON
        output_stream = open(self.args.output_file, 'w') if self.args.output_file else sys.stdout
        try:
            json.dump(final_output_list, output_stream, indent=2)
            if self.args.output_file: 
                output_stream.write("\n")
                logger.info(f"JSON output written to {self.args.output_file}")
        finally:
            if self.args.output_file and output_stream is not sys.stdout:
                output_stream.close()
        
        # 6. Output Summary
        if self.args.summary: 
            generate_summary(final_output_list, stream=sys.stderr)

# --- CLI Argument Parsing and Main Execution ---
def main_cli():
    parser = argparse.ArgumentParser(
        description="Consolidate AUR/local/NVChecker package info and determine updates.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument("--maintainer", required=True, help="AUR maintainer name.")
    parser.add_argument("--path-root", required=True, help="Root directory for PKGBUILDs and .nvchecker.toml files.")
    parser.add_argument("--pkgbuild-script", default="./pkgbuild_to_json.py",
                        help="Path to the script that parses PKGBUILDs to JSON.")
    parser.add_argument("--key-toml", default=None,
                        help="Path to NVChecker's key.toml file. If not set, checks GITHUB_TOKEN env var.")
    parser.add_argument("--output-file", default=None,
                        help="File to write JSON output to. If not set, output to STDOUT.")
    parser.add_argument("--summary", action="store_true",
                        help="Print a human-readable summary of operations to STDERR.")
    parser.add_argument("--debug", action="store_true",
                        help="Enable verbose debug logging.")
    
    args = parser.parse_args()
    
    app = AurPackageUpdater(args)
    try:
        app.run()
    except Exception as e:
        logger.critical(f"An unhandled exception occurred in the application: {e}", exc_info=True)
        sys.exit(2)

if __name__ == "__main__":
    main_cli()
