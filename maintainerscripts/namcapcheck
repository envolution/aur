#!/bin/bash

# --- Configuration ---
pkgbuild_file="./PKGBUILD"
namcap_log_file="./namcap.log"
# --- End Configuration ---

# --- Helper Function ---
# Checks if an element exists in an array
# Usage: array_contains "element" "${array[@]}"
array_contains() {
    local needle="$1"
    shift
    local hay
    for hay in "$@"; do
        [[ "$hay" == "$needle" ]] && return 0
    done
    return 1
}

# --- Safety Check ---
if [[ ! -f "$pkgbuild_file" ]]; then
    echo "Error: PKGBUILD file not found at '$pkgbuild_file'"
    exit 1
fi
if [[ ! -f "$namcap_log_file" ]]; then
    echo "Error: Namcap log file not found at '$namcap_log_file'"
    exit 1
fi

# generate the namcap log
namcap *zst > namcap.log

# --- Load PKGBUILD Dependencies ---
# Source PKGBUILD in a subshell to avoid polluting current environment
# Use declare -p to safely get array definitions
declare -a depends=()
declare -a makedepends=()
declare -a optdepends=()
declare -a checkdepends=() # Add checkdepends if you use it

eval "$(source "$pkgbuild_file" >/dev/null 2>&1; \
        declare -p depends makedepends optdepends checkdepends 2>/dev/null)"

# Combine all known dependencies for easier checking
declare -a all_current_deps
all_current_deps=("${depends[@]}" "${makedepends[@]}" "${optdepends[@]}" "${checkdepends[@]}")
# Make unique
all_current_deps=($(printf "%s\n" "${all_current_deps[@]}" | sort -u))

echo "--- Current Dependencies from PKGBUILD ---"
echo "depends:      (${depends[*]})"
echo "makedepends:  (${makedepends[*]})"
echo "optdepends:   (${optdepends[*]})"
echo "checkdepends: (${checkdepends[*]})" # Include if used
echo "------------------------------------------"
echo

# --- Analyze Namcap Log ---

echo "--- Namcap Analysis ---"

# 1. Required Dependencies (E: Dependency ...)
echo "[Required Dependencies (From 'E: Dependency ...')]
Based on namcap errors, the following seem REQUIRED but are MISSING:"
missing_required=()
while IFS= read -r line; do
    dep_name=$(echo "$line" | sed -n "s/.*E: Dependency \([^ ]*\).*/\1/p")
    if [[ -n "$dep_name" ]]; then
        if ! array_contains "$dep_name" "${all_current_deps[@]}"; then
             # Heuristic: Quote if it doesn't look like a simple package name
             if [[ "$dep_name" =~ [^a-zA-Z0-9_-] ]]; then
                 missing_required+=("'$dep_name'")
             else
                 missing_required+=("$dep_name")
             fi
        fi
    fi
done < <(grep -E "^[^ ]+ E: Dependency .* detected and not included" "$namcap_log_file")

