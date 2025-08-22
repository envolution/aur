set -euo pipefail
source ./PKGBUILD

#  if pkgver contains 'n' (indicating nightly)
if [[ $pkgver =~ ^([0-9]+\.[0-9]+\.[0-9]+)n([0-9]+)$ ]]; then
  # extract base version and nightly number
  base_version="${BASH_REMATCH[1]}"
  nightly_number="${BASH_REMATCH[2]}"
  _upstream_ver="${base_version}-nightly.${nightly_number}"
elif [[ $pkgver =~ ^[0-9.]+$ ]]; then
  # numeric version, no transformation needed
  _upstream_ver="$pkgver"
else
  echo "Warning: Unexpected pkgver format: $pkgver"
  exit 0
fi

sed -i "s/^_pkgver=.*/_pkgver=${_upstream_ver}/" PKGBUILD
echo "Transformed: $_pkgver -> $_upstream_ver"
