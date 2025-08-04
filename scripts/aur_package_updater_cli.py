#!/usr/bin/env python3

import argparse
import json
import os
import subprocess
import sys
import tempfile
import glob
import logging
from awesomeversion import AwesomeVersion
import pyalpm

# --- Constants ---
# Default logger if the script is run standalone.
# If run as part of a larger system, a configured logger should be passed to the main class.
DEFAULT_LOGGER = logging.getLogger("aur_updater_cli")


# --- Version Comparison Function (Using AwesomeVersion, falling back on pyalpm vercmp) ---
def compare_package_versions(
    base_ver1_str: str, rel1_str: str, base_ver2_str: str, rel2_str: str
) -> str:
    comp_logger = logging.getLogger("aur_updater_cli.compare")

    if base_ver1_str is None and base_ver2_str is None:
        return "same"
    if base_ver1_str is None:
        return "upgrade"
    if base_ver2_str is None:
        return "downgrade"

    # First try AwesomeVersion
    try:
        lv1 = AwesomeVersion(base_ver1_str)
        lv2 = AwesomeVersion(base_ver2_str)

        if lv1 < lv2:
            return "upgrade"
        if lv1 > lv2:
            return "downgrade"

    except Exception as e:
        comp_logger.warning(
            f"AwesomeVersion failed for '{base_ver1_str}' or '{base_ver2_str}', falling back to pyalpm.vercmp: {e}"
        )
        try:
            result = pyalpm.vercmp(base_ver1_str, base_ver2_str)
            if result < 0:
                return "upgrade"
            if result > 0:
                return "downgrade"
        except Exception as e2:
            comp_logger.error(
                f"Fallback pyalpm.vercmp failed for '{base_ver1_str}' vs '{base_ver2_str}': {e2}"
            )
            return "unknown"

    # Compare release numbers only if base versions are the same
    try:
        num_rel1 = int(rel1_str) if rel1_str and rel1_str.strip().isdigit() else 0
        num_rel2 = int(rel2_str) if rel2_str and rel2_str.strip().isdigit() else 0
    except ValueError:
        comp_logger.error(
            f"Invalid non-integer release: '{rel1_str}' or '{rel2_str}' with base '{base_ver1_str}'."
        )
        return "unknown"

    if num_rel1 < num_rel2:
        return "upgrade"
    if num_rel1 > num_rel2:
        return "downgrade"

    return "same"


# --- Helper to construct full version string for display ---
def _get_full_version_string(v_str, r_str):
    if not v_str:
        return None
    r_str_val = str(r_str) if r_str is not None else ""
    if r_str_val and r_str_val.strip() and r_str_val != "0":
        return f"{v_str}-{r_str_val}"
    return v_str


# --- Data Fetching Functions ---
# (fetch_aur_data, fetch_local_pkgbuild_data, run_nvchecker remain unchanged from the last version
#  as they focus on data gathering, not the comparison logic itself)
def fetch_aur_data(ownership, maintainer, logger=DEFAULT_LOGGER):
    aur_logger = logger.getChild("aur")
    aur_data_by_pkgbase = {}
    url = f"https://aur.archlinux.org/rpc/v5/search/{maintainer}?by={ownership}"
    aur_logger.info(f"Querying AUR for '{ownership}' key '{maintainer}' at: {url}")
    try:
        process = subprocess.run(
            ["curl", "-s", url], capture_output=True, text=True, check=True, timeout=20
        )
        aur_logger.debug(
            f"AUR raw response for '{maintainer}': {process.stdout[:300]}..."
        )
        data = json.loads(process.stdout)
        if data.get("type") == "error":
            aur_logger.error(
                f"AUR API error for '{ownership}' key '{maintainer}': {data.get('error')}"
            )
            return aur_data_by_pkgbase
        if data.get("resultcount", 0) == 0:
            aur_logger.info(
                f"No packages found on AUR for '{ownership}' key '{maintainer}'."
            )
            return aur_data_by_pkgbase

        count = 0
        for result in data.get("results", []):
            name, base_name, full_ver = (
                result["Name"],
                result["PackageBase"],
                result["Version"],
            )
            ver_no_epoch = full_ver.split(":", 1)[-1]
            parts = ver_no_epoch.rsplit("-", 1)
            base_v, rel_v = (
                parts[0],
                (parts[1] if len(parts) > 1 and parts[1].isdigit() else "0"),
            )
            if base_name not in aur_data_by_pkgbase:
                count += 1
                aur_data_by_pkgbase[base_name] = {
                    "aur_actual_pkgname": name,
                    "aur_pkgbase_reported": base_name,
                    "aur_pkgver": base_v,
                    "aur_pkgrel": rel_v,
                }
                # aur_logger.debug(
                #    f"  Stored AUR for PkgBase='{base_name}' (from PkgName='{name}'): Base='{base_v}', Rel='{rel_v}'"
                # )
        aur_logger.info(
            f"Fetched info for {count} unique PkgBase(s) from AUR for '{maintainer}'."
        )
    except subprocess.TimeoutExpired:
        aur_logger.error(f"AUR query timed out for '{ownership}' key '{maintainer}'.")
    except subprocess.CalledProcessError as e:
        aur_logger.error(
            f"AUR query fail for '{ownership}' key '{maintainer}' (code {e.returncode}): {e.stderr}"
        )
    except json.JSONDecodeError as e:
        aur_logger.error(
            f"Failed to parse AUR JSON for '{ownership}' key '{maintainer}': {e}"
        )
    except Exception as e:
        aur_logger.error(
            f"AUR fetch error for '{ownership}' key '{maintainer}': {e}",
            exc_info=aur_logger.isEnabledFor(logging.DEBUG),
        )
    return aur_data_by_pkgbase


