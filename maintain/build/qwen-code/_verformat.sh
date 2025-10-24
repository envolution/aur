set -euo pipefail
source ./PKGBUILD

if [[ $pkgver =~ ^([0-9]+\.[0-9]+\.[0-9]+)([np])([0-9]+)$ ]]; then
  # extract base version, type (n/p), and number
  base_version="${BASH_REMATCH[1]}"
  type_char="${BASH_REMATCH[2]}"
  number="${BASH_REMATCH[3]}"

  # map n->nightly, p->preview
  [[ $type_char == "n" ]] && type_word="nightly" || type_word="preview"
  _upstream_ver="${base_version}-${type_word}.${number}"
elif [[ $pkgver =~ ^[0-9.]+$ ]]; then
  # numeric version, no transformation needed
  _upstream_ver="$pkgver"
else
  echo "Warning: Unexpected pkgver format: $pkgver" >&2
  exit 1
fi

sed -i "s/^_pkgver=.*/_pkgver=${_upstream_ver}/" PKGBUILD
