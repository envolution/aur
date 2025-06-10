#!/usr/bin/env python3

import argparse
import subprocess
import json
import os
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed
import sys


def parse_pkgbuild_output(output_str: str) -> dict:
    data = {}
    current_key_map = {
        "PKGNAME": "pkgname",
        "PKGBASE": "pkgbase",
        "PKGVER": "pkgver",
        "PKGREL": "pkgrel",
        "DEPENDS": "depends",
        "MAKEDEPENDS": "makedepends",
        "CHECKDEPENDS": "checkdepends",
        "SOURCE": "sources",
        "VALIDPGPKEYS": "validpgpkeys",
    }

    active_key_internal = None
    active_key_json = None
    current_values = []

    lines = output_str.splitlines()

    for line in lines:
        line = line.strip()
        if not line and not active_key_internal:
            continue

        is_marker = False
        for internal_marker, json_key in current_key_map.items():
            if line == f"{internal_marker}_START":
                if active_key_internal:
                    print(
                        f"Warning: Encountered new START marker '{line}' while '{active_key_internal}' was active.",
                        file=sys.stderr,
                    )
                active_key_internal = internal_marker
                active_key_json = json_key
                current_values = []
                is_marker = True
                break
            elif line == f"{internal_marker}_END":
                if (
                    active_key_internal == internal_marker
                ):  # Correctly check against the active marker
                    if active_key_json in ["pkgname", "pkgbase", "pkgver", "pkgrel"]:
                        data[active_key_json] = (
                            current_values[0] if current_values else ""
                        )
                    else:
                        data[active_key_json] = [v for v in current_values if v]
                    active_key_internal = None
                    active_key_json = None
                    current_values = []
                    is_marker = True
                    break
                # If not the active marker, it might be a nested or badly ordered marker.
                elif (
                    active_key_internal is not None
                ):  # only warn if an active key was expecting its own end
                    print(
                        f"Warning: Mismatched END marker. Expected '{active_key_internal}_END', got '{line}'.",
                        file=sys.stderr,
                    )

        if not is_marker and active_key_internal:
            current_values.append(line)

    if active_key_internal and current_values:
        print(
            f"Warning: Data collection for '{active_key_internal}' might be incomplete (no END marker found).",
            file=sys.stderr,
        )
        if active_key_json in ["pkgname", "pkgbase", "pkgver", "pkgrel"]:
            data[active_key_json] = current_values[0] if current_values else ""
        else:
            data[active_key_json] = [v for v in current_values if v]

    return data


def process_single_pkgbuild(pkgbuild_filepath: Path) -> dict:
    pkgbuild_filepath_abs = pkgbuild_filepath.resolve()

    bash_script = f"""
    set -e

    unset pkgver depends makedepends checkdepends

    . "{pkgbuild_filepath_abs}"

    echo "PKGBASE_START"
    echo "${{pkgbase}}"
    echo "PKGBASE_END"

    echo "PKGNAME_START"
    echo "${{pkgname}}"
    echo "PKGNAME_END"

    echo "PKGVER_START"
    echo "${{pkgver:-}}"
    echo "PKGVER_END"

    echo "PKGREL_START"
    echo "${{pkgrel:-}}"
    echo "PKGREL_END"

    echo "DEPENDS_START"
    if [ "${{#depends[@]}}" -gt 0 ]; then printf '%s\\n' "${{depends[@]}}"; fi
    echo "DEPENDS_END"

    echo "MAKEDEPENDS_START"
    if [ "${{#makedepends[@]}}" -gt 0 ]; then printf '%s\\n' "${{makedepends[@]}}"; fi
    echo "MAKEDEPENDS_END"

    echo "CHECKDEPENDS_START"
    if [ "${{#checkdepends[@]}}" -gt 0 ]; then printf '%s\\n' "${{checkdepends[@]}}"; fi
    echo "CHECKDEPENDS_END"

    echo "VALIDPGPKEYS_START"
    if [ "${{#validpgpkeys[@]}}" -gt 0 ]; then printf '%s\\n' "${{validpgpkeys[@]}}"; fi
    echo "VALIDPGPKEYS_END"

    echo "SOURCE_START"
    if [ "${{#source[@]}}" -gt 0 ]; then printf '%s\\n' "${{source[@]}}"; fi
    echo "SOURCE_END"
    """

    try:
        result = subprocess.run(
            ["bash", "-c", bash_script],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )

        if result.returncode != 0:
            return {
                "pkgfile": str(pkgbuild_filepath),
                "error": f"Bash script failed with code {result.returncode}",
                "stderr": result.stderr.strip(),
            }

        parsed_data = parse_pkgbuild_output(result.stdout)

        if not parsed_data.get("pkgname") and not parsed_data.get("pkgbase"):
            return {
                "pkgfile": str(pkgbuild_filepath),
                "error": "Neither pkgname nor pkgbase could be extracted from PKGBUILD.",
            }

        parsed_data["pkgfile"] = str(pkgbuild_filepath)
        return parsed_data

    except subprocess.TimeoutExpired:
        return {"pkgfile": str(pkgbuild_filepath), "error": "Processing timed out."}
    except Exception as e:
        return {
            "pkgfile": str(pkgbuild_filepath),
            "error": f"An unexpected error occurred: {str(e)}",
        }


def main():
    parser = argparse.ArgumentParser(
        description="Source PKGBUILD files and extract variables as JSON."
    )
    parser.add_argument(
        "pkgbuild_files",
        metavar="FILE",
        type=Path,
        nargs="+",
        help="Path to one or more PKGBUILD files.",
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=os.cpu_count() or 1,
        help="Number of bash processes to spawn concurrently (default: number of CPU cores).",
    )

    args = parser.parse_args()

    if args.jobs < 1:
        parser.error("Number of jobs must be at least 1.")

    results = []

    with ProcessPoolExecutor(max_workers=args.jobs) as executor:
        future_to_pkgbuild = {
            executor.submit(process_single_pkgbuild, filepath): filepath
            for filepath in args.pkgbuild_files
        }

        for future in as_completed(future_to_pkgbuild):
            pkgbuild_file = future_to_pkgbuild[future]
            try:
                data = future.result()
                results.append(data)
            except Exception as exc:
                results.append(
                    {
                        "pkgfile": str(pkgbuild_file),
                        "error": f"Worker process for {pkgbuild_file} generated an exception: {exc}",
                    }
                )

    results.sort(key=lambda x: x.get("pkgfile", ""))
    json.dump(results, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
