#!/usr/bin/bash
#
# updpkgsums - update source checksums in-place in PKGBUILDs
#
# Copyright (C) 2012-2013 Dave Reisner <dreisner@archlinux.org>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

shopt -s extglob

declare -r myname='updpkgsums'
declare -r myver='1.10.13'

LIBRARY=${LIBRARY:-'/usr/share/makepkg'}

# Import libmakepkg
# shellcheck source=/dev/null
source "$LIBRARY"/util/schema.sh

usage() {
	cat <<EOF
${myname} v${myver}

Update checksums of a PKGBUILD file.

Usage: ${myname} [options] [build file]

Options:
  -h, --help     display this help message and exit
  -V, --version  display version information and exit
  -s, --skip     skip checksum generation for sources with 'SKIP' in the array

These options can be passed to makepkg:
  -m, --nocolor  do not colorize output
EOF
}

version() {
	printf "%s %s\n" "$myname" "$myver"
	echo 'Copyright (C) 2012-2013 Dave Reisner <dreisner@archlinux.org>'
}

die() {
	# shellcheck disable=SC2059
	printf "==> ERROR: $1\n" "${@:2}" >&2
	exit 1
}

MAKEPKG_OPTS=()
buildfile='PKGBUILD'
skip_sums=false

while (( "$#" )); do
	case "$1" in
		-m|--nocolor) MAKEPKG_OPTS+=("$1"); shift ;;
		-h|--help) usage; exit ;;
		-V|--version) version; exit ;;
		-s|--skip) skip_sums=true; shift ;;
		*) buildfile="$1"; break 2 ;;
	esac
done

if [[ ! -f $buildfile ]]; then
	die "%s not found or is not a file" "$buildfile"
fi

