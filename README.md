# Downloader YT

Interface simples em Python para baixar videos e audios do YouTube usando `yt-dlp`.

## Recursos

- Interface grafica Tkinter e modo terminal interativo.
- Download de video ou audio com selecao de formato/qualidade.
- Suporte a playlists.
- Download de trechos com `--start` e `--end`.
- Modo rapido para audio recortado com `--fast-audio-cut`.
- Testes unitarios para utilitarios de tempo, URL e progresso.

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

Audio recortado em modo rapido, mantendo o formato original e sem converter:

```powershell
py downloader_youtube.py "URL_DO_YOUTUBE" --tipo audio --start 10:15 --end 12:30 --fast-audio-cut
```

## Gerar executavel

No Windows, rode:

```powershell
.\build_exe.ps1
```

O arquivo final sera criado em `dist/DownloaderYT.exe`.

## Desenvolvimento

Instale as dependencias de desenvolvimento e rode os testes:

```powershell
py -m pip install -e ".[dev]"
py -m pytest -q
```

## Observacoes

- O script instala `yt-dlp` automaticamente se precisar.
- `ffmpeg` melhora conversoes, mesclagem de audio/video e recortes.
- Trechos realmente recortados dependem de `ffmpeg`; `--fast-audio-cut` evita a conversao final para reduzir o tempo.
- No Windows, o script tenta encontrar instalacoes feitas pelo Winget.