def get_combined_aur_data(maintainer, logger=DEFAULT_LOGGER):
    """Fetch and combine maintainer and co-maintainer AUR data."""
    aur_logger = logger.getChild("aur")

    # Fetch maintainer data (required)
    aur_data = fetch_aur_data("maintainer", maintainer, logger=logger)

    # Try to fetch co-maintainer data (optional)
    try:
        comaintainer_data = fetch_aur_data("comaintainers", maintainer, logger=logger)
        # Merge the dictionaries - co-maintainer data takes precedence for conflicts
        aur_data.update(comaintainer_data)
        aur_logger.info(f"Successfully merged co-maintainer data for '{maintainer}'")
    except Exception as e:
        aur_logger.warning(
            f"Could not fetch co-maintainer data for '{maintainer}': {e}"
        )
        # Continue with just maintainer data

    return aur_data


def fetch_local_pkgbuild_data(
    path_root, pkgbuild_script_path, manual_packages=None, logger=DEFAULT_LOGGER
):
    local_logger = logger.getChild("local")
    local_data_by_pkgbase = {}
    actual_script_path = os.path.abspath(pkgbuild_script_path)
    if not os.path.exists(actual_script_path):
        local_logger.critical(f"Script '{actual_script_path}' not found.")
        return local_data_by_pkgbase
    abs_path_root = os.path.abspath(path_root)
    pkgbuild_glob = os.path.join(abs_path_root, "**", "PKGBUILD")
    local_logger.info(
        f"Searching PKGBUILDs in '{abs_path_root}' using '{pkgbuild_glob}'"
    )
    pkg_files = glob.glob(pkgbuild_glob, recursive=True)

    if manual_packages:
        local_logger.info(f"Filtering PKGBUILDs for manual packages: {manual_packages}")
        filtered_pkg_files = []
        for pkg_file in pkg_files:
            package_name = os.path.basename(os.path.dirname(pkg_file))
            if package_name in manual_packages:
                filtered_pkg_files.append(pkg_file)
            else:
                local_logger.debug(
                    f"Ignoring PKGBUILD in '{pkg_file}' as '{package_name}' not in manual list."
                )
        pkg_files = filtered_pkg_files
        local_logger.info(
            f"Found {len(pkg_files)} PKGBUILD(s) after manual package filtering."
        )

    if not pkg_files:
        local_logger.warning(f"No PKGBUILDs found in '{abs_path_root}'.")
        return local_data_by_pkgbase
    local_logger.info(f"Found {len(pkg_files)} PKGBUILD(s) to process.")

    cmd = [sys.executable, actual_script_path] + pkg_files
    local_logger.debug(
        f"Calling pkgbuild_to_json.py (cmd snippet): {' '.join(cmd[:3])} ..."
    )

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=90
        )

        if proc.returncode != 0:
            # Enhanced logging for failure
            local_logger.error(
                f"Call to '{actual_script_path}' (command: \"{' '.join(cmd)}\") failed with return code {proc.returncode}."
            )
            if proc.stdout and proc.stdout.strip():
                local_logger.error(f"STDOUT from failed call:\n{proc.stdout.strip()}")
            else:
                local_logger.error("STDOUT from failed call was empty.")
            if proc.stderr and proc.stderr.strip():
                local_logger.error(f"STDERR from failed call:\n{proc.stderr.strip()}")
            else:
                local_logger.error("STDERR from failed call was empty.")
            return local_data_by_pkgbase  # Exit this function

        # Existing stderr warning (if return code was 0 but there's stderr)
        if proc.stderr and proc.stderr.strip():
            local_logger.warning(
                f"pkgbuild_to_json.py STDERR (though command exited 0):\n{proc.stderr.strip()}"
            )

        res_json = proc.stdout
        if not res_json.strip():
            local_logger.warning(
                f'pkgbuild_to_json.py (command: "{" ".join(cmd)}") produced no STDOUT. Cannot parse local package data.'
            )
            return local_data_by_pkgbase

        parsed_items = json.loads(res_json)
        if not parsed_items:  # Check if the JSON parsed to an empty list/dict
            local_logger.warning(
                f'pkgbuild_to_json.py (command: "{" ".join(cmd)}") produced valid but empty JSON. No package data extracted.'
            )
            return local_data_by_pkgbase

        parsed_items = json.loads(res_json)
        count = 0
        for item in parsed_items:
            json_pkgbase, json_pkgname, pkgfile = (
                item.get("pkgbase"),
                item.get("pkgname"),
                item.get("pkgfile"),
            )
            key_pkgbase = json_pkgbase
            if not key_pkgbase and pkgfile:
                key_pkgbase = os.path.basename(os.path.dirname(pkgfile))
            if not key_pkgbase:
                local_logger.warning(
                    f"Cannot determine pkgbase for item: {item}. Skipping."
                )
                continue
            actual_name = json_pkgname or key_pkgbase

            local_data_by_pkgbase[key_pkgbase] = {
                "local_actual_pkgname": actual_name,
                "local_pkgbase_derived": key_pkgbase,
                "pkgver": item.get("pkgver"),
                "pkgrel": item.get("pkgrel"),
                "depends": item.get("depends", []),
                "makedepends": item.get("makedepends", []),
                "checkdepends": item.get("checkdepends", []),
                "sources": item.get("sources", []),
                "validpgpkeys": item.get("validpgpkeys", []),
                "pkgfile": pkgfile,
            }
            count += 1
            # local_logger.debug(
            #    f"  Parsed Local PkgBase='{key_pkgbase}' (Name='{actual_name}'): Base='{item.get('pkgver')}', Rel='{item.get('pkgrel')}'"
            # )
        local_logger.info(f"Parsed local data for {count} unique PkgBase entries.")
    except subprocess.TimeoutExpired:
        local_logger.error(f"Call to '{actual_script_path}' timed out.")
    except json.JSONDecodeError as e:
        local_logger.error(
            f"Failed to parse JSON from '{actual_script_path}': {e}\nData: {res_json[:500]}..."
        )
    except Exception as e:
        local_logger.error(
            f"Local PKGBUILD fetch error: {e}",
            exc_info=local_logger.isEnabledFor(logging.DEBUG),
        )
    return local_data_by_pkgbase


