from __future__ import annotations

import random
from datetime import datetime
from zoneinfo import ZoneInfo

from app.services.captions import (
    MAX_CAPTION_CHARS,
    expand_spintax,
    parse_hashtags,
    render_caption,
)

MADRID = ZoneInfo("Europe/Madrid")


def test_spintax_picks_one_alternative():
    for seed in range(50):
        out = expand_spintax("{hola|buenas|ey}", random.Random(seed))
        assert out in {"hola", "buenas", "ey"}


def test_spintax_handles_nesting():
    for seed in range(50):
        out = expand_spintax("{a|{b|c}}", random.Random(seed))
        assert out in {"a", "b", "c"}


def test_spintax_leaves_braces_without_alternatives_untouched():
    assert expand_spintax("valor {literal} aquí") == "valor {literal} aquí"


def test_spintax_handles_several_groups():
    out = expand_spintax("{a|b} y {c|d} y {e|f}", random.Random(1))
    parts = out.split(" y ")
    assert len(parts) == 3
    assert parts[0] in "ab" and parts[1] in "cd" and parts[2] in "ef"


def test_spintax_terminates_on_malformed_input():
    # Unbalanced braces must not hang the renderer.
    assert isinstance(expand_spintax("{a|b" * 40), str)
    assert isinstance(expand_spintax("{}{}{}"), str)


def test_render_fills_placeholders():
    out = render_caption(
        "Hoy es [dia] [fecha] a las [hora] · vídeo [indice] · [archivo]",
        filename="/tmp/mi_video.mp4",
        index=7,
        now=datetime(2026, 6, 10, 18, 30, tzinfo=MADRID),
    )
    assert "miércoles" in out and "10/06/2026" in out and "18:30" in out
    assert "7" in out and "mi_video" in out


def test_hashtags_are_appended_when_not_referenced():
    out = render_caption(
        "Texto",
        hashtags=["fyp", "#viral", "parati"],
        hashtag_count=3,
        rng=random.Random(0),
    )
    assert out.startswith("Texto")
    for tag in ("#fyp", "#viral", "#parati"):
        assert tag in out


def test_hashtags_placement_is_respected_when_referenced():
    out = render_caption(
        "[hashtags] al principio",
        hashtags=["uno"], hashtag_count=1, rng=random.Random(0),
    )
    assert out == "#uno al principio"


def test_hashtag_sampling_never_repeats_a_tag():
    out = render_caption(
        "x", hashtags=["a", "b", "c", "d"], hashtag_count=4, rng=random.Random(3)
    )
    tags = [t for t in out.split() if t.startswith("#")]
    assert len(tags) == len(set(tags)) == 4


def test_hashtag_count_larger_than_pool_is_clamped():
    out = render_caption("x", hashtags=["a", "b"], hashtag_count=99)
    assert len([t for t in out.split() if t.startswith("#")]) == 2


def test_caption_is_truncated_to_the_limit():
    out = render_caption("palabra " * 1000)
    assert len(out) <= MAX_CAPTION_CHARS


def test_truncation_does_not_cut_mid_word():
    out = render_caption("abcdefgh " * 500)
    assert not out.endswith("abcdefg")
    assert len(out) <= MAX_CAPTION_CHARS


def test_unknown_placeholder_is_left_alone():
    assert "[desconocido]" in render_caption("hola [desconocido]")


def test_empty_template_is_safe():
    assert render_caption("") == ""


def test_parse_hashtags_accepts_mixed_separators():
    assert parse_hashtags("#uno, dos\n#tres  cuatro") == ["#uno", "#dos", "#tres", "#cuatro"]


def test_parse_hashtags_deduplicates():
    assert parse_hashtags("fyp #fyp fyp") == ["#fyp"]


def test_parse_hashtags_empty():
    assert parse_hashtags("") == [] and parse_hashtags("   ") == []


def test_captions_vary_across_renders():
    template = "{Mira esto|No te lo pierdas|Atención} {🔥|👀|✨}"
    seen = {render_caption(template, rng=random.Random(s)) for s in range(40)}
    assert len(seen) > 3, "las variantes deberían producir textos distintos"


def test_a_description_in_lines_gets_its_hashtags_on_a_line_of_their_own():
    out = render_caption("Gancho\nDato.\n¿Pregunta?", hashtags=["#uno", "#dos"],
                         hashtag_count=2, keep_order=True)
    assert out == "Gancho\nDato.\n¿Pregunta?\n#uno #dos"
    assert render_caption("Una línea", hashtags=["#uno"], hashtag_count=1) == "Una línea #uno"
