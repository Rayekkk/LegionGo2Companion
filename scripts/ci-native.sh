#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
# The native Desktop app for the shared checks.yml (DeckyKit runs it before `npm run package`, which needs
# dist/desktop): built against the minimum supported Qt (= SteamOS's system Qt), its C++ and QML tests, and the same
# binaries on the newest Qt 6 runtime. Also fetches the release tags whose in-plugin updaters must accept the ZIP.
# CI only (Ubuntu 24.04 runner with sudo); locally use scripts/build-native.py as README.md describes.
set -euo pipefail
cd "$(dirname "$0")/.."
qt=/tmp/companion-qt
sudo apt-get update
sudo apt-get install -y cmake ninja-build g++ libgl1-mesa-dev libegl1-mesa-dev libfontconfig1 libxkbcommon-x11-0 \
  libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0 libxcb-xinerama0 libxcb-randr0 \
  libxcb-shape0 libxcb-xfixes0 libxcb-xkb1 libxcb-sync1 libwayland-client0 libwayland-cursor0 libwayland-egl1 \
  libgssapi-krb5-2 libbrotli1
# The Qt installer is a build tool, not plugin code: its own venv on the runner's system Python.
/usr/bin/python3 -m venv /tmp/aqt
/tmp/aqt/bin/pip install aqtinstall==3.3.0
/tmp/aqt/bin/aqt install-qt linux desktop 6.9.1 linux_gcc_64 -O "$qt"
/tmp/aqt/bin/aqt install-qt linux desktop 6.11.2 linux_gcc_64 -O "$qt"

node scripts/desktop-contract.mjs --check
python scripts/build-native.py --qt "$qt/6.9.1/gcc_64" --build /tmp/companion-native --test --bundle dist/desktop
python tests/native_capture.py --exe /tmp/companion-native/legiongo2companion --qt "$qt/6.9.1/gcc_64" \
  --output /tmp/companion-screens
(
  export LD_BIND_NOW=1 LD_LIBRARY_PATH="$qt/6.11.2/gcc_64/lib" QT_PLUGIN_PATH="$qt/6.11.2/gcc_64/plugins" \
    QML_IMPORT_PATH="$qt/6.11.2/gcc_64/qml"
  QT_QUICK_BACKEND=software ctest --test-dir /tmp/companion-native --output-on-failure
  python tests/native_capture.py --exe /tmp/companion-native/legiongo2companion --qt "$qt/6.11.2/gcc_64" \
    --output /tmp/companion-screens-newest
  node scripts/check-desktop-build.mjs --runtime
)
# scripts/check-legacy-updaters.py reads the updaters of these releases (the shared checkout has no tags).
git fetch --no-tags --depth=1 origin 'refs/tags/2.1.0:refs/tags/2.1.0' 'refs/tags/2.2.0:refs/tags/2.2.0'
