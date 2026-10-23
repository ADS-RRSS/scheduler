"""make doctor runs anywhere, even with nothing set up, and says what it found."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

DOCTOR = Path(__file__).resolve().parent.parent / "tools" / "doctor.py"


def test_the_doctor_runs_offline_and_reports_every_section(fresh_db):
    # The test environment (conftest) points the database and folders at a
    # temporary tree: the doctor reads those, never the real ones.
    out = subprocess.run([sys.executable, str(DOCTOR), "--offline"], capture_output=True,
                         text=True, timeout=60)
    assert out.returncode in (0, 1), out.stderr
    assert "Traceback" not in out.stderr
    for section in ("Python", "Herramientas", "Configuración", "Base de datos", "Disco y vídeos",
                    "Textos automáticos", "Red", "Puerto"):
        assert f"\n{section}\n" in out.stdout, section
    assert "Sin comprobar (--offline)" in out.stdout
    assert "integridad ok" in out.stdout
