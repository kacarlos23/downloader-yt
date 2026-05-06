#!/usr/bin/env python
"""
Downloader unico para videos e audios do YouTube.

Uso rapido:
  py downloader_youtube.py
  py downloader_youtube.py --cli
  py downloader_youtube.py "https://www.youtube.com/watch?v=..." --tipo video
  py downloader_youtube.py "https://www.youtube.com/watch?v=..." --tipo audio --audio-format mp3
  py downloader_youtube.py "https://www.youtube.com/watch?v=..." --start 10:15 --end 12:30

Observacao:
  - O script instala yt-dlp automaticamente se ele nao estiver disponivel.
  - ffmpeg melhora video em alta qualidade e conversao para mp3, mas nao e obrigatorio.
  - No Windows, use --install-ffmpeg para instalar ffmpeg via winget.
"""

from __future__ import annotations

import argparse
import importlib.util
import logging
import os
import re
import signal
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable


MessageFunc = Callable[[str], None]
ProgressFunc = Callable[[dict[str, Any]], None]


class Config:
    """Constantes e configuracoes da aplicacao."""

    APP_NAME = "Downloader YT"
    DEFAULT_OUTPUT_DIR = Path.cwd() / "downloads"
    LOGGER_NAME = "downloader_yt"
    AUDIO_FORMATS = ("mp3", "m4a", "opus", "wav", "flac")
    VIDEO_QUALITIES = ("best", "1080", "720", "480", "360")


APP_NAME = Config.APP_NAME
DEFAULT_OUTPUT_DIR = Config.DEFAULT_OUTPUT_DIR


class MediaType(str, Enum):
    """Tipos de midia suportados pelo downloader."""

    VIDEO = "video"
    AUDIO = "audio"


class AudioFormat(str, Enum):
    """Formatos de audio expostos pela CLI e GUI."""

    MP3 = "mp3"
    M4A = "m4a"
    OPUS = "opus"
    WAV = "wav"
    FLAC = "flac"


class VideoQuality(str, Enum):
    """Qualidades de video predefinidas na GUI."""

    BEST = "best"
    P1080 = "1080"
    P720 = "720"
    P480 = "480"
    P360 = "360"


@dataclass(frozen=True)
class DownloadOptions:
    """Opcoes de download normalizadas para uso interno."""

    url: str
    media_type: str
    output_dir: Path
    audio_format: str
    video_quality: str
    playlist: bool
    install_ffmpeg: bool
    time_range: tuple[float, float] | None
    precise_cut: bool
    fast_audio_cut: bool = False


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Configura e retorna o logger padrao da aplicacao.

    Args:
        level: Nivel minimo das mensagens registradas.

    Returns:
        Logger configurado para escrever no stdout.
    """

    logger = logging.getLogger(Config.LOGGER_NAME)
    logger.setLevel(level)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
        logger.addHandler(handler)
    return logger


logger = setup_logging()


class PlatformUtils:
    """Utilitarios para diferencas entre sistemas operacionais."""

    @staticmethod
    def is_windows() -> bool:
        """Retorna True quando o processo esta rodando no Windows."""

        return os.name == "nt"

    @staticmethod
    def open_folder(path: Path, message_func: MessageFunc = print) -> bool:
        """Abre uma pasta no gerenciador de arquivos da plataforma.

        Args:
            path: Pasta que deve ser aberta.
            message_func: Callback para mensagens de erro.

        Returns:
            True quando o comando foi iniciado com sucesso.
        """

        try:
            if PlatformUtils.is_windows():
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.run(["open", str(path)], check=True)
            else:
                subprocess.run(["xdg-open", str(path)], check=True)
            return True
        except Exception as exc:
            message_func(f"[erro] Nao foi possivel abrir a pasta: {exc}")
            return False

    @staticmethod
    def get_subprocess_creation_flags(*, hide_window: bool = False) -> int:
        """Retorna flags adequadas para subprocess na plataforma atual."""

        if not PlatformUtils.is_windows():
            return 0
        if hide_window and hasattr(subprocess, "CREATE_NO_WINDOW"):
            return subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
            return subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        return 0


class ThreadManager:
    """Gerencia uma unica thread ativa com exclusao mutua."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self, target: Callable[[], None], *, daemon: bool = True) -> bool:
        """Inicia a thread se nao houver outra em execucao."""

        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self._thread = threading.Thread(target=target, daemon=daemon)
            self._thread.start()
            return True

    def is_running(self) -> bool:
        """Retorna True quando a thread gerenciada ainda esta viva."""

        with self._lock:
            return self._thread is not None and self._thread.is_alive()


