#!/usr/bin/env bash
# Gera dist/MonitorIQOption.exe a partir do Linux, com Python do Windows rodando no Wine (Docker).
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)

docker run --rm -v "$ROOT":/src -w /src tobix/pywine:3.12 bash -c '
    set -e
    wine python -m pip install --quiet -r windows/requirements-windows.txt
    wine pyinstaller --noconfirm --clean --onefile --windowed --noupx \
        --name MonitorIQOption \
        --paths . \
        --add-data "Z:/src/patterns/patterns.json;patterns" \
        --collect-data tzdata \
        --collect-submodules iqoptionapi \
        --distpath dist --workpath build --specpath build \
        windows/iq_monitor.py
    chown -R '"$(id -u):$(id -g)"' dist build
'

echo "Pronto: $ROOT/dist/MonitorIQOption.exe"