def run_nvchecker(
    path_root,
    oldver_data_for_nvchecker,
    key_toml_path_arg,
    manual_packages=None,
    logger=DEFAULT_LOGGER,
):
    nv_logger = logger.getChild("nvchecker")
    results_by_pkgbase = {}
    abs_path_root = os.path.abspath(path_root)
    glob_pattern = os.path.join(abs_path_root, "**", ".nvchecker.toml")
    toml_files = glob.glob(glob_pattern, recursive=True)

    if manual_packages:
        nv_logger.info(
            f"Filtering .nvchecker.toml files for manual packages: {manual_packages}"
        )
        filtered_toml_files = []
        for toml_file in toml_files:
            package_name = os.path.basename(os.path.dirname(toml_file))
            if package_name in manual_packages:
                filtered_toml_files.append(toml_file)
            else:
                nv_logger.debug(
                    f"Ignoring .nvchecker.toml in '{toml_file}' as '{package_name}' not in manual list."
                )
        toml_files = filtered_toml_files
        nv_logger.info(
            f"Found {len(toml_files)} .nvchecker.toml file(s) after manual package filtering."
        )

    if not toml_files:
        nv_logger.warning(
            f"No .nvchecker.toml in '{abs_path_root}'. Skipping NVChecker."
        )
        return results_by_pkgbase
    nv_logger.info(f"Found {len(toml_files)} .nvchecker.toml file(s).")

    with tempfile.TemporaryDirectory(prefix="aurupdater_nv_") as tmpdir:
        all_nv_tomls_path, oldver_json_path, newver_json_path = (
            os.path.join(tmpdir, f)
            for f in ["all_nv.toml", "oldver.json", "newver.json"]
        )
        key_file_to_use = None
        if key_toml_path_arg:
            abs_user_key = os.path.abspath(key_toml_path_arg)
            if os.path.exists(abs_user_key):
                key_file_to_use = abs_user_key
                nv_logger.info(f"Using user key file: {key_file_to_use}")
            else:
                nv_logger.warning(f"User key file '{abs_user_key}' not found.")
        else:
            gh_token = os.environ.get("GITHUB_TOKEN")
            if gh_token:
                temp_key_file = os.path.join(tmpdir, "env_gh_key.toml")
                try:
                    with open(temp_key_file, "w") as f:
                        f.write(f"[keys]\ngithub = '{gh_token}'\n")
                    key_file_to_use = temp_key_file
                    nv_logger.info(
                        f"Using temp key file from GITHUB_TOKEN: {key_file_to_use}"
                    )
                except Exception as e:
                    nv_logger.error(f"Failed to write temp key file: {e}")
            else:
                nv_logger.info(
                    "No --key-toml and no GITHUB_TOKEN. NVChecker proceeds without GitHub keys."
                )

        content = [
            "[__config__]\n",
            f"oldver = '{os.path.basename(oldver_json_path)}'\n",
            f"newver = '{os.path.basename(newver_json_path)}'\n\n",
        ]
        for tf in toml_files:
            try:
                with open(tf, "r") as f:
                    content.extend(
                        [
                            f"# Source: {os.path.relpath(tf, abs_path_root)}\n",
                            f.read(),
                            "\n\n",
                        ]
                    )
            except Exception as e:
                nv_logger.error(f"Error reading {tf}: {e}")
                continue
        with open(all_nv_tomls_path, "w") as f:
            f.write("".join(content))
        if nv_logger.isEnabledFor(logging.DEBUG):
            nv_logger.debug(
                f"Concatenated .nvchecker.toml (first 500c):\n{''.join(content)[:500]}..."
            )
        with open(oldver_json_path, "w") as f:
            json.dump({"version": 2, "data": oldver_data_for_nvchecker}, f)
        if nv_logger.isEnabledFor(logging.DEBUG):
            json_str = json.dumps(
                {"version": 2, "data": oldver_data_for_nvchecker}, indent=2
            )
            truncated = (json_str[:500] + "...") if len(json_str) > 500 else json_str
            nv_logger.debug(f"Oldver JSON for NVChecker (truncated): {truncated}")
        with open(newver_json_path, "w") as f:
            json.dump({}, f)

        cmd = ["nvchecker", "-c", os.path.basename(all_nv_tomls_path), "--logger=json"]
        if key_file_to_use:
            cmd.extend(["-k", key_file_to_use])

        nv_logger.info(f'Running NVChecker: "{" ".join(cmd)}" (in {tmpdir})')
        stdout_data, stderr_data, code = "", "", 1
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=tmpdir,
                check=False,
                timeout=180,
            )
            stdout_data, stderr_data, code = proc.stdout, proc.stderr, proc.returncode
            if code != 0:
                nv_logger.error(f"NVChecker exited with code {code}.")
            if stderr_data:
                nv_logger.warning(f"NVChecker STDERR:\n{stderr_data.strip()}")
            if not stdout_data.strip() and code == 0:
                nv_logger.info("NVChecker ran successfully, no JSON output on STDOUT.")
        except FileNotFoundError:
            nv_logger.critical("nvchecker command not found.")
            return results_by_pkgbase
        except subprocess.TimeoutExpired:
            nv_logger.error("NVChecker command timed out.")
            return results_by_pkgbase
        except Exception as e:
            nv_logger.error(f"NVChecker execution error: {e}", exc_info=True)
            return results_by_pkgbase

        if stdout_data.strip():
            nv_logger.debug(
                f"NVChecker STDOUT (first 500c): {stdout_data.strip()[:500]}..."
            )
        for line in stdout_data.strip().split("\n"):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                if entry.get("logger_name") != "nvchecker.core":
                    nv_logger.debug(f"Skipping NVCR log: {line[:100]}...")
                    continue
                name = entry.get("name")  # This is the pkgbase
                if not name:
                    nv_logger.warning(f"NVCR JSON line missing 'name': {line}")
                    continue
                results_by_pkgbase[name] = {
                    "nvchecker_name_reported": name,
                    "nvchecker_pkgver": entry.get("version"),
                    "nvchecker_event": entry.get("event"),
                    "nvchecker_raw_log": entry,
                }
                ev, v, ov, lvl, msg = (
                    entry.get("event"),
                    entry.get("version", "N/A"),
                    entry.get("old_version", "N/A"),
                    entry.get("level"),
                    entry.get("msg", ""),
                )
                if ev == "updated":
                    nv_logger.info(f"NVCR: {name} UPDATED {ov} -> {v}")
                # elif ev == "up-to-date":
                #    nv_logger.info(f"NVCR: {name} UP-TO-DATE at {v}")
                elif ev == "no-result":
                    nv_logger.warning(f"NVCR: {name} NO-RESULT. {msg}")
                elif lvl == "error" or entry.get("exc_info"):
                    nv_logger.warning(
                        f"NVCR: {name} ERROR - {entry.get('exc_info', msg)}"
                    )
            except json.JSONDecodeError:
                nv_logger.warning(f"Failed to parse NVCR JSON: {line}")
    return results_by_pkgbase