class ProcessRunner:
    """Helpers para execucao de subprocessos e threads."""

    @staticmethod
    def run_in_thread(
        target: Callable[[], None],
        on_complete: Callable[[], None] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> threading.Thread:
        """Executa uma funcao em thread daemon e notifica conclusao/erro."""

        def wrapper() -> None:
            try:
                target()
            except BaseException as exc:
                if on_error:
                    on_error(exc)
                else:
                    raise
            else:
                if on_complete:
                    on_complete()

        thread = threading.Thread(target=wrapper, daemon=True)
        thread.start()
        return thread

    @staticmethod
    def stream_output(stream: Any, callback: Callable[[str, bool], None], *, is_error: bool = False) -> None:
        """Le um stream de texto e envia linhas/retornos de carro para callback."""

        buffer = ""
        while True:
            char = stream.read(1)
            if not char:
                break
            if char in {"\r", "\n"}:
                callback(buffer, is_error)
                buffer = ""
            else:
                buffer += char
                if len(buffer) >= 1000:
                    callback(buffer, is_error)
                    buffer = ""
        callback(buffer, is_error)


class DependencyInstaller:
    """Instalador injetavel de dependencias Python."""

    def __init__(self, pip_cmd: list[str] | None = None) -> None:
        self.pip_cmd = pip_cmd or [sys.executable, "-m", "pip", "install"]

    def install_if_missing(
        self,
        package_name: str,
        import_name: str | None = None,
        message_func: MessageFunc = print,
    ) -> bool:
        """Instala uma dependencia quando o modulo importavel nao existe."""

        module_name = import_name or package_name
        if importlib.util.find_spec(module_name) is not None:
            return True
        if getattr(sys, "frozen", False):
            return False

        message_func(f"[setup] Instalando dependencia Python: {package_name}")
        result = run_command([*self.pip_cmd, "--upgrade", package_name], check=False)
        return result.returncode == 0


def configure_console_output() -> None:
    """Configura stdout/stderr para tolerar caracteres nao representaveis."""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(errors="replace")


configure_console_output()


def run_command(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Executa um comando externo em modo texto."""

    return subprocess.run(command, check=check, text=True)


def ensure_package(package_name: str, import_name: str | None = None, message_func: MessageFunc = print) -> None:
    """Garante que um pacote Python esteja disponivel para importacao."""

    module_name = import_name or package_name
    if importlib.util.find_spec(module_name) is not None:
        return

    if getattr(sys, "frozen", False):
        raise RuntimeError(f"A biblioteca {package_name} não foi encontrada no executável.")

    message_func(f"[setup] Instalando dependencia Python: {package_name}")
    run_command([sys.executable, "-m", "pip", "install", "--upgrade", package_name])


def import_yt_dlp(message_func: MessageFunc = print) -> Any:
    ensure_package("yt-dlp", "yt_dlp", message_func=message_func)
    import yt_dlp

    return yt_dlp


def has_command(command: str) -> bool:
    return shutil.which(command) is not None


def find_ffmpeg_bin_dir() -> Path | None:
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        return Path(ffmpeg_path).parent

    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return None

    local_ffmpeg = Path(local_app_data) / "Programs" / "ffmpeg" / "ffmpeg.exe"
    if local_ffmpeg.exists():
        return local_ffmpeg.parent

    winget_packages = Path(local_app_data) / "Microsoft" / "WinGet" / "Packages"
    if not winget_packages.exists():
        return None

    matches = sorted(winget_packages.glob("Gyan.FFmpeg*/ffmpeg-*/bin/ffmpeg.exe"), reverse=True)
    if not matches:
        return None

    return matches[0].parent


def find_deno_bin_dir() -> Path | None:
    deno_path = shutil.which("deno")
    if deno_path:
        return Path(deno_path).parent

    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return None

    winget_packages = Path(local_app_data) / "Microsoft" / "WinGet" / "Packages"
    if not winget_packages.exists():
        return None

    matches = sorted(winget_packages.glob("DenoLand.Deno*/deno.exe"), reverse=True)
    if not matches:
        return None

    return matches[0].parent


def add_to_process_path(path: Path) -> None:
    current_paths = os.environ.get("PATH", "").split(os.pathsep)
    path_text = str(path)
    if path_text.lower() not in {item.lower() for item in current_paths}:
        os.environ["PATH"] = path_text + os.pathsep + os.environ.get("PATH", "")


def install_ffmpeg_with_winget(message_func: MessageFunc = print) -> bool:
    if not has_command("winget"):
        message_func("[ffmpeg] winget nao foi encontrado. Instale ffmpeg manualmente:")
        message_func("         https://ffmpeg.org/download.html")
        return False

    message_func("[ffmpeg] Instalando ffmpeg via winget...")
    command = [
        "winget",
        "install",
        "--id",
        "Gyan.FFmpeg",
        "-e",
        "--accept-source-agreements",
        "--accept-package-agreements",
    ]
    result = run_command(command, check=False)
    if result.returncode == 0:
        message_func("[ffmpeg] Instalacao concluida. Se o comando ainda nao aparecer, reabra o terminal.")
        return True

    message_func("[ffmpeg] Nao foi possivel instalar automaticamente via winget.")
    return False


def ensure_ffmpeg(*, install: bool, required: bool, message_func: MessageFunc = print) -> bool:
    ffmpeg_bin_dir = find_ffmpeg_bin_dir()
    if ffmpeg_bin_dir:
        add_to_process_path(ffmpeg_bin_dir)
        return True

    if has_command("ffmpeg"):
        return True

    if install:
        installed = install_ffmpeg_with_winget(message_func=message_func)
        ffmpeg_bin_dir = find_ffmpeg_bin_dir()
        if ffmpeg_bin_dir:
            add_to_process_path(ffmpeg_bin_dir)
            return True
        if installed:
            message_func("[ffmpeg] Reabra o terminal e rode o script novamente para usar ffmpeg.")
        return False

    if required:
        message_func("[ffmpeg] ffmpeg nao foi encontrado. Vou usar um modo sem conversao/mesclagem.")
        message_func("         Rode: py downloader_youtube.py --install-ffmpeg")
        message_func("         Ou instale manualmente: https://ffmpeg.org/download.html")
    else:
        message_func("[ffmpeg] ffmpeg nao encontrado. O download de video ainda pode funcionar,")
        message_func("         mas a qualidade/mesclagem pode ser limitada.")
    return False


def use_deno_if_available() -> bool:
    deno_bin_dir = find_deno_bin_dir()
    if deno_bin_dir:
        add_to_process_path(deno_bin_dir)
        return True
    return False


def parse_timecode(value: str, *, label: str) -> float:
    """Converte string de tempo para segundos.

    Args:
        value: String nos formatos SS, MM:SS, HH:MM:SS ou fim/final.
        label: Rotulo usado nas mensagens de erro.

    Returns:
        Total em segundos, ou infinito para fim/final.

    Raises:
        ValueError: Se o formato for invalido ou negativo.
    """

    value = value.strip().lower()
    if value in {"inf", "infinite", "fim", "final"}:
        return float("inf")

    parts = value.split(":")
    if not 1 <= len(parts) <= 3 or any(part == "" for part in parts):
        raise ValueError(f"{label} deve estar no formato segundos, MM:SS ou HH:MM:SS.")

    try:
        total = 0.0
        for part in parts:
            total = total * 60 + float(part)
    except ValueError as exc:
        raise ValueError(f"{label} contem um horario invalido: {value}") from exc

    if total < 0:
        raise ValueError(f"{label} nao pode ser negativo.")
    return total


def build_time_range(start: str | None, end: str | None) -> tuple[float, float] | None:
    """Retorna tupla de inicio/fim em segundos ou None quando nao ha trecho."""

    if not start and not end:
        return None

    start_seconds = parse_timecode(start or "0", label="--start")
    end_seconds = parse_timecode(end or "inf", label="--end")
    if end_seconds <= start_seconds:
        raise ValueError("--end deve ser maior que --start.")
    return start_seconds, end_seconds


def format_time(seconds: float) -> str:
    """Formata segundos como MM:SS, HH:MM:SS ou fim."""

    if seconds == float("inf"):
        return "fim"

    rounded = int(seconds)
    hours, remainder = divmod(rounded, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def parse_ffmpeg_progress_time(text: str) -> float | None:
    """Extrai o marcador time=HH:MM:SS.xx de uma linha do ffmpeg."""

    match = re.search(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def time_range_duration(time_range: tuple[float, float] | None) -> float | None:
    """Retorna a duracao finita de um trecho, quando disponivel."""

    if not time_range:
        return None
    start_seconds, end_seconds = time_range
    if end_seconds == float("inf"):
        return None
    return max(0.0, end_seconds - start_seconds)


def validate_youtube_url(url: str) -> bool:
    """Valida se a string parece uma URL do YouTube.

    Args:
        url: Texto informado pelo usuario.

    Returns:
        True para URLs plausiveis do YouTube, incluindo videos, shorts, lives,
        canais, handles e playlists.
    """

    patterns = (
        r"^https?://(www\.|m\.)?youtube\.com/watch\?.*v=[\w-]+",
        r"^https?://(www\.|m\.)?youtube\.com/playlist\?.*list=[\w-]+",
        r"^https?://(www\.|m\.)?youtube\.com/shorts/[\w-]+",
        r"^https?://(www\.|m\.)?youtube\.com/live/[\w-]+",
        r"^https?://(www\.|m\.)?youtube\.com/(channel|c|user)/[\w@.-]+/?(\?.*)?$",
        r"^https?://(www\.|m\.)?youtube\.com/@[\w@.-]+/?(\?.*)?$",
        r"^https?://music\.youtube\.com/watch\?.*v=[\w-]+",
        r"^https?://youtu\.be/[\w-]+/?(\?.*)?$",
    )
    return any(re.match(pattern, url.strip(), flags=re.IGNORECASE) for pattern in patterns)


def setup_signal_handlers(cancel_callback: Callable[[], None]) -> None:
    """Registra handlers para Ctrl+C e SIGTERM.

    Args:
        cancel_callback: Funcao chamada antes de encerrar o processo.
    """

    def handler(signum: int, frame: Any) -> None:
        del signum, frame
        print("\nCancelando operacao...")
        cancel_callback()
        raise SystemExit(130)

    signal.signal(signal.SIGINT, handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handler)


def make_download_ranges(time_range: tuple[float, float]) -> Callable[[dict[str, Any], Any], Any]:
    """Cria callback de ranges usado pelo yt-dlp."""

    start_seconds, end_seconds = time_range

    def download_ranges(info_dict: dict[str, Any], ydl: Any) -> list[dict[str, float]]:
        segment: dict[str, float] = {"start_time": start_seconds}
        # Só envia o end_time se ele não for infinito
        if end_seconds != float("inf"):
            segment["end_time"] = end_seconds
        return [segment]

    return download_ranges


def build_output_template(output_dir: Path) -> str:
    """Cria template de saida do yt-dlp e garante que a pasta exista."""

    output_dir.mkdir(parents=True, exist_ok=True)
    return str(output_dir / "%(title).180B [%(id)s].%(ext)s")


def build_video_format(video_quality: str, *, ffmpeg_available: bool) -> str:
    """Monta seletor de formato de video compativel com a disponibilidade do ffmpeg."""

    if not ffmpeg_available:
        if video_quality == "best":
            return "best[ext=mp4]/best"
        return f"best[height<={video_quality}][ext=mp4]/best[height<={video_quality}]/best"

    compatible_audio = "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]"
    if video_quality == "best":
        return (
            f"bestvideo[ext=mp4]+({compatible_audio})/"
            f"bestvideo[vcodec^=avc1]+({compatible_audio})/"
            "best[ext=mp4]/best"
        )

    return (
        f"bestvideo[height<={video_quality}][ext=mp4]+({compatible_audio})/"
        f"bestvideo[height<={video_quality}][vcodec^=avc1]+({compatible_audio})/"
        f"best[height<={video_quality}][ext=mp4]/best[height<={video_quality}]/best"
    )


def make_progress_hook(
    message_func: MessageFunc = print,
    progress_func: ProgressFunc | None = None,
) -> ProgressFunc:
    """Cria hook de progresso para CLI ou GUI."""

    def progress_hook(status: dict[str, Any]) -> None:
        if progress_func:
            progress_func(status)

        if status.get("status") == "downloading":
            filename = Path(status.get("filename", "")).name
            percent = status.get("_percent_str", "").strip()
            speed = status.get("_speed_str", "").strip()
            eta = status.get("_eta_str", "").strip()
            parts = [part for part in [percent, speed, f"ETA {eta}" if eta else ""] if part]
            message = f"Baixando {filename}: {' | '.join(parts)}"
            if progress_func:
                return
            print(f"\r{message}", end="", flush=True)
        elif status.get("status") == "finished":
            message_func("\nDownload concluido. Processando arquivo..." if progress_func is None else "Download concluido. Processando arquivo...")

    return progress_hook


def make_postprocessor_hook(message_func: MessageFunc = print) -> ProgressFunc:
    """Cria hook para informar etapas longas de pos-processamento do yt-dlp."""

    def postprocessor_hook(status: dict[str, Any]) -> None:
        state = status.get("status")
        postprocessor = status.get("postprocessor") or "pos-processamento"
        if state == "started":
            message_func(f"[processamento] Iniciando {postprocessor}...")
        elif state == "finished":
            message_func(f"[processamento] {postprocessor} concluido.")

    return postprocessor_hook


class YtdlpLogger:
    """Adaptador simples para mensagens do yt-dlp."""

    def __init__(self, message_func: MessageFunc):
        self.message_func = message_func

    def debug(self, message: str) -> None:
        return None

    def warning(self, message: str) -> None:
        self.message_func(f"[aviso] {message}")

    def error(self, message: str) -> None:
        self.message_func(f"[erro] {message}")


class YtDlpManager:
    """Encapsula configuracao e chamadas ao yt-dlp."""

    def __init__(self, message_func: MessageFunc = print, progress_func: ProgressFunc | None = None) -> None:
        self.message_func = message_func
        self.progress_func = progress_func
        self.yt_dlp = import_yt_dlp(message_func=message_func)

    def extract_info(self, url: str, playlist: bool) -> dict[str, Any]:
        """Carrega metadados de uma URL sem baixar midia.

        Args:
            url: URL do YouTube.
            playlist: Quando True, permite extrair playlist inteira.

        Returns:
            Dicionario de metadados retornado pelo yt-dlp.
        """

        deno_available = use_deno_if_available()
        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": not playlist,
            "logger": YtdlpLogger(self.message_func),
        }
        if deno_available:
            options["remote_components"] = ["ejs:github"]
        with self.yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
        return dict(info or {})

    def download(self, options: DownloadOptions) -> None:
        """Baixa midia conforme as opcoes recebidas."""

        if not validate_youtube_url(options.url):
            raise ValueError("URL invalida. Cole uma URL do YouTube valida.")

        deno_available = use_deno_if_available()
        ffmpeg_required = bool(options.time_range) or options.media_type == "audio" or options.video_quality == "best"
        ffmpeg_available = ensure_ffmpeg(
            install=options.install_ffmpeg,
            required=ffmpeg_required,
            message_func=self.message_func,
        )
        if options.time_range and not ffmpeg_available:
            raise SystemExit("[trecho] Baixar apenas uma faixa precisa de ffmpeg.")

        ytdlp_options: dict[str, Any] = {
            "outtmpl": build_output_template(options.output_dir),
            "noplaylist": not options.playlist,
            "progress_hooks": [make_progress_hook(self.message_func, self.progress_func)],
            "postprocessor_hooks": [make_postprocessor_hook(self.message_func)],
            "windowsfilenames": True,
            "ignoreerrors": options.playlist,
            "retries": 10,
            "fragment_retries": 10,
        }
        if self.progress_func:
            ytdlp_options["quiet"] = True
            ytdlp_options["logger"] = YtdlpLogger(self.message_func)
        if deno_available:
            ytdlp_options["remote_components"] = ["ejs:github"]
        if options.time_range:
            ytdlp_options["download_ranges"] = make_download_ranges(options.time_range)
            ytdlp_options["force_keyframes_at_cuts"] = options.precise_cut
            start_display, end_display = (format_time(value) for value in options.time_range)
            self.message_func(f"[trecho] Baixando apenas de {start_display} ate {end_display}.")
            if options.precise_cut:
                self.message_func("[trecho] Corte preciso ativado; o processamento pode demorar mais.")

        if options.media_type == "audio":
            ytdlp_options["format"] = "bestaudio/best"
            if ffmpeg_available and options.fast_audio_cut and options.time_range:
                self.message_func(
                    "[audio] Modo rapido ativado: baixando o trecho no formato original, sem converter audio."
                )
                self.message_func("[audio] Isso evita a etapa lenta de extracao/conversao depois do corte.")
            elif ffmpeg_available:
                if options.time_range:
                    self.message_func("[audio] Recortando o trecho e convertendo o audio; esta etapa pode demorar.")
                ytdlp_options["postprocessors"] = [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": options.audio_format,
                        "preferredquality": "192",
                    }
                ]
            else:
                self.message_func("[audio] Sem ffmpeg: baixando o melhor audio original, sem converter formato.")
        else:
            ytdlp_options["format"] = build_video_format(options.video_quality, ffmpeg_available=ffmpeg_available)
            if ffmpeg_available:
                ytdlp_options["merge_output_format"] = "mp4"
                ytdlp_options["postprocessor_args"] = {
                    "merger+ffmpeg_o": ["-c:a", "aac", "-b:a", "192k"],
                }
                self.message_func("[video] O audio do MP4 final sera mantido/convertido para AAC, mais compativel.")
            elif options.video_quality == "best":
                self.message_func("[video] Sem ffmpeg: baixando um arquivo unico quando disponivel.")
            else:
                self.message_func("[video] Sem ffmpeg: baixando um arquivo unico quando disponivel.")

        logger.info("Iniciando download de %s", options.url)
        self.message_func(f"[{APP_NAME}] Salvando em: {options.output_dir}")
        with self.yt_dlp.YoutubeDL(ytdlp_options) as ydl:
            ydl.download([options.url])

        self.message_func(f"[{APP_NAME}] Pronto.")


def download_media(
    url: str,
    *,
    media_type: str,
    output_dir: Path,
    audio_format: str,
    video_quality: str,
    playlist: bool,
    install_ffmpeg: bool,
    start_time: str | None,
    end_time: str | None,
    precise_cut: bool,
    fast_audio_cut: bool = False,
    message_func: MessageFunc = print,
    progress_func: ProgressFunc | None = None,
) -> None:
    time_range = build_time_range(start_time, end_time)
    download_options = DownloadOptions(
        url=url,
        media_type=media_type,
        output_dir=output_dir,
        audio_format=audio_format,
        video_quality=video_quality,
        playlist=playlist,
        install_ffmpeg=install_ffmpeg,
        time_range=time_range,
        precise_cut=precise_cut,
        fast_audio_cut=fast_audio_cut,
    )
    YtDlpManager(message_func=message_func, progress_func=progress_func).download(download_options)


def launch_gui() -> None:
    try:
        import queue
        import threading
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except ImportError:
        print("[interface] Tkinter nao esta disponivel. Abrindo modo terminal.")
        args = interactive_args()
        download_media(
            args.url,
            media_type=args.tipo,
            output_dir=args.output.expanduser(),
            audio_format=args.audio_format,
            video_quality=str(args.video_quality).lower(),
            playlist=args.playlist,
            install_ffmpeg=args.install_ffmpeg,
            start_time=args.start,
            end_time=args.end,
            precise_cut=args.precise_cut,
            fast_audio_cut=getattr(args, "fast_audio_cut", False),
        )
        return

    THEME = {
        "primary": "#2563eb",
        "primary_hover": "#1d4ed8",
        "primary_disabled": "#93c5fd",
        "success": "#059669",
        "success_hover": "#047857",
        "success_bg": "#d1fae5",
        "danger": "#dc2626",
        "danger_hover": "#b91c1c",
        "warning": "#d97706",
        "bg_primary": "#f8fafc",
        "bg_secondary": "#ffffff",
        "bg_tertiary": "#f1f5f9",
        "text_primary": "#1e293b",
        "text_secondary": "#64748b",
        "text_disabled": "#94a3b8",
        "text_inverse": "#ffffff",
        "border": "#e2e8f0",
        "border_focus": "#3b82f6",
        "progress_bg": "#e0e7ff",
        "progress_fill": "#4f46e5",
    }

    class StyleConfig:
        """Configuracao centralizada de estilos ttk."""

        def __init__(self, window: tk.Tk) -> None:
            self.root = window
            self.style = ttk.Style()
            self.font_title = ("Segoe UI", 18, "bold")
            self.font_section = ("Segoe UI", 10, "bold")
            self.font_normal = ("Segoe UI", 10)
            self.font_small = ("Segoe UI", 9)
            self.font_button = ("Segoe UI", 10, "bold")
            self._configure_theme()

        def _configure_theme(self) -> None:
            available_themes = self.style.theme_names()
            for theme in ("vista", "clam", "alt", "default"):
                if theme in available_themes:
                    self.style.theme_use(theme)
                    break
            self.root.configure(bg=THEME["bg_primary"])
            self._configure_styles()

        def _configure_styles(self) -> None:
            style = self.style
            style.configure("TFrame", background=THEME["bg_primary"])
            style.configure("Card.TFrame", background=THEME["bg_secondary"], relief="flat")
            style.configure(
                "TLabel",
                background=THEME["bg_primary"],
                foreground=THEME["text_primary"],
                font=self.font_normal,
            )
            style.configure("Title.TLabel", font=self.font_title, foreground=THEME["text_primary"])
            style.configure("Secondary.TLabel", foreground=THEME["text_secondary"], font=self.font_small)
            style.configure(
                "Card.TLabel",
                background=THEME["bg_secondary"],
                foreground=THEME["text_primary"],
                font=self.font_normal,
            )
            style.configure(
                "CardSecondary.TLabel",
                background=THEME["bg_secondary"],
                foreground=THEME["text_secondary"],
                font=self.font_small,
            )
            style.configure("TButton", padding=(12, 8), font=self.font_button)
            style.configure(
                "TLabelframe",
                background=THEME["bg_secondary"],
                bordercolor=THEME["border"],
                relief="solid",
            )
            style.configure(
                "TLabelframe.Label",
                background=THEME["bg_primary"],
                foreground=THEME["text_primary"],
                font=self.font_section,
                padding=(8, 4),
            )
            style.configure("TEntry", padding=6, font=self.font_normal)
            style.configure("TCombobox", padding=6, font=self.font_normal)
            style.configure(
                "TCheckbutton",
                background=THEME["bg_secondary"],
                foreground=THEME["text_primary"],
                font=self.font_normal,
                padding=4,
            )
            style.map("TCheckbutton", background=[("active", THEME["bg_secondary"])])
            style.configure(
                "TRadiobutton",
                background=THEME["bg_secondary"],
                foreground=THEME["text_primary"],
                font=self.font_normal,
                padding=4,
            )
            style.map("TRadiobutton", background=[("active", THEME["bg_secondary"])])
            style.configure(
                "TProgressbar",
                background=THEME["progress_fill"],
                troughcolor=THEME["progress_bg"],
                bordercolor=THEME["progress_bg"],
                lightcolor=THEME["progress_fill"],
                darkcolor=THEME["progress_fill"],
                thickness=10,
            )
            style.configure("TScrollbar", background=THEME["bg_tertiary"], troughcolor=THEME["border"])

    class StyledButton(tk.Button):
        """Botao com estilo moderno e feedback visual."""

        def __init__(self, parent: Any, text: str, command: Callable[[], None] | None = None, variant: str = "primary", **kwargs: Any) -> None:
            self._variant = variant
            self._colors = self._get_variant_colors(variant)
            default_kwargs = {
                "text": text,
                "command": command,
                "bg": self._colors["bg"],
                "fg": self._colors["fg"],
                "activebackground": self._colors["active_bg"],
                "activeforeground": self._colors["active_fg"],
                "disabledforeground": THEME["text_disabled"],
                "font": ("Segoe UI", 10, "bold"),
                "relief": "flat",
                "cursor": "hand2",
                "padx": 20,
                "pady": 10,
                "borderwidth": 0,
                "highlightthickness": 0,
            }
            default_kwargs.update(kwargs)
            super().__init__(parent, **default_kwargs)
            self.bind("<Enter>", lambda _event: self._on_hover())
            self.bind("<Leave>", lambda _event: self._on_leave())
            self.bind("<Button-1>", lambda _event: self._on_press())
            self.bind("<ButtonRelease-1>", lambda _event: self._on_release())

        def _get_variant_colors(self, variant: str) -> dict[str, str]:
            variants = {
                "primary": {
                    "bg": THEME["primary"],
                    "hover_bg": THEME["primary_hover"],
                    "active_bg": THEME["primary_hover"],
                    "fg": THEME["text_inverse"],
                    "active_fg": THEME["text_inverse"],
                },
                "success": {
                    "bg": THEME["success"],
                    "hover_bg": THEME["success_hover"],
                    "active_bg": THEME["success_hover"],
                    "fg": THEME["text_inverse"],
                    "active_fg": THEME["text_inverse"],
                },
                "danger": {
                    "bg": THEME["danger"],
                    "hover_bg": THEME["danger_hover"],
                    "active_bg": THEME["danger_hover"],
                    "fg": THEME["text_inverse"],
                    "active_fg": THEME["text_inverse"],
                },
                "secondary": {
                    "bg": THEME["bg_tertiary"],
                    "hover_bg": THEME["border"],
                    "active_bg": THEME["border"],
                    "fg": THEME["text_primary"],
                    "active_fg": THEME["text_primary"],
                },
                "outline": {
                    "bg": THEME["bg_secondary"],
                    "hover_bg": THEME["bg_tertiary"],
                    "active_bg": THEME["bg_tertiary"],
                    "fg": THEME["primary"],
                    "active_fg": THEME["primary"],
                },
            }
            return variants.get(variant, variants["primary"])

        def _on_hover(self) -> None:
            if self["state"] != "disabled":
                self.configure(bg=self._colors["hover_bg"])

        def _on_leave(self) -> None:
            if self["state"] != "disabled":
                self.configure(bg=self._colors["bg"], relief="flat")

        def _on_press(self) -> None:
            if self["state"] != "disabled":
                self.configure(relief="sunken")

        def _on_release(self) -> None:
            if self["state"] != "disabled":
                self.configure(relief="flat", bg=self._colors["hover_bg"])

        def set_enabled(self, enabled: bool) -> None:
            state = "normal" if enabled else "disabled"
            self.configure(state=state, cursor="hand2" if enabled else "arrow")
            if enabled:
                self.configure(bg=self._colors["bg"])

    class AnimatedProgressbar(ttk.Progressbar):
        """Progressbar com suporte a modo indeterminado durante processamento."""

        def __init__(self, parent: Any, **kwargs: Any) -> None:
            default_kwargs = {"mode": "determinate", "style": "TProgressbar", "maximum": 100}
            default_kwargs.update(kwargs)
            super().__init__(parent, **default_kwargs)
            self._is_pulsing = False

        def start_pulse(self) -> None:
            if not self._is_pulsing:
                self._is_pulsing = True
                self.configure(mode="indeterminate")
                self.start(12)

        def stop_pulse(self) -> None:
            if self._is_pulsing:
                self.stop()
                self._is_pulsing = False
            self.configure(mode="determinate")

        def set_progress(self, value: float) -> None:
            self.stop_pulse()
            self.configure(value=max(0.0, min(100.0, value)))

    class StyledLogText(tk.Text):
        """Widget de log com cores por tipo de mensagem."""

        def __init__(self, parent: Any, **kwargs: Any) -> None:
            default_kwargs = {
                "wrap": "word",
                "bg": THEME["bg_secondary"],
                "fg": THEME["text_primary"],
                "font": ("Consolas", 9),
                "relief": "flat",
                "borderwidth": 0,
                "highlightthickness": 1,
                "highlightbackground": THEME["border"],
                "highlightcolor": THEME["border_focus"],
                "padx": 12,
                "pady": 12,
            }
            default_kwargs.update(kwargs)
            super().__init__(parent, **default_kwargs)
            self._configure_tags()

        def _configure_tags(self) -> None:
            self.tag_configure("info", foreground=THEME["text_secondary"])
            self.tag_configure("success", foreground=THEME["success"])
            self.tag_configure("warning", foreground=THEME["warning"])
            self.tag_configure("error", foreground=THEME["danger"])
            self.tag_configure("progress", foreground=THEME["primary"])

        def append_log(self, message: str) -> None:
            lower = message.lower()
            if "[erro]" in lower:
                tag = "error"
            elif "[aviso]" in lower or "warning" in lower:
                tag = "warning"
            elif "concluido" in lower or "pronto" in lower:
                tag = "success"
            elif "baixando" in lower or "download" in lower or "processando" in lower:
                tag = "progress"
            else:
                tag = "info"
            self.insert("end", f"[{time.strftime('%H:%M:%S')}] ", "info")
            self.insert("end", f"{message}\n", tag)
            self.see("end")

    class Tooltip:
        """Tooltip simples para widgets."""

        def __init__(self, widget: Any, text: str) -> None:
            self.widget = widget
            self.text = text
            self.tooltip_window: tk.Toplevel | None = None
            widget.bind("<Enter>", self.show_tooltip, add="+")
            widget.bind("<Leave>", self.hide_tooltip, add="+")

        def show_tooltip(self, event: Any = None) -> None:
            del event
            if self.tooltip_window:
                return
            x = self.widget.winfo_rootx() + 24
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 8
            self.tooltip_window = tk.Toplevel(self.widget)
            self.tooltip_window.wm_overrideredirect(True)
            self.tooltip_window.wm_geometry(f"+{x}+{y}")
            label = tk.Label(
                self.tooltip_window,
                text=self.text,
                background=THEME["text_primary"],
                foreground=THEME["text_inverse"],
                font=("Segoe UI", 9),
                padx=8,
                pady=4,
            )
            label.pack()

        def hide_tooltip(self, event: Any = None) -> None:
            del event
            if self.tooltip_window:
                self.tooltip_window.destroy()
                self.tooltip_window = None

    def set_button_enabled(button: Any, enabled: bool) -> None:
        if hasattr(button, "set_enabled"):
            button.set_enabled(enabled)
        else:
            button.configure(state="normal" if enabled else "disabled")

    def create_card(parent: Any, text: str, row: int, column: int = 0, columnspan: int = 3, **grid_kwargs: Any) -> Any:
        card = ttk.LabelFrame(parent, text=text, padding=16)
        card.grid(row=row, column=column, columnspan=columnspan, sticky="ew", **grid_kwargs)
        return card

    events: queue.Queue[tuple[Any, ...]] = queue.Queue()
    loaded_duration: dict[str, int | None] = {"seconds": None}
    download_thread = ThreadManager()

    root = tk.Tk()
    root.title(APP_NAME)
    StyleConfig(root)
    root.minsize(900, 700)
    screen_width = root.winfo_screenwidth()
    screen_height = root.winfo_screenheight()
    window_width = min(int(screen_width * 0.72), 1200)
    window_height = min(int(screen_height * 0.78), 860)
    window_x = max((screen_width - window_width) // 2, 0)
    window_y = max((screen_height - window_height) // 2, 0)
    root.geometry(f"{window_width}x{window_height}+{window_x}+{window_y}")
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)

    main = ttk.Frame(root, padding=24)
    main.grid(row=0, column=0, sticky="nsew")
    main.columnconfigure(1, weight=1)
    main.rowconfigure(7, weight=1)

    url_var = tk.StringVar()
    output_var = tk.StringVar(value=str(DEFAULT_OUTPUT_DIR))
    type_var = tk.StringVar(value="video")
    video_quality_var = tk.StringVar(value="best")
    audio_format_var = tk.StringVar(value="mp3")
    playlist_var = tk.BooleanVar(value=False)
    install_ffmpeg_var = tk.BooleanVar(value=True)
    use_section_var = tk.BooleanVar(value=False)
    end_to_finish_var = tk.BooleanVar(value=False)
    precise_cut_var = tk.BooleanVar(value=False)
    fast_audio_cut_var = tk.BooleanVar(value=False)
    status_var = tk.StringVar(value="Pronto.")
    info_var = tk.StringVar(value="Dados do video ainda nao carregados.")
    section_preview_var = tk.StringVar(value="Trecho desativado.")
    progress_var = tk.DoubleVar(value=0)

    def put_event(kind: str, *payload: Any) -> None:
        events.put((kind, *payload))

    def log_message(message: str) -> None:
        put_event("log", message.rstrip())

    last_progress_sent = {"at": 0.0}
    last_process_activity = {"at": time.monotonic()}

    def progress_message(status: dict[str, Any]) -> None:
        if status.get("status") == "downloading":
            now = time.monotonic()
            if now - last_progress_sent["at"] < 0.25:
                return
            last_progress_sent["at"] = now

            total = status.get("total_bytes") or status.get("total_bytes_estimate")
            downloaded = status.get("downloaded_bytes") or 0
            percent = (downloaded / total * 100) if total else None
            filename = Path(status.get("filename", "")).name
            text_parts = [
                status.get("_percent_str", "").strip(),
                status.get("_speed_str", "").strip(),
                f"ETA {status.get('_eta_str', '').strip()}" if status.get("_eta_str") else "",
            ]
            details = " | ".join(part for part in text_parts if part)
            put_event("progress", percent, f"Baixando {filename}: {details}")
        elif status.get("status") == "finished":
            put_event("progress", None, "Processando arquivo...")

    def validate_digits(proposed: str, max_digits: str) -> bool:
        return proposed == "" or (proposed.isdigit() and len(proposed) <= int(max_digits))

    validate_2_digits = (root.register(validate_digits), "%P", "2")
    validate_3_digits = (root.register(validate_digits), "%P", "3")
    Spinbox = getattr(ttk, "Spinbox", tk.Spinbox)

    header_frame = ttk.Frame(main)
    header_frame.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 18))
    ttk.Label(header_frame, text=APP_NAME, style="Title.TLabel").pack(anchor="w")
    ttk.Label(
        header_frame,
        text="Baixe videos e audios do YouTube com controle de qualidade, formato e trecho.",
        style="Secondary.TLabel",
    ).pack(anchor="w", pady=(4, 0))
    ttk.Separator(header_frame, orient="horizontal").pack(fill="x", pady=(14, 0))

    input_frame = create_card(main, "Origem e destino", row=1, pady=(0, 12))
    input_frame.columnconfigure(1, weight=1)

    ttk.Label(input_frame, text="URL do YouTube", style="CardSecondary.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 8))
    url_entry = ttk.Entry(input_frame, textvariable=url_var)
    url_entry.grid(row=0, column=1, sticky="ew", pady=(0, 8), padx=(10, 10))
    load_button = StyledButton(input_frame, text="Carregar", variant="secondary")
    load_button.grid(row=0, column=2, sticky="ew", pady=(0, 8))

    ttk.Label(input_frame, text="Salvar em", style="CardSecondary.TLabel").grid(row=1, column=0, sticky="w")
    output_entry = ttk.Entry(input_frame, textvariable=output_var)
    output_entry.grid(row=1, column=1, sticky="ew", padx=(10, 10))
    browse_button = StyledButton(input_frame, text="Escolher", variant="secondary")
    browse_button.grid(row=1, column=2, sticky="ew")

    options_frame = create_card(main, "Download", row=2, pady=(0, 12))
    for column in range(6):
        options_frame.columnconfigure(column, weight=1)

    ttk.Radiobutton(options_frame, text="Video", variable=type_var, value="video").grid(row=0, column=0, sticky="w")
    ttk.Radiobutton(options_frame, text="Audio", variable=type_var, value="audio").grid(row=0, column=1, sticky="w")
    ttk.Checkbutton(options_frame, text="Playlist inteira", variable=playlist_var).grid(row=0, column=2, sticky="w")
    ttk.Checkbutton(options_frame, text="Instalar ffmpeg se precisar", variable=install_ffmpeg_var).grid(
        row=0, column=3, columnspan=3, sticky="w"
    )

    ttk.Label(options_frame, text="Qualidade").grid(row=1, column=0, sticky="w", pady=(10, 0))
    video_quality_combo = ttk.Combobox(
        options_frame,
        textvariable=video_quality_var,
        values=("best", "1080", "720", "480", "360"),
        width=10,
    )
    video_quality_combo.grid(row=1, column=1, sticky="w", pady=(10, 0))

    ttk.Label(options_frame, text="Formato audio").grid(row=1, column=2, sticky="w", pady=(10, 0))
    audio_format_combo = ttk.Combobox(
        options_frame,
        textvariable=audio_format_var,
        values=("mp3", "m4a", "opus", "wav", "flac"),
        width=10,
        state="readonly",
    )
    audio_format_combo.grid(row=1, column=3, sticky="w", pady=(10, 0))

    info_card = create_card(main, "Informacoes do video", row=3, pady=(0, 12))
    ttk.Label(info_card, textvariable=info_var, style="Card.TLabel").grid(row=0, column=0, sticky="ew")
    info_card.columnconfigure(0, weight=1)

    section_frame = create_card(main, "Trecho do video", row=4, pady=(0, 14))
    for column in range(10):
        section_frame.columnconfigure(column, weight=0)
    section_frame.columnconfigure(9, weight=1)

    start_hour_var = tk.StringVar(value="0")
    start_minute_var = tk.StringVar(value="0")
    start_second_var = tk.StringVar(value="0")
    end_hour_var = tk.StringVar(value="0")
    end_minute_var = tk.StringVar(value="0")
    end_second_var = tk.StringVar(value="0")

    start_vars = (start_hour_var, start_minute_var, start_second_var)
    end_vars = (end_hour_var, end_minute_var, end_second_var)

    section_widgets: list[Any] = []
    end_widgets: list[Any] = []

    ttk.Checkbutton(section_frame, text="Baixar apenas um trecho", variable=use_section_var).grid(
        row=0, column=0, columnspan=4, sticky="w", pady=(0, 8)
    )
    end_to_finish_check = ttk.Checkbutton(section_frame, text="Ate o fim", variable=end_to_finish_var)
    end_to_finish_check.grid(row=0, column=4, columnspan=2, sticky="w", pady=(0, 8))
    precise_cut_check = ttk.Checkbutton(section_frame, text="Corte preciso", variable=precise_cut_var)
    precise_cut_check.grid(row=0, column=6, columnspan=2, sticky="w", pady=(0, 8))
    fast_audio_cut_check = ttk.Checkbutton(
        section_frame,
        text="Audio rapido sem converter",
        variable=fast_audio_cut_var,
    )
    fast_audio_cut_check.grid(row=0, column=8, columnspan=2, sticky="w", pady=(0, 8))
    section_widgets.extend([end_to_finish_check, precise_cut_check, fast_audio_cut_check])

    def add_time_controls(row: int, label: str, variables: tuple[tk.StringVar, tk.StringVar, tk.StringVar]) -> list[Any]:
        widgets: list[Any] = []
        label_widget = ttk.Label(section_frame, text=label)
        label_widget.grid(row=row, column=0, sticky="w", pady=4)
        widgets.append(label_widget)
        for index, (var, suffix, maximum, validate_cmd) in enumerate(
            (
                (variables[0], "h", 99, validate_3_digits),
                (variables[1], "min", 59, validate_2_digits),
                (variables[2], "s", 59, validate_2_digits),
            )
        ):
            spinbox = Spinbox(
                section_frame,
                from_=0,
                to=maximum,
                increment=1,
                width=5,
                textvariable=var,
                validate="key",
                validatecommand=validate_cmd,
            )
            spinbox.grid(row=row, column=1 + index * 2, sticky="w", pady=4)
            suffix_label = ttk.Label(section_frame, text=suffix)
            suffix_label.grid(row=row, column=2 + index * 2, sticky="w", padx=(2, 12), pady=4)
            widgets.extend([spinbox, suffix_label])
        return widgets

    section_widgets.extend(add_time_controls(1, "Inicio", start_vars))
    end_widgets.extend(add_time_controls(2, "Fim", end_vars))
    section_widgets.extend(end_widgets)
    preview_label = ttk.Label(section_frame, textvariable=section_preview_var)
    preview_label.grid(row=3, column=0, columnspan=10, sticky="ew", pady=(8, 0))
    section_widgets.append(preview_label)

    action_frame = ttk.Frame(main)
    action_frame.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(8, 14))
    download_button = StyledButton(action_frame, text="Baixar", variant="success", font=("Segoe UI", 11, "bold"), padx=32, pady=12)
    download_button.pack(side="left", padx=(0, 12))
    open_folder_button = StyledButton(action_frame, text="Abrir pasta", variant="outline")
    open_folder_button.pack(side="left")

    log_frame = create_card(main, "Status", row=7)
    log_frame.grid_configure(sticky="nsew")
    log_frame.columnconfigure(0, weight=1)
    log_frame.rowconfigure(1, weight=1)
    progress_bar = AnimatedProgressbar(log_frame, variable=progress_var)
    progress_bar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
    ttk.Label(log_frame, textvariable=status_var, style="CardSecondary.TLabel").grid(row=2, column=0, sticky="ew", pady=(8, 0))
    log_text = StyledLogText(log_frame, height=10)
    log_text.grid(row=1, column=0, sticky="nsew")
    scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=log_text.yview)
    scrollbar.grid(row=1, column=1, sticky="ns")
    log_text.configure(yscrollcommand=scrollbar.set)

    def set_time_vars(seconds: int, variables: tuple[tk.StringVar, tk.StringVar, tk.StringVar]) -> None:
        hours, remainder = divmod(max(seconds, 0), 3600)
        minutes, secs = divmod(remainder, 60)
        variables[0].set(str(hours))
        variables[1].set(str(minutes))
        variables[2].set(str(secs))

    def read_time_vars(variables: tuple[tk.StringVar, tk.StringVar, tk.StringVar], label: str) -> int:
        values: list[int] = []
        for var, part_label, maximum in zip(variables, ("horas", "minutos", "segundos"), (99, 59, 59), strict=True):
            value = var.get().strip()
            if value == "":
                value = "0"
            if not value.isdigit():
                raise ValueError(f"{label}: {part_label} deve ser um numero.")
            number = int(value)
            if number > maximum:
                raise ValueError(f"{label}: {part_label} deve ser no maximo {maximum}.")
            values.append(number)
        return values[0] * 3600 + values[1] * 60 + values[2]

    def update_section_preview(*_: Any) -> None:
        if not use_section_var.get():
            section_preview_var.set("Trecho desativado.")
            return
        try:
            start_seconds = read_time_vars(start_vars, "Inicio")
            if end_to_finish_var.get():
                if loaded_duration["seconds"]:
                    duration = max(loaded_duration["seconds"] - start_seconds, 0)
                    section_preview_var.set(f"Selecionado: {format_time(start_seconds)} ate o fim ({format_time(duration)}).")
                else:
                    section_preview_var.set(f"Selecionado: {format_time(start_seconds)} ate o fim.")
                return
            end_seconds = read_time_vars(end_vars, "Fim")
            if end_seconds <= start_seconds:
                section_preview_var.set("O fim precisa ser maior que o inicio.")
                return
            section_preview_var.set(
                f"Selecionado: {format_time(start_seconds)} ate {format_time(end_seconds)} "
                f"({format_time(end_seconds - start_seconds)})."
            )
        except ValueError as exc:
            section_preview_var.set(str(exc))

    def configure_section_state(*_: Any) -> None:
        section_enabled = use_section_var.get()
        state = "normal" if section_enabled else "disabled"
        for widget in section_widgets:
            try:
                widget.configure(state=state)
            except tk.TclError:
                pass
        end_state = "disabled" if not section_enabled or end_to_finish_var.get() else "normal"
        for widget in end_widgets:
            try:
                widget.configure(state=end_state)
            except tk.TclError:
                pass
        if type_var.get() == "audio":
            try:
                fast_audio_cut_check.configure(state=state)
            except tk.TclError:
                pass
        else:
            fast_audio_cut_var.set(False)
        update_section_preview()

    def configure_media_state(*_: Any) -> None:
        if type_var.get() == "video":
            video_quality_combo.configure(state="normal")
            audio_format_combo.configure(state="disabled")
            fast_audio_cut_check.configure(state="disabled")
            fast_audio_cut_var.set(False)
        else:
            video_quality_combo.configure(state="disabled")
            audio_format_combo.configure(state="readonly")
            fast_audio_cut_check.configure(state="normal" if use_section_var.get() else "disabled")

    def choose_output_folder() -> None:
        folder = filedialog.askdirectory(initialdir=output_var.get() or str(Path.cwd()))
        if folder:
            output_var.set(folder)

    def open_output_folder() -> None:
        folder = Path(output_var.get()).expanduser()
        folder.mkdir(parents=True, exist_ok=True)
        PlatformUtils.open_folder(folder, message_func=log_message)

    def build_download_command(
        *,
        url: str,
        media_type: str,
        output_dir: Path,
        audio_format: str,
        video_quality: str,
        playlist: bool,
        install_ffmpeg: bool,
        start_arg: str | None,
        end_arg: str | None,
        precise_cut: bool,
        fast_audio_cut: bool,
    ) -> list[str]:
        if getattr(sys, "frozen", False):
            command = [sys.executable]
        else:
            command = [
                sys.executable,
                "-u",
                str(Path(__file__).resolve()),
            ]

        command.extend([
            url,
            "--tipo",
            media_type,
            "--output",
            str(output_dir),
            "--audio-format",
            audio_format,
            "--video-quality",
            video_quality,
        ])
        if playlist:
            command.append("--playlist")
        if install_ffmpeg:
            command.append("--install-ffmpeg")
        if start_arg:
            command.extend(["--start", start_arg])
        if end_arg:
            command.extend(["--end", end_arg])
        if precise_cut:
            command.append("--precise-cut")
        if fast_audio_cut:
            command.append("--fast-audio-cut")
        return command

    def send_process_output(text: str, *, is_error: bool = False) -> None:
        send_process_output_with_duration(text, is_error=is_error, expected_duration=None)

    def send_process_output_with_duration(
        text: str,
        *,
        is_error: bool = False,
        expected_duration: float | None = None,
    ) -> None:
        clean = text.strip()
        if not clean:
            return

        now = time.monotonic()
        last_process_activity["at"] = now
        is_progress = (
            clean.startswith("[download]")
            or clean.startswith("Baixando ")
            or clean.startswith("[processamento]")
            or clean.startswith("[audio]")
            or clean.startswith("[trecho]")
            or clean.startswith("frame=")
            or clean.startswith("size=")
            or "time=" in clean
            or "ETA" in clean
        )
        if is_progress:
            if now - last_progress_sent["at"] >= 0.35:
                last_progress_sent["at"] = now
                percent = None
                ffmpeg_seconds = parse_ffmpeg_progress_time(clean)
                if expected_duration and ffmpeg_seconds is not None:
                    percent = min(99.0, max(0.0, ffmpeg_seconds / expected_duration * 100))
                put_event("progress", percent, clean)
            return

        noisy_ffmpeg_prefixes = (
            "Input #",
            "Output #",
            "Stream #",
            "Metadata:",
            "Duration:",
            "Press [q]",
        )
        if clean.startswith(noisy_ffmpeg_prefixes):
            return

        log_message(f"[erro] {clean}" if is_error else clean)

    def read_process_stream(stream: Any, *, is_error: bool = False) -> None:
        ProcessRunner.stream_output(stream, lambda text, error: send_process_output(text, is_error=error), is_error=is_error)

    def run_download_process(command: list[str], *, expected_duration: float | None = None) -> int:
        creationflags = PlatformUtils.get_subprocess_creation_flags(hide_window=True)
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        last_process_activity["at"] = time.monotonic()
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            creationflags=creationflags,
        )

        stdout_thread = threading.Thread(
            target=ProcessRunner.stream_output,
            args=(
                process.stdout,
                lambda text, error: send_process_output_with_duration(
                    text,
                    is_error=error,
                    expected_duration=expected_duration,
                ),
            ),
            kwargs={"is_error": False},
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=ProcessRunner.stream_output,
            args=(
                process.stderr,
                lambda text, error: send_process_output_with_duration(
                    text,
                    is_error=error,
                    expected_duration=expected_duration,
                ),
            ),
            kwargs={"is_error": True},
            daemon=True,
        )

        def emit_heartbeat() -> None:
            while process.poll() is None:
                time.sleep(5)
                idle_seconds = time.monotonic() - last_process_activity["at"]
                if idle_seconds >= 5:
                    put_event(
                        "progress",
                        None,
                        "Processando audio/trecho com ffmpeg... o download continua em execucao.",
                    )

        stdout_thread.start()
        stderr_thread.start()
        heartbeat_thread = threading.Thread(target=emit_heartbeat, daemon=True)
        heartbeat_thread.start()
        returncode = process.wait()
        stdout_thread.join(timeout=2)
        stderr_thread.join(timeout=2)
        heartbeat_thread.join(timeout=1)
        return returncode

    def load_video_info() -> None:
        url = url_var.get().strip()
        if not url:
            messagebox.showerror(APP_NAME, "Cole a URL do YouTube antes de carregar os dados.")
            return
        if not validate_youtube_url(url):
            messagebox.showerror(APP_NAME, "URL invalida. Cole uma URL do YouTube valida.")
            return

        set_button_enabled(load_button, False)
        info_var.set("Carregando dados do video...")
        log_message("[info] Carregando dados do video...")
        playlist_selected = playlist_var.get()

        def worker() -> None:
            try:
                info = YtDlpManager(message_func=log_message).extract_info(url, playlist_selected)
                title = info.get("title") or "Titulo nao encontrado"
                duration = int(info.get("duration") or 0)
                put_event("info_loaded", title, duration)
            except BaseException as exc:
                put_event("info_error", str(exc))

        threading.Thread(target=worker, daemon=True).start()

    def build_section_args() -> tuple[str | None, str | None]:
        if not use_section_var.get():
            return None, None

        start_seconds = read_time_vars(start_vars, "Inicio")
        if end_to_finish_var.get():
            return format_time(start_seconds), "fim"

        end_seconds = read_time_vars(end_vars, "Fim")
        if end_seconds <= start_seconds:
            raise ValueError("O fim do trecho precisa ser maior que o inicio.")
        return format_time(start_seconds), format_time(end_seconds)

    def start_download() -> None:
        if download_thread.is_running():
            return

        url = url_var.get().strip()
        if not url:
            messagebox.showerror(APP_NAME, "Cole a URL do YouTube antes de baixar.")
            return
        if not validate_youtube_url(url):
            messagebox.showerror(APP_NAME, "URL invalida. Cole uma URL do YouTube valida.")
            return

        try:
            start_arg, end_arg = build_section_args()
        except ValueError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return

        progress_var.set(0)
        status_var.set("Iniciando download...")
        set_button_enabled(download_button, False)
        set_button_enabled(load_button, False)
        log_message("[download] Iniciando...")
        media_type = type_var.get()
        output_dir = Path(output_var.get()).expanduser()
        audio_format = audio_format_var.get()
        video_quality = video_quality_var.get().lower()
        playlist_selected = playlist_var.get()
        install_ffmpeg_selected = install_ffmpeg_var.get()
        precise_cut_selected = precise_cut_var.get()
        fast_audio_cut_selected = type_var.get() == "audio" and use_section_var.get() and fast_audio_cut_var.get()
        expected_duration = None
        if start_arg and end_arg and end_arg != "fim":
            expected_duration = time_range_duration(build_time_range(start_arg, end_arg))
        command = build_download_command(
            url=url,
            media_type=media_type,
            output_dir=output_dir,
            audio_format=audio_format,
            video_quality=video_quality,
            playlist=playlist_selected,
            install_ffmpeg=install_ffmpeg_selected,
            start_arg=start_arg,
            end_arg=end_arg,
            precise_cut=precise_cut_selected,
            fast_audio_cut=fast_audio_cut_selected,
        )

        def worker() -> None:
            try:
                returncode = run_download_process(command, expected_duration=expected_duration)
                if returncode == 0:
                    put_event("done")
                else:
                    put_event("download_error", f"O processo de download terminou com codigo {returncode}.")
            except BaseException as exc:
                put_event("download_error", str(exc))

        download_thread.start(worker)

    def pump_events() -> None:
        while True:
            try:
                event = events.get_nowait()
            except queue.Empty:
                break

            kind = event[0]
            if kind == "log":
                log_text.append_log(str(event[1]))
            elif kind == "progress":
                percent, text = event[1], event[2]
                if percent is not None:
                    progress_bar.set_progress(float(percent))
                else:
                    progress_bar.start_pulse()
                status_var.set(text)
            elif kind == "info_loaded":
                title, duration = event[1], event[2]
                loaded_duration["seconds"] = duration or None
                if duration:
                    info_var.set(f"{title} | Duracao: {format_time(duration)}")
                    if not end_to_finish_var.get():
                        set_time_vars(duration, end_vars)
                else:
                    info_var.set(f"{title} | Duracao indisponivel")
                set_button_enabled(load_button, True)
                update_section_preview()
            elif kind == "info_error":
                set_button_enabled(load_button, True)
                info_var.set("Nao foi possivel carregar os dados.")
                log_text.append_log(f"[erro] {event[1]}")
                messagebox.showerror(APP_NAME, event[1])
            elif kind == "done":
                progress_bar.set_progress(100)
                status_var.set("Download concluido.")
                set_button_enabled(download_button, True)
                set_button_enabled(load_button, True)
                messagebox.showinfo(APP_NAME, "Download concluido.")
            elif kind == "download_error":
                progress_bar.stop_pulse()
                status_var.set("Falha no download.")
                set_button_enabled(download_button, True)
                set_button_enabled(load_button, True)
                log_text.append_log(f"[erro] {event[1]}")
                messagebox.showerror(APP_NAME, event[1])

        root.after(100, pump_events)

    browse_button.configure(command=choose_output_folder)
    open_folder_button.configure(command=open_output_folder)
    load_button.configure(command=load_video_info)
    download_button.configure(command=start_download)
    Tooltip(url_entry, "Cole a URL completa do video, short, canal ou playlist do YouTube.")
    Tooltip(load_button, "Carrega titulo e duracao sem iniciar o download.")
    Tooltip(output_entry, "Pasta onde os arquivos baixados serao salvos.")
    Tooltip(browse_button, "Escolhe a pasta de destino.")
    Tooltip(download_button, "Inicia o download com as opcoes selecionadas.")
    Tooltip(open_folder_button, "Abre a pasta de destino no gerenciador de arquivos.")
    Tooltip(fast_audio_cut_check, "Para audio recortado, pula a conversao final e mantem o formato original.")
    root.bind("<Control-d>", lambda _event: start_download())
    root.bind("<Control-o>", lambda _event: open_output_folder())
    root.bind("<Control-l>", lambda _event: load_video_info())
    type_var.trace_add("write", configure_media_state)
    use_section_var.trace_add("write", configure_section_state)
    end_to_finish_var.trace_add("write", configure_section_state)
    for time_var in (*start_vars, *end_vars):
        time_var.trace_add("write", update_section_preview)

    configure_media_state()
    configure_section_state()
    pump_events()
    url_entry.focus_set()
    root.mainloop()


def ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or (default or "")


def interactive_args() -> argparse.Namespace:
    print(f"=== {APP_NAME} ===")
    url = ask("Cole a URL do video ou playlist")
    media_type = ask("Tipo: video ou audio", "video").lower()
    while media_type not in {"video", "audio"}:
        media_type = ask("Digite apenas video ou audio", "video").lower()

    audio_format = "mp3"
    video_quality = "best"
    if media_type == "audio":
        audio_format = ask("Formato do audio: mp3, m4a, opus, wav", "mp3").lower()
    else:
        video_quality = ask("Qualidade: best, 1080, 720, 480, 360", "best").lower()

    output_dir = Path(ask("Pasta de saida", str(DEFAULT_OUTPUT_DIR))).expanduser()
    playlist_answer = ask("Baixar playlist inteira se a URL for playlist? s/n", "n").lower()
    install_ffmpeg_answer = ask("Instalar ffmpeg automaticamente se precisar? s/n", "s").lower()
    section_answer = ask("Baixar apenas um trecho? s/n", "n").lower()
    start_time = None
    end_time = None
    precise_cut = False
    fast_audio_cut = False
    if section_answer.startswith("s"):
        start_time = ask("Inicio do trecho: segundos, MM:SS ou HH:MM:SS", "0")
        end_time = ask("Fim do trecho: segundos, MM:SS, HH:MM:SS ou fim", "fim")
        precise_cut_answer = ask("Corte preciso? s/n", "n").lower()
        precise_cut = precise_cut_answer.startswith("s")
        if media_type == "audio":
            fast_audio_cut_answer = ask("Modo rapido sem converter audio? s/n", "s").lower()
            fast_audio_cut = fast_audio_cut_answer.startswith("s")

    return argparse.Namespace(
        url=url,
        tipo=media_type,
        output=output_dir,
        audio_format=audio_format,
        video_quality=video_quality,
        playlist=playlist_answer.startswith("s"),
        install_ffmpeg=install_ffmpeg_answer.startswith("s"),
        start=start_time,
        end=end_time,
        precise_cut=precise_cut,
        fast_audio_cut=fast_audio_cut,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Baixa videos e audios do YouTube usando yt-dlp.")
    parser.add_argument("url", nargs="?", help="URL do video, short, live, canal ou playlist.")
    parser.add_argument("--cli", action="store_true", help="Usa o modo terminal interativo em vez da janela.")
    parser.add_argument("--tipo", choices=["video", "audio"], default="video", help="Tipo de download.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_DIR, help="Pasta de saida.")
    parser.add_argument(
        "--audio-format",
        choices=["mp3", "m4a", "opus", "wav", "flac"],
        default="mp3",
        help="Formato de audio ao usar --tipo audio.",
    )
    parser.add_argument(
        "--video-quality",
        default="best",
        help="Qualidade maxima do video: best, 1080, 720, 480, 360 etc.",
    )
    parser.add_argument("--playlist", action="store_true", help="Baixa playlist inteira.")
    parser.add_argument(
        "--install-ffmpeg",
        action="store_true",
        help="Instala ffmpeg via winget no Windows se ele estiver ausente.",
    )
    parser.add_argument(
        "--start",
        help="Inicio do trecho: segundos, MM:SS ou HH:MM:SS. Ex.: 90, 01:30, 00:10:15.",
    )
    parser.add_argument(
        "--end",
        help="Fim do trecho: segundos, MM:SS, HH:MM:SS ou 'fim'. Ex.: 12:30.",
    )
    parser.add_argument(
        "--precise-cut",
        action="store_true",
        help="Recodifica quando necessario para cortar exatamente nos tempos. Mais lento.",
    )
    parser.add_argument(
        "--fast-audio-cut",
        action="store_true",
        help="Para audio com --start/--end, baixa o trecho no formato original e pula conversao.",
    )
    parser.add_argument("--update-ytdlp", action="store_true", help="Atualiza yt-dlp e sai.")
    args = parser.parse_args()

    if args.update_ytdlp:
        run_command([sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"])
        raise SystemExit(0)

    if args.install_ffmpeg and not args.url:
        ensure_ffmpeg(install=True, required=True)
        raise SystemExit(0)

    if not args.url and not args.cli:
        args.gui = True
        return args

    if not args.url:
        return interactive_args()

    return args


def main() -> None:
    setup_signal_handlers(lambda: None)
    args = parse_args()
    if getattr(args, "gui", False):
        launch_gui()
        return

    if not args.url:
        raise SystemExit("URL nao informada.")

    download_media(
        args.url,
        media_type=args.tipo,
        output_dir=args.output.expanduser(),
        audio_format=args.audio_format,
        video_quality=str(args.video_quality).lower(),
        playlist=args.playlist,
        install_ffmpeg=args.install_ffmpeg,
        start_time=args.start,
        end_time=args.end,
        precise_cut=args.precise_cut,
        fast_audio_cut=getattr(args, "fast_audio_cut", False),
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelado pelo usuario.")
        raise SystemExit(130)
