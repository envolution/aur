#!/bin/bash

set -euo pipefail

# Ensure the script receives exactly 7 arguments
if [ "$#" -ne 7 ]; then
	echo "[debug] Usage: $0 <GITHUB_REPO> <GITHUB_TOKEN> <GITHUB_WORKSPACE> <BUILD:0:1> <PACKAGE_NAME> <PKGBUILD_PATH> <COMMIT_MESSAGE>"
	exit 1
fi

log_array() {
	local array_name="$1"
	shift
	local array=("$@")

	echo "[debug] Logging array: $array_name"
	for index in "${!array[@]}"; do
		echo "[debug] [$index]: ${array[$index]}"
	done
}

# Define variables
GITHUB_REPOSITORY="$1"
GH_TOKEN="$2"
GITHUB_WORKSPACE="$3"
BUILD=$4
PACKAGE_NAME="$5"
PKGBUILD_PATH="$6"
COMMIT_MESSAGE="$7"
RELEASE_BODY="To install, run: sudo pacman -U PACKAGENAME.pkg.tar.zst"
AUR_REPO="ssh://aur@aur.archlinux.org/${PACKAGE_NAME}.git"
FAILURE=0
INITIAL=0

# Enable debugging if DEBUG environment variable is set
if [ "${DEBUG:-}" == "true" ]; then
	set -x
fi

# Authenticate using the GitHub token
echo "[debug] === Auth to GH ==="
echo "${GH_TOKEN}" | gh auth login --with-token
if gh auth status &>/dev/null; then
	echo "[debug] GitHub CLI is authenticated"
else
	echo "[debug] ===!!! GitHub CLI is not authenticated !!!==="
	exit 1
fi

# Create and move to a clean build directory
BUILD_DIR="/tmp/build-${PACKAGE_NAME}"
rm -rf "${BUILD_DIR}"
mkdir -p "${BUILD_DIR}"
cd "${BUILD_DIR}"

# Clone AUR repository
echo "[debug] Cloning AUR repository for ${PACKAGE_NAME}..."
git clone "$AUR_REPO" .

TRACKED_FILES=()

# Collect PKGBUILD from our repo
if [ -f "${GITHUB_WORKSPACE}/${PKGBUILD_PATH}/PKGBUILD" ]; then
	echo "[debug] Copying PKGBUILD and others from ${PKGBUILD_PATH}"
	cp "${GITHUB_WORKSPACE}/${PKGBUILD_PATH}/PKGBUILD" .
	TRACKED_FILES+=("PKGBUILD")
fi
# Collect .nvchecker.toml from our repo
if [ -f "${GITHUB_WORKSPACE}/${PKGBUILD_PATH}/.nvchecker.toml" ]; then
	echo "[debug] Copying .nvchecker.toml and others from ${PKGBUILD_PATH}"
	cp "${GITHUB_WORKSPACE}/${PKGBUILD_PATH}/.nvchecker.toml" .
	TRACKED_FILES+=(".nvchecker.toml")
fi

#get the source array directly from our PKGBUILD.  We want to get all of our sources from this.
readarray -t SOURCES < <(bash -c 'source PKGBUILD; printf "%s\n" "${source[@]}"' | grep .)
readarray -t DEPENDS < <(bash -c 'source PKGBUILD; printf "%s\n" "${depends[@]}"' | grep .)
readarray -t MAKEDEPENDS < <(bash -c 'source PKGBUILD; printf "%s\n" "${makedepends[@]}"' | grep .)
readarray -t CHECKDEPENDS < <(bash -c 'source PKGBUILD; printf "%s\n" "${checkdepends[@]}"' | grep .)
readarray -t PGPKEYS < <(bash -c 'source PKGBUILD; printf "%s\n" "${validpgpkeys[@]}"' | grep .)
readarray -t PACKAGES < <(bash -c 'source PKGBUILD; printf "%s\n" "${pkgname[@]}"' | grep .)

