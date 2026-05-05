param(
    [string]$Name = "DownloaderYT"
)

$ErrorActionPreference = "Stop"

py -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name $Name `
    --collect-all yt_dlp `
    .\downloader_youtube.py