# --- Data Processing and Comparison ---
def process_and_compare_data(all_data_by_pkgbase, logger=DEFAULT_LOGGER):
    proc_logger = logger.getChild("processing")
    output_list = []

    for pkgbase_key, data in all_data_by_pkgbase.items():
        if not data.get("pkgfile"):  # Filter out entries without a local PKGBUILD
            proc_logger.debug(
                f"PkgBase {pkgbase_key} has no local PKGBUILD file. Skipping from final output."
            )
            continue

        display_name = (
            data.get("aur_actual_pkgname")
            or data.get("local_actual_pkgname")
            or data.get("nvchecker_name_reported")
            or pkgbase_key
        )

        pkg_entry = {
            "pkgbase": pkgbase_key,
            "pkgname": display_name,
            "pkgver": data.get("pkgver"),
            "pkgrel": data.get("pkgrel"),
            "aur_pkgver": data.get("aur_pkgver"),
            "aur_pkgrel": data.get("aur_pkgrel"),
            "nvchecker_pkgver": data.get("nvchecker_pkgver"),
            "depends": data.get("depends", []),
            "makedepends": data.get("makedepends", []),
            "checkdepends": data.get("checkdepends", []),
            "pkgfile": data.get("pkgfile"),
            "validpgpkeys": data.get("validpgpkeys", []),
            "nvchecker_event": data.get("nvchecker_event"),
            "nvchecker_raw_log": data.get("nvchecker_raw_log"),
            "errors": [],
            "local_is_ahead": False,
            "is_update_candidate": True,
            "is_update": False,
            "update_source": None,
            "new_version_for_update": None,
            "comparison_details": {},
        }

        nv_base, aur_base, aur_r, local_base, local_r = (
            pkg_entry.get(k)
            for k in [
                "nvchecker_pkgver",
                "aur_pkgver",
                "aur_pkgrel",
                "pkgver",
                "pkgrel",
            ]
        )

        # Error conditions
        if aur_base and nv_base:
            comp = compare_package_versions(aur_base, None, nv_base, None)
            pkg_entry["comparison_details"]["aur_base_vs_nv_base"] = (
                f"AURBase({aur_base}) vs NVBase({nv_base}) -> {comp}"
            )
            if comp == "downgrade":
                pkg_entry["errors"].append(
                    f"AUR base ver ({aur_base}) > NVChecker base ver ({nv_base}) "
                )
                pkg_entry["is_update_candidate"] = False
        if local_base and nv_base:
            comp = compare_package_versions(local_base, None, nv_base, None)
            pkg_entry["comparison_details"]["local_base_vs_nv_base"] = (
                f"LocalBase({local_base}) vs NVBase({nv_base}) -> {comp}"
            )
            if comp == "downgrade":
                pkg_entry["errors"].append(
                    f"Local base ver ({local_base}) > NVChecker base ver ({nv_base}) "
                )
                pkg_entry["is_update_candidate"] = False

        if not pkg_entry["is_update_candidate"]:
            output_list.append(pkg_entry)
            continue

        # Update determination
        update_found = False
        if nv_base:
            comp_local_to_nv = compare_package_versions(
                local_base, local_r, nv_base, None
            )
            comp_aur_to_nv = compare_package_versions(aur_base, aur_r, nv_base, None)
            # No longer adding these to comparison_details unless specifically needed for debugging
            # pkg_entry['comparison_details']['local_full_to_nv_base'] = ...
            # pkg_entry['comparison_details']['aur_full_to_nv_base'] = ...
            if comp_local_to_nv == "upgrade" and comp_aur_to_nv == "upgrade":
                pkg_entry.update(
                    {
                        "is_update": True,
                        "update_source": "nvchecker (new pkg)"
                        if not local_base and not aur_base
                        else "nvchecker",
                        "new_version_for_update": nv_base,
                    }
                )
                update_found = True

        if not update_found and aur_base:
            comp_local_to_aur = compare_package_versions(
                local_base, local_r, aur_base, aur_r
            )
            pkg_entry["comparison_details"]["local_full_to_aur_full"] = (
                f"Local({_get_full_version_string(local_base, local_r)}) to AUR({_get_full_version_string(aur_base, aur_r)}) -> {comp_local_to_aur}"
            )
            if comp_local_to_aur == "upgrade":
                pkg_entry.update(
                    {
                        "is_update": True,
                        "update_source": "aur (new pkg)" if not local_base else "aur",
                        "new_version_for_update": _get_full_version_string(
                            aur_base, aur_r
                        ),
                    }
                )
                update_found = True

        if not update_found and not pkg_entry["errors"] and local_base:
            ahead_aur = (
                compare_package_versions(local_base, local_r, aur_base, aur_r)
                == "downgrade"
            )
            ahead_nv = (
                compare_package_versions(local_base, local_r, nv_base, None)
                == "downgrade"
            )
            expected_comparisons = (1 if aur_base else 0) + (1 if nv_base else 0)
            actual_ahead_count = (1 if ahead_aur and aur_base else 0) + (
                1 if ahead_nv and nv_base else 0
            )
            if expected_comparisons > 0 and actual_ahead_count == expected_comparisons:
                pkg_entry["local_is_ahead"] = True
            elif expected_comparisons == 0:
                pkg_entry["local_is_ahead"] = True

        if pkg_entry["is_update"]:
            proc_logger.info(
                f"Update for {pkgbase_key} (Name {display_name}): to {pkg_entry['new_version_for_update']} via {pkg_entry['update_source']}"
            )
        elif pkg_entry["local_is_ahead"]:
            proc_logger.info(
                f"Local version for {pkgbase_key} (Name {display_name}) is ahead."
            )
        # elif not pkg_entry["errors"]:
        #    proc_logger.info(f"No update for {pkgbase_key} (Name {display_name}).")
        output_list.append(pkg_entry)
    return output_list


