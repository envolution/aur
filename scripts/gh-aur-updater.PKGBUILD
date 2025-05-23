pkgname=gh-aur-updater
pkgver=0.1.0
pkgrel=1
pkgdesc="GitHub-integrated AUR package updater"
arch=('any')
url="https://github.com/envolution/gh-aur-updater"
license=('MIT')
depends=('python' 'python-requests')
makedepends=('python-build' 'python-installer' 'python-wheel' 'python-setuptools'
  python-pytest python-responses git)
source=("git+$url.git")
sha256sums=('SKIP')

build() {
  cd $pkgname
  python -m build --wheel --no-isolation
}

package() {
  cd $pkgname
  python -m installer --destdir="$pkgdir" dist/*.whl
}
# vim:set ts=2 sw=2 et:
