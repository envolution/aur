# Maintainer: Carl Smedstad <carsme@archlinux.org>

pkgname=fb303-git
_pkgname=fb303
pkgver=2024.10.28.00
pkgrel=1
pkgdesc="thrift functions that provide uerying stats and other information from a service"
arch=(x86_64)
url="https://github.com/facebook/fb303"
license=(Apache-2.0)
depends=(
  fbthrift
  fmt
  folly
  gcc-libs
  gflags
  glibc
  google-glog
  python
)
makedepends=(
  boost
  cmake
  mvfst
  git
)
conflicts=(fb303)
provides=(
  "fb303=${pkgver%%+*}"
  libfb303.so
  libfb303_thrift_cpp.so
)
options=(!lto)
source=(git+https://github.com/facebook/fb303.git)
sha256sums=('522f4ba3eb8781c72eeb62896606be72d85753321bbe495903f3b8eed9c19253')

pkgver() {
  cd "$srcdir/$_pkgname"
  _version=$(git describe --tags --abbrev=0 | tr - .)
  _commits=$(git rev-list --count HEAD)
  _short_commit_hash=$(git rev-parse --short=9 HEAD)
  echo "${_version#'v'}+r${_commits}+g${_short_commit_hash}"
}

prepare() {
  cd $_pkgname
  # Use system CMake config instead of bundled module
  sed -i 's/find_package(Glog MODULE REQUIRED)/find_package(Glog CONFIG REQUIRED)/' \
    CMakeLists.txt
}

build() {
  cd $_pkgname
  cmake -S . -B build \
    -DCMAKE_BUILD_TYPE=None \
    -DCMAKE_INSTALL_PREFIX=/usr \
    -Wno-dev \
    -DBUILD_SHARED_LIBS=ON \
    -DPYTHON_EXTENSIONS=ON \
    -DPACKAGE_VERSION="$pkgver"
  cmake --build build
}

package() {
  cd $_pkgname
  DESTDIR="$pkgdir" cmake --install build

  # Remove empty dirs to silence namcap warnings
  rm -vr "$pkgdir/usr/include/fb303/test" || true
  rm -vr "$pkgdir/usr/include/fb303/thrift/clients" || true
  rm -vr "$pkgdir/usr/include/fb303/thrift/services" || true
}