# --- Summary Function ---
def generate_summary(processed_output_list, stream=sys.stderr, logger=DEFAULT_LOGGER):
    summary_logger = logger.getChild("summary")
    (
        updates_nv,
        updates_aur,
        errors_list,
        nv_issues,
        up_to_date_count,
        local_ahead_list,
    ) = [], [], [], [], 0, []

    for pkg_data in processed_output_list:
        id_name = pkg_data.get("pkgbase", "UnknownBase")
        display_name_info = (
            f" (Name: {pkg_data.get('pkgname', id_name)})"
            if pkg_data.get("pkgname") != id_name
            else ""
        )

        if pkg_data["is_update"]:
            src = pkg_data["update_source"]
            new_ver = pkg_data["new_version_for_update"]
            old_ver_display = "N/A"

            if "nvchecker" in src:
                old_local_full = _get_full_version_string(
                    pkg_data.get("pkgver"), pkg_data.get("pkgrel")
                )
                old_aur_full = _get_full_version_string(
                    pkg_data.get("aur_pkgver"), pkg_data.get("aur_pkgrel")
                )
                if old_local_full:
                    old_ver_display = old_local_full
                elif old_aur_full:
                    old_ver_display = old_aur_full
            elif "aur" in src:
                old_local_full = _get_full_version_string(
                    pkg_data.get("pkgver"), pkg_data.get("pkgrel")
                )
                if old_local_full:
                    old_ver_display = old_local_full

            update_str = f"{id_name}{display_name_info} (from {old_ver_display} to {new_ver} via {src})"
            (updates_nv if "nvchecker" in src else updates_aur).append(update_str)

        elif pkg_data.get("local_is_ahead"):
            local_full_ver = _get_full_version_string(
                pkg_data.get("pkgver"), pkg_data.get("pkgrel")
            )
            local_ahead_list.append(
                f"{id_name}{display_name_info} (Local: {local_full_ver})"
            )
        elif not pkg_data["errors"]:
            up_to_date_count += 1

        if pkg_data["errors"]:
            errors_list.extend(
                [f"{id_name}{display_name_info}: {err}" for err in pkg_data["errors"]]
            )

        if (
            not pkg_data["is_update"]
            and not pkg_data["errors"]
            and not pkg_data.get("local_is_ahead")
        ):
            nv_raw_log = pkg_data.get("nvchecker_raw_log")
            if nv_raw_log:
                nv_event, nv_level = nv_raw_log.get("event"), nv_raw_log.get("level")
                if (
                    nv_event == "no-result"
                    or nv_level == "error"
                    or nv_raw_log.get("exc_info")
                ):
                    details = nv_raw_log.get(
                        "exc_info",
                        nv_raw_log.get("msg", f"Event: {nv_event}, Level: {nv_level}"),
                    )
                    nv_issues.append(
                        f"{id_name}{display_name_info}: NVChecker operational issue - {details}"
                    )

    lines = ["--- Package Update Summary ---"]
    if updates_nv:
        lines.extend(
            [f"\nNVChecker Driven Updates ({len(updates_nv)}):"]
            + [f"  - {s}" for s in updates_nv]
        )
    if updates_aur:
        lines.extend(
            [f"\nAUR Driven Updates ({len(updates_aur)}):"]
            + [f"  - {s}" for s in updates_aur]
        )
    if local_ahead_list:
        lines.extend(
            [f"\nLocal Packages Ahead of AUR/NVChecker ({len(local_ahead_list)}):"]
            + [f"  - {s}" for s in local_ahead_list]
        )
    if not updates_nv and not updates_aur and not local_ahead_list and not errors_list:
        lines.append("\nNo updates, local ahead, or errors.")
    elif not updates_nv and not updates_aur and not local_ahead_list:
        lines.append("\nNo updates or local ahead (check errors).")
    if up_to_date_count > 0:
        lines.append(f"\nPackages up-to-date or no other action: {up_to_date_count}")
    if errors_list:
        lines.extend(
            [f"\nVersion Discrepancies ({len(errors_list)}):"]
            + [f"  - {s}" for s in errors_list]
        )
    if nv_issues:
        lines.extend(
            [f"\nNVChecker Operational Issues ({len(nv_issues)}):"]
            + [f"  - {s}" for s in nv_issues]
        )
    # Use the passed-in stream for output, which defaults to sys.stderr
    # The logger is used for internal logging, but the final summary report is
    # directed to the specified stream.
    print("\n".join(lines), file=stream)


# --- Main Application Class ---
class AurPackageUpdater:
    def __init__(self, args, logger_instance=None):
        self.args = args
        self.logger = logger_instance or self._setup_default_logger()
        self.all_package_data_by_pkgbase = {}
        self._configure_logging()

    def _setup_default_logger(self):
        # This is a fallback for when the script is run standalone.
        handler = logging.StreamHandler(sys.stderr)
        formatter = logging.Formatter("%(levelname)s: %(name)s: %(message)s")
        handler.setFormatter(formatter)
        DEFAULT_LOGGER.addHandler(handler)
        DEFAULT_LOGGER.setLevel(logging.INFO)
        return DEFAULT_LOGGER

    def _configure_logging(self):
        # Set the level of the logger instance based on the debug flag.
        app_log_level = logging.DEBUG if self.args.debug else logging.INFO
        self.logger.setLevel(app_log_level)
        self.logger.info(
            f"App logger '{self.logger.name}' effective level: {logging.getLevelName(self.logger.getEffectiveLevel())}"
        )

    def run(self):
        manual_packages_list = None
        if self.args.manual_packages:
            try:
                manual_packages_list = json.loads(self.args.manual_packages)
                if not isinstance(manual_packages_list, list):
                    self.logger.error(
                        "--manual-packages argument is not a valid JSON list. Treating as empty."
                    )
                    manual_packages_list = []
                else:
                    self.logger.info(
                        f"Manual mode activated for packages: {manual_packages_list}"
                    )
            except json.JSONDecodeError:
                self.logger.error(
                    "Failed to decode JSON from --manual-packages. Ignoring."
                )
                manual_packages_list = []

        aur_data = get_combined_aur_data(self.args.maintainer, logger=self.logger)
        for pkgbase, data in aur_data.items():
            self.all_package_data_by_pkgbase.setdefault(pkgbase, {}).update(data)

        if not os.path.isdir(self.args.path_root):
            self.logger.critical(
                f"Path root '{self.args.path_root}' is not valid. Exiting."
            )
            sys.exit(1)
        local_data = fetch_local_pkgbuild_data(
            self.args.path_root,
            self.args.pkgbuild_script,
            manual_packages=manual_packages_list,
            logger=self.logger,
        )
        for pkgbase, data in local_data.items():
            self.all_package_data_by_pkgbase.setdefault(pkgbase, {}).update(data)

        nvchecker_oldver_input = {
            pb: {"version": d["aur_pkgver"]}
            for pb, d in self.all_package_data_by_pkgbase.items()
            if d.get("aur_pkgver")
        }
        nvchecker_data = run_nvchecker(
            self.args.path_root,
            nvchecker_oldver_input,
            self.args.key_toml,
            manual_packages=manual_packages_list,
            logger=self.logger,
        )
        for pkgbase, data in nvchecker_data.items():
            self.all_package_data_by_pkgbase.setdefault(pkgbase, {}).update(data)

        final_output_list = process_and_compare_data(
            self.all_package_data_by_pkgbase, logger=self.logger
        )

        output_stream = (
            open(self.args.output_file, "w") if self.args.output_file else sys.stdout
        )
        try:
            json.dump(final_output_list, output_stream, indent=2)
            if self.args.output_file:
                output_stream.write("\n")
                self.logger.info(f"JSON output written to {self.args.output_file}")
        finally:
            if self.args.output_file and output_stream is not sys.stdout:
                output_stream.close()

        if self.args.summary:
            generate_summary(final_output_list, stream=sys.stderr, logger=self.logger)


