# Downloader YT

Interface simples em Python para baixar videos e audios do YouTube usando `yt-dlp`.

## Uso

Abra a interface grafica:

```powershell
py downloader_youtube.py
```

Modo terminal interativo:

```powershell
py downloader_youtube.py --cli
```

Download direto:

```powershell
py downloader_youtube.py "URL_DO_YOUTUBE" --tipo video
py downloader_youtube.py "URL_DO_YOUTUBE" --tipo audio --audio-format mp3
```

Baixar apenas um trecho:

```powershell
py downloader_youtube.py "URL_DO_YOUTUBE" --start 10:15 --end 12:30
```

## Observacoes

- O script instala `yt-dlp` automaticamente se precisar.
- `ffmpeg` melhora conversoes, mesclagem de audio/video e recortes.
- No Windows, o script tenta encontrar instalacoes feitas pelo Winget.