[[ ${#SOURCES[@]} -gt 0 ]] && log_array "SOURCES" "${SOURCES[@]}" ||
	echo "[debug] !!!== No sources in PKGBUILD, this is probably not intended =="
[[ ${#DEPENDS[@]} -gt 0 ]] && log_array "DEPENDS" "${DEPENDS[@]}" ||
	echo "[debug] == No depends in PKGBUILD =="
[[ ${#MAKEDEPENDS[@]} -gt 0 ]] && log_array "MAKEDEPENDS" "${MAKEDEPENDS[@]}" ||
	echo "[debug] == No make depends in PKGBUILD =="
[[ ${#CHECKDEPENDS[@]} -gt 0 ]] && log_array "CHECKDEPENDS" "${CHECKDEPENDS[@]}" ||
	echo "[debug] == No check depends in PKGBUILD =="
[[ ${#PGPKEYS[@]} -gt 0 ]] && gpg --receive-keys "${PGPKEYS[@]}" &&
	echo "[debug] == Adopted package PGP keys ==" ||
	echo "[debug] == No PGP keys in PKGBUILD =="

if [[ ${#SOURCES[@]} -gt 1 ]]; then
	echo "[debug] == There is more than one source in PKGBUILD =="
	# Iterate over the array and copy our source files one at a time.  Avoid URLs
	for item in "${SOURCES[@]}"; do
		if [[ "$item" != *[/:]* ]]; then
			echo "[debug] \"$item\" identified possible file"
			if [ -f "${GITHUB_WORKSPACE}/${PKGBUILD_PATH}/${item}" ]; then
				cp "${GITHUB_WORKSPACE}/${PKGBUILD_PATH}/${item}" . &&
					TRACKED_FILES+=("${item}") #add this file for git push
			fi
		else
			echo "[debug] ${item} is an invalid file (probably a url)"
		fi
	done
else
	echo "[debug] == PKGBUILD source array looks like just one item =="
fi

# Check for a TOML version file
if [ -f "${GITHUB_WORKSPACE}/${PKGBUILD_PATH}/.nvchecker.toml" ]; then
	NEW_VERSION=$(nvchecker -c .nvchecker.toml --logger json | jq -r 'select(.logger_name == "nvchecker.core") | .version')
	[[ -z $NEW_VERSION ]] &&
		echo "[debug] !! ${GITHUB_WORKSPACE}/${PKGBUILD_PATH}/.nvchecker.toml exists, but it's giving errors." &&
		exit 1
	echo "[debug] == UPDATE DETECTED ${NEW_VERSION} from upstream, PKGBUILD updating... =="
	sed -i "s|pkgver=.*|pkgver=${NEW_VERSION}|" PKGBUILD
	cat PKGBUILD
fi

# Update source files
echo "[debug] == Updating package checksums =="
updpkgsums

# Generate .SRCINFO
echo "[debug] == Generating .SRCINFO =="
makepkg --printsrcinfo >.SRCINFO
TRACKED_FILES+=(".SRCINFO")

# Check for changes and commit
echo "[debug] == Checking for changes to commit =="
echo "[debug] == staging current files to compare against remote for changes =="
log_array "TRACKED_FILES" "${TRACKED_FILES[@]}"
git rm --cached .gitignore || true
git add -f "${TRACKED_FILES[@]}"

if [ -z "$(git rev-parse --verify HEAD 2>/dev/null)" ]; then
	echo "[debug] == Initial commit, committing selected files =="
	git commit -m "${COMMIT_MESSAGE}: ${NEW_VERSION:-}"
	git push
	INITIAL=1
fi

if git diff-index --cached --quiet HEAD -- && [ $INITIAL -ne 1 ] && [[ ${PACKAGE_NAME} != *-git ]] && [ "$BUILD" != "test" ]; then
	echo "[debug] == No changes detected. Skipping commit and push =="
else

	echo "[debug] == Changes detected. Committing and pushing selected files =="
	if [ "$BUILD" == "build" ] || [ "$BUILD" == "test" ]; then

		echo "[debug] == ${PACKAGE_NAME} has been configured to be compiled and installed before pushing =="

		#Install package dependancies if they exist
		if [[ ${#DEPENDS[@]} -gt 0 ]] && paru -S --needed --norebuild --noconfirm --mflags "--skipchecksums --skippgpcheck" "${DEPENDS[@]}"; then
			echo "[debug] == Package dependencies installed successfully =="
		else
			echo "[debug] == FAIL Package dependency installation failed (this should not cause issues as makepkg will try again but won't have access to AUR) =="
		fi

		#Install package make dependancies if they exist
		if [[ ${#MAKEDEPENDS[@]} -gt 0 ]] && paru -S --needed --norebuild --noconfirm --mflags "--skipchecksums --skippgpcheck" "${MAKEDEPENDS[@]}"; then
			echo "[debug] == Package make dependencies installed successfully =="
		else
			echo "[debug] == FAIL Package make dependency installation failed (this should not cause issues as makepkg will try again but won't have access to AUR) =="
		fi

		#Install package check dependancies if they exist
		if [[ ${#CHECKDEPENDS[@]} -gt 0 ]] && paru -S --needed --norebuild --noconfirm --mflags "--skipchecksums --skippgpcheck" "${CHECKDEPENDS[@]}"; then
			echo "[debug] == Package check dependencies installed successfully =="
		else
			echo "[debug] == FAIL Package make dependency installation failed (this should not cause issues as makepkg will try again but won't have access to AUR) =="
		fi

		# Build package
		echo "[debug] Building package..."
		if makepkg -s --noconfirm; then
			echo "[debug] == Package ${PACKAGE_NAME} built successfully =="
		else
			echo "[debug] == FAIL makepkg build of ${PACKAGE_NAME} failed (skipping commit) =="
			FAILURE=1
		fi

		# Install the package
		echo "[debug] == Installing package '${PACKAGE_NAME}' and attempting to auto resolve any conflicts =="
		sudo rm -f "${PACKAGE_NAME}"*debug*pkg.tar.zst || true
		ls -latr

		if sudo pacman --noconfirm -U *.pkg.tar.zst; then
			if [ "${#PACKAGES[@]}" -gt 1 ]; then
				echo "[debug] == Package ${PACKAGE_NAME} installed successfully, it's a multi package so not removing it due to possible complexities =="
			else
				echo "[debug] == Package ${PACKAGE_NAME} installed successfully, attempting to remove it =="
				sudo pacman --noconfirm -R "$(expac --timefmt=%s '%l\t%n' | sort | cut -f2 | xargs -r pacman -Q | cut -f1 -d' ' | tail -n 1)"
			fi
			# Create a new release
			if [ "$BUILD" != "test" ]; then
				echo "[debug] === Push compiled binary to releases ==="
				gh release create "${PACKAGE_NAME}" --title "Binary installers for ${PACKAGE_NAME}" --notes "${RELEASE_BODY}" -R "${GITHUB_REPOSITORY}" ||
				echo "[debug] == Assuming tag ${PACKAGE_NAME} exists as we can't create one =="
				for PACKAGE in "${PACKAGES[@]}"; do
					gh release upload "${PACKAGE_NAME}" ./"${PACKAGE}"*.pkg.tar.zst --clobber -R "${GITHUB_REPOSITORY}"
				done
			fi
		else
			echo "[debug] == FAIL install of ${PACKAGE_NAME} failed (skipping commit) =="
			FAILURE=1
		fi
	fi

	if [ $FAILURE = 0 ]; then

		git fetch
		# Stage tracked files that have changes
		echo "[debug] == ATTEMPTING TO TRACK CHANGES FOR FILES =="
		log_array "TRACKED_FILES" "${TRACKED_FILES[@]}"
		git add "${TRACKED_FILES[@]}"

		if [[ -z $(git status --porcelain --untracked-files=no) ]] && [ "$BUILD" != "test" ]; then
			echo "[debug] == AUR and LOCAL already synced for ${PACKAGE_NAME} =="
		else
			if [ "$BUILD" != "test" ]; then
				git commit -m "${COMMIT_MESSAGE}: ${NEW_VERSION:-}"
				if git push origin master; then
					echo "[debug] == ${PACKAGE_NAME} submitted to AUR successfully =="
					# We update our local PKGBUILD now since we've confirmed an update to remote AUR
					for file in "${TRACKED_FILES[@]}"; do
						echo "[debug] [debug] - LOOPING $file - Still ok "
						if [[ -f "$file" ]]; then
						    _ret=0
							# Check if the file exists in the remote repository
							echo "[debug] [debug] - 1 - Still ok "
							sha=$(gh api "/repos/${GITHUB_REPOSITORY}/contents/${PKGBUILD_PATH}/${file}" --jq '.sha' 2>/dev/null) || true
							echo "[debug] [debug] - 2 - Still ok "
							if [[ -n "$sha" ]]; then
								echo "[debug] [debug] - 3 - Still ok "
								# File exists, update it
								filesha=$(git hash-object "$file")
								if [[ "$sha" != "$filesha" ]]; then
									echo "[debug] local ${file} != /contents/${PKGBUILD_PATH}/${file} replacing..."
									gh api -X PUT "/repos/${GITHUB_REPOSITORY}/contents/${PKGBUILD_PATH}/${file}" \
										-f message="Auto updated ${file} in ${GITHUB_REPOSITORY} while syncing to AUR" \
										-f content="$(base64 <"${file}")" \
										--jq '.commit.sha' \
										-f sha="$sha"
									_ret=$?
								else
									echo "[debug] $file with same sha on remote.  Skipping ..."
								fi
							else
								# File does not exist, create it
								echo "[debug] [debug] - 4 - Still ok "
								gh api -X PUT "/repos/${GITHUB_REPOSITORY}/contents/${PKGBUILD_PATH}${file}" \
									-f message="Added ${file} to ${GITHUB_REPOSITORY}" \
									-f content="$(base64 <"${file}")" \
									--jq '.commit.sha'
								_ret=$?
							fi
							if [[ $_ret -eq 0 ]]; then
								echo "[debug] ==${file} pushed to ${GITHUB_REPOSITORY}/${PKGBUILD_PATH} successfully =="
							else
								echo "[debug] !! FAILED on ${file} push to ${GITHUB_REPOSITORY}/${PKGBUILD_PATH} !!"
							fi

						else
							echo "[debug] !! ${file} is in our tracked files but doesn't appear to be a file (something is wrong mate) !!"
						fi
					done
					echo "[debug] == local PKGBUILD updated =="
				else
					echo "[debug] == FAILED ${PACKAGE_NAME} submission to AUR =="
					FAILURE=1
				fi
			else
				echo "[debug] == Test for $PACKAGE_NAME concluded =="
			fi
		fi

	fi

fi

if [ $FAILURE -eq 0 ]; then
	echo "[debug] ==== ${PACKAGE_NAME} processed without detected errors ===="
else
	echo "[debug] **** ${PACKAGE_NAME} has had some errors while processing, check logs for more details ****"
fi
echo "[debug] ==== Build and release process for ${PACKAGE_NAME} exiting ===="
