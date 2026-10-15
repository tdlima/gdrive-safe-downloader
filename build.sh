#!/usr/bin/env bash
# Gera dist/gdrive-downloader (executável único para Linux) com PyInstaller.
# Uso, na pasta do projeto com o ambiente virtual ativado:
#   ./build.sh
set -euo pipefail
python -m pip install --upgrade "pyinstaller>=6.10"
python -m PyInstaller --noconfirm --clean --onefile \
    --name gdrive-downloader \
    --hidden-import keyring.backends.SecretService \
    --copy-metadata keyring \
    main.py
echo
echo "Executável gerado em: dist/gdrive-downloader"
echo "Teste com: ./dist/gdrive-downloader --help"