# Resolve any symlinks to avoid replacing the symlink with a file. But, we
# have to do this portably -- readlink's flags are inconsistent across OSes.
while [[ -L $buildfile ]]; do
	buildfile=$(readlink "$buildfile")
	if [[ $buildfile = */* ]]; then
		cd "${buildfile%/*}" || exit
		buildfile=${buildfile##*/}
	fi
done

# cd into the directory with the build file. This avoids creating random src/
# directories scattered about the filesystem, and avoids cases where we might
# not be able to write in the $PWD.
if [[ $buildfile = */* ]]; then
	cd "${buildfile%/*}" || exit
	buildfile=${buildfile##*/}
fi

# Check $PWD/ for permission to unlink the $buildfile and write a new one
if [[ ! -w . ]]; then
	die "No write permission in '%s'" "$PWD"
fi

# Generate the new sums
BUILDDIR=$(mktemp -d  "${TMPDIR:-/tmp}/updpkgsums.XXXXXX")
export BUILDDIR
newbuildfile=$(mktemp "${TMPDIR:-/tmp}/updpkgsums.XXXXXX")

trap 'rm -rf "$BUILDDIR" "$newbuildfile"' EXIT
# shellcheck disable=SC2154 # known_hash_algos imported via libmakepkg
sumtypes=$(IFS='|'; echo "${known_hash_algos[*]}")

# Create an associative array to map sumtypes to array names
declare -A sumtype_mapping
for sumtype in ${known_hash_algos[*]}; do
	sumtype_mapping[$sumtype]="${sumtype}sums"
done

# Get the original checksums and indices of 'SKIP' if requested
if $skip_sums; then
	original_sums=$(awk -v sumtypes="$sumtypes" '
		BEGIN {
			# Build a regex to match any of the sumtypes
			sumtype_regex = "^[[:blank:]]*(" sumtypes ")sums([_[:alnum:]]+)?=[[:blank:]]*\\(";
		}
		$0 ~ sumtype_regex {
			# Check if the line defines an empty array (more robust check)
			if ($0 ~ "=[[:blank:]]*\\([[:space:]]*\\)[[:blank:]]*$") {
			next; # Skip empty array lines
			}

			# Extract the sumtype
			match($0, sumtype_regex, arr);
			sumtype = arr[1];

			# Extract the array content
			gsub(/\\$/, "", $0);  # Remove trailing backslash if present
			gsub(/^[[:blank:]]*[^=]+=[[:blank:]]*/, "", $0); # Remove everything before opening parenthesis
			gsub(/\\)[[:blank:]]*$/, "", $0); # Remove closing parenthesis and trailing whitespace

			# Print the sumtype and array content
			if (length($0) > 0){
				print sumtype, $0;
			}
		}
	' "$buildfile")
	skip_indices=()
	skip_sumtypes=()
	IFS=$'\n' read -rd '' -a original_sums_array <<< "$original_sums"

	for line in "${original_sums_array[@]}"; do
		# Extract sumtype and content from the line using IFS
		IFS=' ' read -r sumtype content <<< "$line"

		# Store the sumtype for later use with skip_indices
		skip_sumtypes+=("$sumtype")

		# Get the actual array name from the mapping
		array_name="${sumtype_mapping[$sumtype]}"

		# Use nameref to access the array indirectly
		declare -n current_sums="$array_name"

		# Replace shell escapes with actual newlines in content
		content="${content//$'\134\134'/$'\134'}"
		content="${content//$'\134\040'/' '}"
		content="${content//$'\134\011'/$'\011'}"
		content="${content//$'\134\156'/$'\156'}"

		# Populate a temporary array with the content
		declare -a temp_array
		IFS=$'\n' read -ra temp_array <<< "$content"

		# Iterate over the temporary array to find 'SKIP' entries
		for ((i=0; i<${#temp_array[@]}; i++)); do
			if [[ "${temp_array[i]}" == "SKIP" ]]; then
				skip_indices+=("$i")
			fi
		done
	done
fi

newsums=$(makepkg -g -p "$buildfile" "${MAKEPKG_OPTS[@]}") || die 'Failed to generate new checksums'

if [[ -z $newsums ]]; then
	die "$buildfile does not contain sources to update"
fi

# Modify newsums if skip_sums is true
if $skip_sums; then
	IFS=$'\n' read -rd '' -a newsums_array <<< "$newsums"
	current_sumtype_index=0
	j=0
	for ((i=0; i<${#newsums_array[@]}; i++)); do
		# Identify lines that correspond to checksum array declarations
		if [[ "${newsums_array[i]}" =~ ^[[:blank:]]*([a-zA-Z0-9]+)sums= ]]; then
			# Extract sumtype from the line
			current_sumtype="${BASH_REMATCH[1]}"

			# If this sumtype was processed before, find corresponding skip_indices
			if [[ " ${skip_sumtypes[*]} " =~ " ${current_sumtype} " ]]; then
				while [[ "${skip_sumtypes[current_sumtype_index]}" != "$current_sumtype" ]]; do
					((current_sumtype_index++))
				done

				# Iterate through elements of current checksum array
				for ((k=i+1; k<${#newsums_array[@]}; k++)); do
					if [[ "${newsums_array[k]}" == ")" ]]; then
						break;
					fi
					if [[ $j -lt ${#skip_indices[@]} ]] && [[ $k -eq $((skip_indices[j] + i + 1)) ]]; then
						newsums_array[$k]="SKIP"
						((j++))
					fi
				done
				# Reset j for the next checksum type
				j=0
				# Increment the index for the next sumtype
				((current_sumtype_index++))
			fi
		fi
	done
	newsums=$(printf '%s\n' "${newsums_array[@]}")
fi

awk -v sumtypes="$sumtypes" -v newsums="$newsums" '
	$0 ~"^[[:blank:]]*(" sumtypes ")sums(_[^=]+)?\\+?=", $0 ~ "\\)[[:blank:]]*(#.*)?$" {
		if (!w) {
			print newsums
			w++
		}
		next
	}

	1
	END { if (!w) print newsums }
' "$buildfile" > "$newbuildfile" || die 'Failed to write new PKGBUILD'

# Rewrite the original buildfile. Use cat instead of mv/cp to preserve
# permissions implicitly.
if ! cat -- "$newbuildfile" >"$buildfile"; then
	die "Failed to update %s. The file has not been modified." "$buildfile"
fi
