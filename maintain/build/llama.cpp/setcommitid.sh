set -euo pipefail

source ./PKGBUILD

_commit_id=$(curl -sf "https://api.github.com/repos/ggml-org/llama.cpp/git/ref/tags/${pkgver}" |
  jq -r '.object.sha' | cut -c1-7)

[[ $pkgver =~ ([0-9]+) ]] && _build_number="${BASH_REMATCH[1]}"

sed -i "s/^_commit_id=.*/_commit_id=${_commit_id}/" PKGBUILD
sed -i "s/^_build_number=.*/_build_number=${_build_number}/" PKGBUILD
