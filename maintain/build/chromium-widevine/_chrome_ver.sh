set -euo pipefail

source ./PKGBUILD
cat >./_chrome_ver.toml <<'EOF'
[google-chrome]
source = "apt"
mirror = "https://dl.google.com/linux/chrome/deb/"
pkg = "google-chrome-stable"
suite = "stable"
repo = "main"
strip_release = true
EOF

_chrome_ver=$(nvchecker --logger json -c ./_chrome_ver.toml | jq -r '.version')
rm ./_chrome_ver.toml
if [[ ! $_chrome_ver =~ ^[0-9]+(\.[0-9]+)*$ ]]; then
  echo "Error: chromium widevine nvchecker returned invalid chrome version '$_chrome_ver'" >&2
  exit 1
fi
sed -i "s/^_chrome_ver=.*/_chrome_ver=${_chrome_ver}/" PKGBUILD
