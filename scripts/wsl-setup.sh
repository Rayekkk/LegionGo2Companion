#!/usr/bin/env bash
# One-time WSL2 (Ubuntu 24.04) dev setup matching CI: Python 3.14, Node 24, native Qt build deps.
# Run inside WSL: bash scripts/wsl-setup.sh   (asks for your Linux sudo password)
set -euo pipefail
sudo apt-get update
sudo apt-get install -y software-properties-common ca-certificates curl gnupg
sudo add-apt-repository -y ppa:deadsnakes/ppa
curl -fsSL https://deb.nodesource.com/setup_24.x | sudo -E bash -
sudo apt-get install -y python3.14 nodejs zip \
  cmake ninja-build g++ libgl1-mesa-dev libegl1-mesa-dev libfontconfig1 libxkbcommon-x11-0 libxcb-cursor0 \
  libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0 libxcb-xinerama0 libxcb-randr0 libxcb-shape0 \
  libxcb-xfixes0 libxcb-xkb1 libxcb-sync1 libwayland-client0 libwayland-cursor0 libwayland-egl1 libgssapi-krb5-2 libbrotli1
python3.14 --version; node --version
echo WSL_SETUP_OK