# --- CLI Argument Parsing and Main Execution ---
def main_cli():
    parser = argparse.ArgumentParser(
        description="AUR/local/NVChecker package update tool.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--maintainer", required=True, help="AUR maintainer name.")
    parser.add_argument(
        "--path-root",
        required=True,
        help="Root directory for PKGBUILDs and .nvchecker.toml files.",
    )
    parser.add_argument(
        "--pkgbuild-script",
        default="./pkgbuild_to_json.py",
        help="Script to parse PKGBUILDs to JSON.",
    )
    parser.add_argument(
        "--key-toml",
        default=None,
        help="Path to NVChecker's key.toml. Checks GITHUB_TOKEN env var if not set.",
    )
    parser.add_argument(
        "--manual-packages",
        default=None,
        help="JSON string of a list of package names to process manually.",
    )
    parser.add_argument(
        "--output-file", default=None, help="File for JSON output (default: STDOUT)."
    )
    parser.add_argument(
        "--summary", action="store_true", help="Print summary to STDERR."
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging.")
    args = parser.parse_args()
    app = AurPackageUpdater(args)
    try:
        app.run()
    except Exception as e:
        app.logger.critical(f"Unhandled exception: {e}", exc_info=True)
        sys.exit(2)


if __name__ == "__main__":
    main_cli()
