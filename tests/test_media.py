"""Pre-flight validation against real files produced by ffmpeg.

These use actual encoded video rather than fixtures, so they catch the thing a
mock never would: ffprobe reporting something we did not expect.
"""
from __future__ import annotations

import shutil
import subprocess

import pytest

from app.services import media

pytestmark = pytest.mark.skipif(
    not shutil.which("ffmpeg"), reason="ffmpeg no disponible"
)


def make_video(path, *, size="1080x1920", rate=30, duration=3, audio=True, codec="libx264"):
    cmd = ["ffmpeg", "-y", "-v", "error",
           "-f", "lavfi", "-i", f"testsrc=size={size}:rate={rate}:duration={duration}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}", "-c:a", "aac"]
    else:
        cmd += ["-an"]
    cmd += ["-c:v", codec, "-pix_fmt", "yuv420p", "-preset", "ultrafast", "-shortest", str(path)]
    subprocess.run(cmd, check=True, capture_output=True)
    return path


# --------------------------------------------------------------------------- #
# Probing
# --------------------------------------------------------------------------- #

def test_probe_reads_the_real_properties(tmp_path):
    video = make_video(tmp_path / "v.mp4", size="1080x1920", rate=30, duration=3)
    probe = media.probe_video(video)
    assert probe.probed
    assert (probe.width, probe.height) == (1080, 1920)
    assert probe.fps == pytest.approx(30, abs=0.5)
    assert probe.duration_sec == pytest.approx(3, abs=0.3)
    assert probe.video_codec == "h264"
    assert probe.has_audio and probe.audio_codec == "aac"
    assert probe.size_bytes > 0


def test_probe_reports_missing_audio(tmp_path):
    probe = media.probe_video(make_video(tmp_path / "mute.mp4", audio=False))
    assert probe.probed and probe.has_audio is False


def test_probe_on_a_missing_file(tmp_path):
    probe = media.probe_video(tmp_path / "no-existe.mp4")
    assert not probe.probed and "no existe" in probe.probe_error


def test_probe_on_garbage(tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"no soy un video")
    probe = media.probe_video(bad)
    assert not probe.probed and probe.probe_error


# --------------------------------------------------------------------------- #
# Pre-flight: what blocks and what merely warns
# --------------------------------------------------------------------------- #

def test_good_vertical_video_passes_clean(tmp_path):
    check = media.preflight(make_video(tmp_path / "ok.mp4"))
    assert check.ok
    assert not any("aspecto" in w for w in check.warnings)


def test_horizontal_video_warns_but_is_allowed(tmp_path):
    check = media.preflight(make_video(tmp_path / "wide.mp4", size="1920x1080"))
    assert check.ok, check.errors
    assert any("aspecto" in w for w in check.warnings)


def test_missing_audio_warns_but_is_allowed(tmp_path):
    check = media.preflight(make_video(tmp_path / "mute.mp4", audio=False))
    assert check.ok, check.errors
    assert any("audio" in w for w in check.warnings)


def test_very_short_video_warns(tmp_path):
    check = media.preflight(make_video(tmp_path / "short.mp4", duration=1))
    assert check.ok
    assert any("cortos" in w for w in check.warnings)


def test_duration_over_the_account_limit_is_an_error(tmp_path):
    video = make_video(tmp_path / "long.mp4", duration=5)
    check = media.preflight(video, max_duration_sec=2)
    assert not check.ok
    assert any("permite" in e for e in check.errors)
    # The same file is fine when the account allows more.
    assert media.preflight(video, max_duration_sec=600).ok


def test_resolution_below_the_minimum_is_an_error(tmp_path):
    check = media.preflight(make_video(tmp_path / "tiny.mp4", size="200x356"))
    assert not check.ok
    assert any("ancho" in e or "alto" in e for e in check.errors)


def test_framerate_below_the_minimum_is_an_error(tmp_path):
    check = media.preflight(make_video(tmp_path / "slow.mp4", rate=10))
    assert not check.ok
    assert any("FPS" in e for e in check.errors)


def test_unsupported_extension_is_an_error(tmp_path):
    src = make_video(tmp_path / "v.mp4")
    renamed = tmp_path / "v.avi"
    src.rename(renamed)
    check = media.preflight(renamed)
    assert not check.ok
    assert any("Extensión" in e for e in check.errors)


def test_corrupt_file_is_an_error_not_a_warning(tmp_path):
    bad = tmp_path / "roto.mp4"
    bad.write_bytes(b"esto no es un video")
    check = media.preflight(bad)
    assert not check.ok
    assert any("corrupto" in e or "No se pudo leer" in e for e in check.errors)


def test_empty_file_is_an_error(tmp_path):
    empty = tmp_path / "vacio.mp4"
    empty.touch()
    check = media.preflight(empty)
    assert not check.ok
    assert any("vacío" in e for e in check.errors)


def test_missing_file_is_an_error(tmp_path):
    check = media.preflight(tmp_path / "fantasma.mp4")
    assert not check.ok


# --------------------------------------------------------------------------- #
# Deduplication hash
# --------------------------------------------------------------------------- #

def test_checksum_is_stable_for_the_same_content(tmp_path):
    a = make_video(tmp_path / "a.mp4")
    b = tmp_path / "b.mp4"
    b.write_bytes(a.read_bytes())
    assert media.checksum(a) == media.checksum(b), "renombrar no debe cambiar el hash"


def test_checksum_differs_for_different_content(tmp_path):
    a = make_video(tmp_path / "a.mp4", duration=3)
    b = make_video(tmp_path / "b.mp4", duration=5)
    assert media.checksum(a) != media.checksum(b)


def test_checksum_handles_files_smaller_than_the_window(tmp_path):
    small = tmp_path / "small.bin"
    small.write_bytes(b"abc")
    assert len(media.checksum(small)) == 64
