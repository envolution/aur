set -euo pipefail

source ./PKGBUILD
_source=$(curl -s https://raw.githubusercontent.com/mozilla-firefox/firefox/refs/heads/main/toolkit/content/gmp-sources/widevinecdm.json |
  jq -r '.vendors["gmp-widevinecdm"].platforms["Linux_x86_64-gcc3"].mirrorUrls[0]')
sed -i "s|^source=.*|source=(${_source})|" PKGBUILD