if [[ ${#missing_required[@]} -gt 0 ]]; then
    printf "  - %s\n" "${missing_required[@]}"
    echo "-> Consider adding these to the 'depends' array in your PKGBUILD."
else
    echo "  (None missing based on E: errors)"
fi
echo

# 2. Potential Optional/Check Dependencies (W: Referenced python module...)
echo "[Potential Optional/Check Dependencies (From 'W: Referenced python module ...')]
Namcap found references to modules from these packages. They might be needed
for specific features, benchmarks, or tests. Check if they are already present
or if they should be added to 'optdepends' or 'checkdepends'."

# List of common packages often seen in these warnings
declare -A potential_optdep_map=(
    ["triton"]="python-triton" # Often needed for runtime/build
    ["pytorch_lightning"]="python-pytorch-lightning"
    ["einops"]="python-einops"
    ["timm"]="python-timm"
    ["fvcore"]="python-fvcore"
    ["submitit"]="python-submitit"
    ["hydra"]="python-hydra"
    ["pytest"]="python-pytest"
    ["pandas"]="python-pandas"
    ["matplotlib"]="python-matplotlib"
    ["seaborn"]="python-seaborn"
    ["tqdm"]="python-tqdm"
    ["pynvml"]="python-pynvml"
    ["sentencepiece"]="python-sentencepiece" # Already caught by E: error
    ["torchvision"]="python-torchvision" # Already caught by E: error
    ["safetensors"]="python-safetensors" # Already caught by E: error
    ["transformers"]="python-transformers" # Already caught by E: error
    ["dcgm"]="python-pydcgm" # Or similar depending on exact import (dcgm_fields, dcgm_structs)
    # Add more mappings if needed based on the W: warnings
)
# Flash-attn is often vendored, ignore for now unless explicitly needed.
# Internal xformers.* modules are ignored.

missing_potential=()
checked_potential_pkgs=() # Avoid duplicate suggestions
while IFS= read -r line; do
    module_ref=$(echo "$line" | sed -n "s/.*W: Referenced python module '\([^']*\)'.*/\1/p")
    # Extract top-level module (e.g., 'transformers' from 'transformers.utils.hub')
    top_module=$(echo "$module_ref" | cut -d. -f1)

    # Skip internal refs and flash_attn (heuristic)
    if [[ "$top_module" == "xformers" || "$top_module" == "flash_attn" || "$top_module" == "flash_attn_2_cuda" || "$top_module" == "flash_attn_cuda" || "$top_module" == "fused_dense_lib" || "$top_module" == "fused_softmax_lib" || "$top_module" == "dropout_layer_norm" || "$top_module" == "utils" ]]; then
        continue
    fi

    pkg_name="${potential_optdep_map[$top_module]}"

    if [[ -n "$pkg_name" ]]; then
       if ! array_contains "$pkg_name" "${checked_potential_pkgs[@]}"; then
            checked_potential_pkgs+=("$pkg_name") # Mark as checked
            if ! array_contains "$pkg_name" "${all_current_deps[@]}"; then
                missing_potential+=("$pkg_name (needed for module: $top_module)")
            fi
       fi
    # else
    #     # Optionally list unmapped top-level modules for manual review
    #     # echo "  - Unmapped module found: $top_module (from $module_ref)"
    fi
done < <(grep "W: Referenced python module" "$namcap_log_file")


if [[ ${#missing_potential[@]} -gt 0 ]]; then
    printf "  - %s\n" "${missing_potential[@]}"
    echo "-> Consider adding these to 'optdepends' (most likely) or 'checkdepends' (e.g., pytest)."
else
    echo "  (No common external packages detected as missing based on W: module warnings)"
fi
echo

# 3. Library Dependencies (W: Referenced library ...)
echo "[Library Dependencies (From 'W: Referenced library ...')]
These libraries were found linked. Ensure the packages providing them are listed,
usually via main dependencies like pytorch-cuda and cuda."
missing_libs=()
declare -A lib_map=(
    ["libcudart.so"]="cuda" # Or cuda-toolkit
    ["libtorch_cuda.so"]="pytorch-cuda"
    ["libc10_cuda.so"]="pytorch-cuda"
    # Add others if needed
)
checked_lib_pkgs=()
while IFS= read -r line; do
    lib_name=$(echo "$line" | sed -n "s/.*W: Referenced library '\([^']*\)'.*/\1/p" | sed 's/\.[0-9.]*$//') # Strip version numbers like .so.12
     pkg_name="${lib_map[$lib_name]}"
      if [[ -n "$pkg_name" ]]; then
        if ! array_contains "$pkg_name" "${checked_lib_pkgs[@]}"; then
            checked_lib_pkgs+=("$pkg_name")
            if ! array_contains "$pkg_name" "${all_current_deps[@]}"; then
                 # Heuristic: Quote if it doesn't look like a simple package name
                 if [[ "$pkg_name" =~ [^a-zA-Z0-9_-] ]]; then
                     missing_libs+=("'$pkg_name' (provides library matching '$lib_name')")
                 else
                     missing_libs+=("$pkg_name (provides library matching '$lib_name')")
                 fi

            fi
        fi
    # else
        # echo "  - Unmapped library reference: $lib_name*"
    fi
done < <(grep "W: Referenced library" "$namcap_log_file")

if [[ ${#missing_libs[@]} -gt 0 ]]; then
    printf "  - %s\n" "${missing_libs[@]}"
    echo "-> Ensure these packages (or equivalents) are in 'depends'."
else
    echo "  (Required library providers seem covered by existing dependencies like pytorch-cuda/cuda)"
fi
echo

# 4. Potentially Unused Dependencies
echo "[Potentially Unused Dependencies]
Checking if dependencies listed in PKGBUILD were mentioned by namcap...
(Note: Namcap might not detect all uses, especially for build tools or indirect usage. Review carefully!)"
potentially_unused=()
for dep in "${all_current_deps[@]}"; do
    # Simplify check: grep for the package name (might need refinement for complex cases)
    # Remove potential quotes added earlier
    clean_dep=$(echo "$dep" | sed "s/^'//;s/'$//")
    # Skip base/implicit packages that namcap might ignore
    if [[ "$clean_dep" == "glibc" || "$clean_dep" == "gcc-libs" || "$clean_dep" == "python" ]]; then
        continue
    fi

    # Check if the dependency name appears in any relevant namcap line
    if ! grep -q -E "(E:|W:) (Dependency|Referenced python module|Referenced library).*${clean_dep}" "$namcap_log_file"; then
        # Add heuristic check for provides (e.g. if PKGBUILD lists 'cuda' but namcap mentions 'libcudart')
        is_mentioned=false
        for lib_pkg in "${!lib_map[@]}"; do
            if [[ "${lib_map[$lib_pkg]}" == "$clean_dep" ]]; then
                 if grep -q "Referenced library.*$lib_pkg" "$namcap_log_file"; then
                     is_mentioned=true
                     break
                 fi
            fi
        done
        # Add similar check for python modules if needed

        if ! $is_mentioned; then
           potentially_unused+=("$dep")
        fi
    fi
done

if [[ ${#potentially_unused[@]} -gt 0 ]]; then
    printf "  - %s\n" "${potentially_unused[@]}"
    echo "-> These dependencies were listed in the PKGBUILD but not explicitly mentioned as needed by namcap. Double-check if they are truly required (e.g., for build, optional features not scanned, or indirect needs)."
else
    echo "  (All listed dependencies seem to be mentioned by namcap in some way, or are base packages)"
fi
echo

echo "--- Other Issues Noted by Namcap ---"
grep -E "^[^ ]+ E: Uncommon license" "$namcap_log_file" | sed 's/^/  - FIX LICENSE: /'
grep -E "^[^ ]+ W: ELF file .* lacks (FULL RELRO|GNU_PROPERTY_X86_FEATURE_1_SHSTK)" "$namcap_log_file" | sed 's/^/  - BUILD FLAG?: /' | sort -u

echo "------------------------------------"
echo "Analysis complete. Review the suggestions above."
