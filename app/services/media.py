"""Local video inspection and pre-flight validation.

Catching a bad file here costs a second; catching it at TikTok costs a failed
publish, a burnt slot and a confusing error code.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

MB = 1024 * 1024
MAX_FILE_BYTES = 4 * 1024 * MB
MIN_SIDE_PX = 360
MAX_SIDE_PX = 4096
MIN_FPS = 23.0
MAX_FPS = 60.0
# TikTok caps API posts at 10 minutes; the account's own limit may be lower and
# is read from creator_info.max_video_post_duration_sec.
API_MAX_DURATION_SEC = 600
SUPPORTED_SUFFIXES = {".mp4", ".mov", ".webm"}
RECOMMENDED_ASPECT = 9 / 16


@dataclass
class Probe:
    duration_sec: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    video_codec: str = ""
    audio_codec: str = ""
    has_audio: bool = False
    bitrate_kbps: int = 0
    size_bytes: int = 0
    container: str = ""
    probed: bool = False
    probe_error: str = ""

    def as_dict(self) -> dict:
        return self.__dict__.copy()

    @property
    def aspect_ratio(self) -> float:
        return (self.width / self.height) if self.height else 0.0


@dataclass
class Preflight:
    probe: Probe
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def ffprobe_available() -> bool:
    return shutil.which("ffprobe") is not None


def _parse_fps(rate: str | None) -> float:
    if not rate or rate in ("0/0", "0"):
        return 0.0
    try:
        return float(Fraction(rate))
    except (ValueError, ZeroDivisionError):
        return 0.0


def probe_video(path: str | Path) -> Probe:
    p = Path(path)
    result = Probe(size_bytes=p.stat().st_size if p.exists() else 0)

    if not p.exists():
        result.probe_error = "el archivo no existe"
        return result
    if not ffprobe_available():
        result.probe_error = "ffprobe no encontrado (instala ffmpeg para validar los vídeos)"
        return result

    try:
        raw = subprocess.run(
            [
                "ffprobe", "-v", "error", "-print_format", "json",
                "-show_format", "-show_streams", str(p),
            ],
            capture_output=True, text=True, timeout=60, check=True,
        ).stdout
        data = json.loads(raw)
    except subprocess.CalledProcessError as exc:
        result.probe_error = (exc.stderr or "ffprobe falló").strip()[:300]
        return result
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
        result.probe_error = f"ffprobe falló: {exc}"[:300]
        return result

    fmt = data.get("format", {})
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    result.container = fmt.get("format_name", "")
    try:
        result.duration_sec = float(fmt.get("duration") or 0.0)
    except ValueError:
        result.duration_sec = 0.0
    try:
        result.bitrate_kbps = int(int(fmt.get("bit_rate") or 0) / 1000)
    except ValueError:
        result.bitrate_kbps = 0

    if video:
        result.width = int(video.get("width") or 0)
        result.height = int(video.get("height") or 0)
        result.video_codec = video.get("codec_name", "")
        result.fps = _parse_fps(video.get("avg_frame_rate") or video.get("r_frame_rate"))
        if not result.duration_sec:
            try:
                result.duration_sec = float(video.get("duration") or 0.0)
            except ValueError:
                pass
        # Vertical video shot on a phone is often stored landscape + a rotation
        # tag. Report the displayed dimensions, not the stored ones.
        rotation = _rotation_of(video)
        if rotation in (90, 270):
            result.width, result.height = result.height, result.width

    if audio:
        result.has_audio = True
        result.audio_codec = audio.get("codec_name", "")

    result.probed = True
    return result


def _rotation_of(stream: dict) -> int:
    tags = stream.get("tags") or {}
    try:
        rot = int(float(tags.get("rotate", 0)))
    except (TypeError, ValueError):
        rot = 0
    for side in stream.get("side_data_list") or []:
        if "rotation" in side:
            try:
                rot = int(float(side["rotation"]))
            except (TypeError, ValueError):
                pass
    return abs(rot) % 360


def preflight(path: str | Path, max_duration_sec: int | None = None) -> Preflight:
    """Validate a file against TikTok's documented media limits."""
    p = Path(path)
    probe = probe_video(p)
    errors: list[str] = []
    warnings: list[str] = []

    if not p.exists():
        return Preflight(probe, ["El archivo no existe."], [])

    if p.suffix.lower() not in SUPPORTED_SUFFIXES:
        errors.append(
            f"Extensión no soportada ({p.suffix or 'sin extensión'}). "
            f"Usa {', '.join(sorted(SUPPORTED_SUFFIXES))}."
        )

    if probe.size_bytes == 0:
        errors.append("El archivo está vacío.")
    elif probe.size_bytes > MAX_FILE_BYTES:
        errors.append(f"El archivo pesa {probe.size_bytes / MB:.0f} MB; el máximo son 4 GB.")

    if not probe.probed:
        if not ffprobe_available():
            # We simply cannot check. Let it through rather than block the user.
            warnings.append(
                "ffprobe no está instalado, así que no se ha validado el vídeo. "
                "Se subirá igualmente. Instala ffmpeg para detectar problemas antes."
            )
        else:
            # ffprobe works but choked on this file: it is not a usable video.
            errors.append(
                f"No se pudo leer el archivo como vídeo ({probe.probe_error or 'formato ilegible'}). "
                "Probablemente está corrupto o incompleto."
            )
        return Preflight(probe, errors, warnings)

    if not probe.video_codec:
        errors.append("El archivo no contiene pista de vídeo.")

    limit = min(max_duration_sec or API_MAX_DURATION_SEC, API_MAX_DURATION_SEC)
    if probe.duration_sec <= 0:
        warnings.append("No se pudo determinar la duración del vídeo.")
    elif probe.duration_sec > limit:
        errors.append(
            f"Dura {probe.duration_sec:.0f}s y tu cuenta permite {limit}s por la API."
        )
    elif probe.duration_sec < 3:
        warnings.append(f"Sólo dura {probe.duration_sec:.1f}s; TikTok penaliza los vídeos muy cortos.")

    for label, value in (("ancho", probe.width), ("alto", probe.height)):
        if value and not (MIN_SIDE_PX <= value <= MAX_SIDE_PX):
            errors.append(
                f"El {label} es {value}px y debe estar entre {MIN_SIDE_PX} y {MAX_SIDE_PX}px."
            )

    if probe.fps and not (MIN_FPS <= probe.fps <= MAX_FPS):
        errors.append(f"Los FPS son {probe.fps:.1f} y deben estar entre {MIN_FPS:.0f} y {MAX_FPS:.0f}.")

    if probe.video_codec and probe.video_codec not in ("h264", "hevc", "vp8", "vp9", "av1"):
        warnings.append(
            f"Códec de vídeo poco habitual ({probe.video_codec}). "
            "H.264 en MP4 es lo más seguro."
        )

    if not probe.has_audio:
        warnings.append(
            "El vídeo no tiene pista de audio. TikTok distribuye peor el contenido mudo."
        )

    if probe.aspect_ratio:
        deviation = abs(probe.aspect_ratio - RECOMMENDED_ASPECT) / RECOMMENDED_ASPECT
        if deviation > 0.12:
            warnings.append(
                f"La relación de aspecto es {probe.width}x{probe.height}; "
                "9:16 vertical (p. ej. 1080x1920) rinde bastante mejor."
            )

    if probe.bitrate_kbps and probe.bitrate_kbps < 800:
        warnings.append(f"Bitrate bajo ({probe.bitrate_kbps} kbps); se verá pixelado.")

    return Preflight(probe, errors, warnings)


def checksum(path: str | Path, chunk: int = 1 * MB) -> str:
    """Content hash used to deduplicate the queue.

    Hashes the head, the tail and the file size rather than every byte: a full
    SHA-256 of a 500 MB video is wasted work when we only need to recognise the
    same file being imported twice.
    """
    p = Path(path)
    h = hashlib.sha256()
    size = p.stat().st_size
    h.update(str(size).encode())
    with p.open("rb") as fh:
        h.update(fh.read(chunk))
        if size > chunk * 2:
            fh.seek(-chunk, 2)
            h.update(fh.read(chunk))
    return h.hexdigest()
