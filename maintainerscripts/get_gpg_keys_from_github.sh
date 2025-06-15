#!/bin/bash
#
# v5 - Final robust version. Fetches unique GPG fingerprints from Git tags,
# then enriches them with canonical data (UID, expiration) from a keyserver.
#
# This provides clean, authoritative data for an Arch Linux PKGBUILD.

set -e
set -o pipefail

# --- Configuration ---
KEYSERVER="keys.openpgp.org"

# --- Helper Functions & Pre-flight Checks ---
usage() {
  cat <<EOF
Usage: $(basename "$0") <repository_url> [options]

Fetches unique GPG keys from signed Git tags for a PKGBUILD.

Arguments:
  repository_url        The URL of the git repository to check.

Options:
  --recent N            Check the N most recent tags.
  --tags T1 T2 ...      Check a specific, space-separated list of tags.
  --help, -h            Show this help message.

If no options are provided, the script defaults to checking the single most recent tag.
EOF
}

if ! command -v gpg &>/dev/null; then
  echo "Error: 'gpg' command not found. Please install GnuPG." >&2
  exit 1
fi

# --- Argument Parsing ---
# ... (argument parsing remains the same)
REPO_URL=""
RECENT_COUNT=0
declare -a TAG_ARRAY=()
MODE="default"

while [[ $# -gt 0 ]]; do
  key="$1"
  case $key in
  --recent)
    MODE="recent"
    RECENT_COUNT="$2"
    shift 2
    ;;
  --tags)
    MODE="tags"
    shift
    while [[ $# -gt 0 && ! "$1" =~ ^- ]]; do
      TAG_ARRAY+=("$1")
      shift
    done
    ;;
  -h | --help)
    usage
    exit 0
    ;;
  *)
    if [ -n "$REPO_URL" ]; then
      echo "Error: Unknown argument: $1" >&2
      usage
      exit 1
    fi
    REPO_URL="$1"
    shift
    ;;
  esac
done

if [ -z "$REPO_URL" ]; then
  echo "Error: Repository URL is required." >&2
  usage
  exit 1
fi

# --- Phase 1: Extract Fingerprints from Git Repository ---
echo "🔎 Phase 1: Analyzing Git tags in '$REPO_URL'..."

TMP_DIR=$(mktemp -d)
trap 'echo "🧹 Cleaning up..."; rm -rf "$TMP_DIR"' EXIT

echo "⏳ Cloning a temporary copy of the repository..."
git clone --bare --filter=blob:none "$REPO_URL" "$TMP_DIR/repo.git" >/dev/null 2>&1
cd "$TMP_DIR/repo.git"

declare -a tags_to_check
# ... (tag selection logic remains the same)
case $MODE in
recent)
  echo "🎯 Mode: Checking the $RECENT_COUNT most recent tags."
  tags_to_check=($(git tag --sort=-creatordate | head -n "$RECENT_COUNT" || true))
  ;;
tags)
  echo "🎯 Mode: Checking specified tags: ${TAG_ARRAY[*]}"
  tags_to_check=("${TAG_ARRAY[@]}")
  ;;
default)
  echo "🎯 Mode: Checking the single most recent tag (default)."
  tags_to_check=($(git tag --sort=-creatordate | head -n 1 || true))
  ;;
esac

if [ ${#tags_to_check[@]} -eq 0 ]; then
  echo "❌ No tags found to check."
  exit 0
fi

declare -A unique_fingerprints

for tag in "${tags_to_check[@]}"; do
  echo "  - Inspecting tag: $tag"
  tag_info=$(git verify-tag "$tag" 2>&1 || true)

  # Robustly extract a 40-character hex fingerprint. This is the key fix.
  fp=$(echo "$tag_info" | grep -oE '[A-F0-9]{40}' || true)

  if [ -n "$fp" ]; then
    echo "    ✅ Found fingerprint: ${fp: -16}"
    unique_fingerprints["$fp"]=1
  else
    echo "    ℹ️  Info: Tag '$tag' is not signed with a standard GPG key. Skipping."
  fi
done

if [ ${#unique_fingerprints[@]} -eq 0 ]; then
  echo "❌ No GPG fingerprints found in the specified tags."
  exit 1
fi

# --- Phase 2: Enrich Fingerprints with Data from Keyserver ---
echo
echo "🔎 Phase 2: Fetching full key details from keyserver '$KEYSERVER'..."
declare -A key_details

# Import all unique keys in one batch for efficiency
gpg_recv_output=$(gpg --batch --keyserver "$KEYSERVER" --recv-keys "${!unique_fingerprints[@]}" 2>&1)
echo "$gpg_recv_output" | grep -E 'gpg: key|gpg: importing' | sed 's/^/  /'

for fp in "${!unique_fingerprints[@]}"; do
  # Use GPG's machine-readable "colon" format to get data
  key_data=$(gpg --with-colons --fingerprint "$fp")

  # Get the primary User ID
  uid=$(echo "$key_data" | grep '^uid:' | head -n 1 | cut -d: -f10)
  # Get the expiration date (Unix timestamp)
  exp_ts=$(echo "$key_data" | grep '^pub:' | cut -d: -f7)

  exp_str="Never"
  if [ -n "$exp_ts" ]; then
    exp_str=$(date -d "@$exp_ts" --iso-8601)
  fi

  key_details["$fp"]="$uid (Expires: $exp_str)"
done

# --- Final Output ---
echo
echo "✅ Success! Found ${#key_details[@]} unique GPG key(s)."
echo "   Copy the following into your PKGBUILD:"
echo "------------------------------------------------------------"
echo "validpgpkeys=( "

for fp in "${!key_details[@]}"; do
  printf "              '%s' # %s\n" "$fp" "${key_details[$fp]}"
done

echo "             )"
echo "------------------------------------------------------------"
