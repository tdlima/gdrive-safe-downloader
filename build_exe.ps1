# Gera dist\gdrive-downloader.exe com PyInstaller.
# Uso (PowerShell, na pasta do projeto, com o ambiente virtual ativado):
#   .\build_exe.ps1
$ErrorActionPreference = "Stop"
python -m pip install --upgrade "pyinstaller>=6.10"
python -m PyInstaller --noconfirm --clean --onefile `
    --name gdrive-downloader `
    --hidden-import keyring.backends.Windows `
    --copy-metadata keyring `
    main.py
Write-Host ""
Write-Host "Executável gerado em: dist\gdrive-downloader.exe"
Write-Host "Teste com: .\dist\gdrive-downloader.exe --help"
