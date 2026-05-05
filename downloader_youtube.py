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
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable


APP_NAME = "Downloader YT"
DEFAULT_OUTPUT_DIR = Path.cwd() / "downloads"
MessageFunc = Callable[[str], None]
ProgressFunc = Callable[[dict[str, Any]], None]


def run_command(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=check, text=True)


def ensure_package(package_name: str, import_name: str | None = None, message_func: MessageFunc = print) -> None:
    module_name = import_name or package_name
    if importlib.util.find_spec(module_name) is not None:
        return

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
    if not start and not end:
        return None

    start_seconds = parse_timecode(start or "0", label="--start")
    end_seconds = parse_timecode(end or "inf", label="--end")
    if end_seconds <= start_seconds:
        raise ValueError("--end deve ser maior que --start.")
    return start_seconds, end_seconds


def format_time(seconds: float) -> str:
    if seconds == float("inf"):
        return "fim"

    rounded = int(seconds)
    hours, remainder = divmod(rounded, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def build_output_template(output_dir: Path) -> str:
    output_dir.mkdir(parents=True, exist_ok=True)
    return str(output_dir / "%(title).180B [%(id)s].%(ext)s")


def build_video_format(video_quality: str, *, ffmpeg_available: bool) -> str:
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


class YtdlpLogger:
    def __init__(self, message_func: MessageFunc):
        self.message_func = message_func

    def debug(self, message: str) -> None:
        return None

    def warning(self, message: str) -> None:
        self.message_func(f"[aviso] {message}")

    def error(self, message: str) -> None:
        self.message_func(f"[erro] {message}")


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
    message_func: MessageFunc = print,
    progress_func: ProgressFunc | None = None,
) -> None:
    yt_dlp = import_yt_dlp(message_func=message_func)
    from yt_dlp.utils import download_range_func

    deno_available = use_deno_if_available()
    time_range = build_time_range(start_time, end_time)

    ffmpeg_required = bool(time_range) or media_type == "audio" or video_quality == "best"
    ffmpeg_available = ensure_ffmpeg(install=install_ffmpeg, required=ffmpeg_required, message_func=message_func)
    if time_range and not ffmpeg_available:
        raise SystemExit("[trecho] Baixar apenas uma faixa precisa de ffmpeg.")

    options: dict[str, Any] = {
        "outtmpl": build_output_template(output_dir),
        "noplaylist": not playlist,
        "progress_hooks": [make_progress_hook(message_func, progress_func)],
        "windowsfilenames": True,
        "ignoreerrors": playlist,
        "retries": 10,
        "fragment_retries": 10,
    }
    if progress_func:
        options["quiet"] = True
        options["logger"] = YtdlpLogger(message_func)
    if deno_available:
        options["remote_components"] = ["ejs:github"]
    if time_range:
        options["download_ranges"] = download_range_func([], [time_range])
        options["force_keyframes_at_cuts"] = precise_cut
        start_display, end_display = (format_time(value) for value in time_range)
        message_func(f"[trecho] Baixando apenas de {start_display} ate {end_display}.")
        if precise_cut:
            message_func("[trecho] Corte preciso ativado; o processamento pode demorar mais.")

    if media_type == "audio":
        options["format"] = "bestaudio/best"
        if ffmpeg_available:
            options["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": audio_format,
                    "preferredquality": "192",
                }
            ]
        else:
            message_func("[audio] Sem ffmpeg: baixando o melhor audio original, sem converter formato.")
    else:
        options["format"] = build_video_format(video_quality, ffmpeg_available=ffmpeg_available)
        if ffmpeg_available:
            options["merge_output_format"] = "mp4"
            options["postprocessor_args"] = {
                "merger+ffmpeg_o": ["-c:a", "aac", "-b:a", "192k"],
            }
            message_func("[video] O audio do MP4 final sera mantido/convertido para AAC, mais compativel.")
        elif video_quality == "best":
            message_func("[video] Sem ffmpeg: baixando um arquivo unico quando disponivel.")
        else:
            message_func("[video] Sem ffmpeg: baixando um arquivo unico quando disponivel.")

    message_func(f"[{APP_NAME}] Salvando em: {output_dir}")
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([url])

    message_func(f"[{APP_NAME}] Pronto.")


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
        )
        return

    events: queue.Queue[tuple[Any, ...]] = queue.Queue()
    loaded_duration: dict[str, int | None] = {"seconds": None}
    active_thread: dict[str, threading.Thread | None] = {"thread": None}

    root = tk.Tk()
    root.title(APP_NAME)
    root.geometry("860x720")
    root.minsize(780, 640)
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)

    main = ttk.Frame(root, padding=16)
    main.grid(row=0, column=0, sticky="nsew")
    main.columnconfigure(1, weight=1)
    main.rowconfigure(6, weight=1)

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
    status_var = tk.StringVar(value="Pronto.")
    info_var = tk.StringVar(value="Dados do video ainda nao carregados.")
    section_preview_var = tk.StringVar(value="Trecho desativado.")
    progress_var = tk.DoubleVar(value=0)

    def put_event(kind: str, *payload: Any) -> None:
        events.put((kind, *payload))

    def log_message(message: str) -> None:
        put_event("log", message.rstrip())

    def progress_message(status: dict[str, Any]) -> None:
        if status.get("status") == "downloading":
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

    ttk.Label(main, text="URL").grid(row=0, column=0, sticky="w", pady=(0, 8))
    url_entry = ttk.Entry(main, textvariable=url_var)
    url_entry.grid(row=0, column=1, sticky="ew", pady=(0, 8), padx=(8, 8))
    load_button = ttk.Button(main, text="Carregar dados")
    load_button.grid(row=0, column=2, sticky="ew", pady=(0, 8))

    ttk.Label(main, text="Salvar em").grid(row=1, column=0, sticky="w", pady=(0, 8))
    output_entry = ttk.Entry(main, textvariable=output_var)
    output_entry.grid(row=1, column=1, sticky="ew", pady=(0, 8), padx=(8, 8))
    browse_button = ttk.Button(main, text="Escolher pasta")
    browse_button.grid(row=1, column=2, sticky="ew", pady=(0, 8))

    options_frame = ttk.LabelFrame(main, text="Download", padding=12)
    options_frame.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(0, 10))
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

    ttk.Label(main, textvariable=info_var).grid(row=3, column=0, columnspan=3, sticky="ew", pady=(0, 8))

    section_frame = ttk.LabelFrame(main, text="Trecho do video", padding=12)
    section_frame.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(0, 10))
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
    section_widgets.extend([end_to_finish_check, precise_cut_check])

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
    action_frame.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(0, 10))
    action_frame.columnconfigure(0, weight=1)
    download_button = ttk.Button(action_frame, text="Baixar")
    download_button.grid(row=0, column=0, sticky="ew", padx=(0, 8))
    open_folder_button = ttk.Button(action_frame, text="Abrir pasta")
    open_folder_button.grid(row=0, column=1, sticky="e")

    log_frame = ttk.LabelFrame(main, text="Status", padding=10)
    log_frame.grid(row=6, column=0, columnspan=3, sticky="nsew")
    log_frame.columnconfigure(0, weight=1)
    log_frame.rowconfigure(1, weight=1)
    ttk.Progressbar(log_frame, variable=progress_var, maximum=100).grid(row=0, column=0, sticky="ew", pady=(0, 6))
    ttk.Label(log_frame, textvariable=status_var).grid(row=2, column=0, sticky="ew", pady=(6, 0))
    log_text = tk.Text(log_frame, height=10, wrap="word")
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
        update_section_preview()

    def configure_media_state(*_: Any) -> None:
        if type_var.get() == "video":
            video_quality_combo.configure(state="normal")
            audio_format_combo.configure(state="disabled")
        else:
            video_quality_combo.configure(state="disabled")
            audio_format_combo.configure(state="readonly")

    def choose_output_folder() -> None:
        folder = filedialog.askdirectory(initialdir=output_var.get() or str(Path.cwd()))
        if folder:
            output_var.set(folder)

    def open_output_folder() -> None:
        folder = Path(output_var.get()).expanduser()
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(folder)

    def load_video_info() -> None:
        url = url_var.get().strip()
        if not url:
            messagebox.showerror(APP_NAME, "Cole a URL do YouTube antes de carregar os dados.")
            return

        load_button.configure(state="disabled")
        info_var.set("Carregando dados do video...")
        log_message("[info] Carregando dados do video...")

        def worker() -> None:
            try:
                yt_dlp = import_yt_dlp(message_func=log_message)
                deno_available = use_deno_if_available()
                options: dict[str, Any] = {
                    "quiet": True,
                    "no_warnings": True,
                    "noplaylist": not playlist_var.get(),
                    "logger": YtdlpLogger(log_message),
                }
                if deno_available:
                    options["remote_components"] = ["ejs:github"]
                with yt_dlp.YoutubeDL(options) as ydl:
                    info = ydl.extract_info(url, download=False)
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
        current_thread = active_thread.get("thread")
        if current_thread and current_thread.is_alive():
            return

        url = url_var.get().strip()
        if not url:
            messagebox.showerror(APP_NAME, "Cole a URL do YouTube antes de baixar.")
            return

        try:
            start_arg, end_arg = build_section_args()
        except ValueError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return

        progress_var.set(0)
        status_var.set("Iniciando download...")
        download_button.configure(state="disabled")
        load_button.configure(state="disabled")
        log_message("[download] Iniciando...")

        def worker() -> None:
            try:
                download_media(
                    url,
                    media_type=type_var.get(),
                    output_dir=Path(output_var.get()).expanduser(),
                    audio_format=audio_format_var.get(),
                    video_quality=video_quality_var.get().lower(),
                    playlist=playlist_var.get(),
                    install_ffmpeg=install_ffmpeg_var.get(),
                    start_time=start_arg,
                    end_time=end_arg,
                    precise_cut=precise_cut_var.get(),
                    message_func=log_message,
                    progress_func=progress_message,
                )
                put_event("done")
            except BaseException as exc:
                put_event("download_error", str(exc))

        thread = threading.Thread(target=worker, daemon=True)
        active_thread["thread"] = thread
        thread.start()

    def pump_events() -> None:
        while True:
            try:
                event = events.get_nowait()
            except queue.Empty:
                break

            kind = event[0]
            if kind == "log":
                log_text.insert("end", f"{event[1]}\n")
                log_text.see("end")
            elif kind == "progress":
                percent, text = event[1], event[2]
                if percent is not None:
                    progress_var.set(max(0, min(100, percent)))
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
                load_button.configure(state="normal")
                update_section_preview()
            elif kind == "info_error":
                load_button.configure(state="normal")
                info_var.set("Nao foi possivel carregar os dados.")
                log_text.insert("end", f"[erro] {event[1]}\n")
                log_text.see("end")
                messagebox.showerror(APP_NAME, event[1])
            elif kind == "done":
                progress_var.set(100)
                status_var.set("Download concluido.")
                download_button.configure(state="normal")
                load_button.configure(state="normal")
                messagebox.showinfo(APP_NAME, "Download concluido.")
            elif kind == "download_error":
                status_var.set("Falha no download.")
                download_button.configure(state="normal")
                load_button.configure(state="normal")
                log_text.insert("end", f"[erro] {event[1]}\n")
                log_text.see("end")
                messagebox.showerror(APP_NAME, event[1])

        root.after(100, pump_events)

    browse_button.configure(command=choose_output_folder)
    open_folder_button.configure(command=open_output_folder)
    load_button.configure(command=load_video_info)
    download_button.configure(command=start_download)
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
    if section_answer.startswith("s"):
        start_time = ask("Inicio do trecho: segundos, MM:SS ou HH:MM:SS", "0")
        end_time = ask("Fim do trecho: segundos, MM:SS, HH:MM:SS ou fim", "fim")
        precise_cut_answer = ask("Corte preciso? s/n", "n").lower()
        precise_cut = precise_cut_answer.startswith("s")

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
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelado pelo usuario.")
        raise SystemExit(130)
