"""Files the assistant makes for the CEO: PowerPoint decks for now.

The model writes the outline (a title, slides with bullets and speaker notes); the deck is
built here with python-pptx, kept in ``settings.files_dir`` and offered for download in
the app.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Emu, Inches, Pt
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.clock import Clock
from app.db.models import MemoRow, new_id
from app.engine.memory import Memory
from app.llm.client import LLMClient, Task
from app.llm.prompts import load_prompt
from app.llm.schemas import DeckOutline, Slide

PPTX_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
MAX_SLIDES = 15
MAX_BULLETS = 5
SLIDE_WIDTH = Emu(12192000)  # 16:9
SLIDE_HEIGHT = Emu(6858000)


@dataclass
class MadeFile:
    id: str
    name: str
    path: Path
    media_type: str
    slides: int = 0
    created_at: datetime | None = None


def safe_name(title: str) -> str:
    words = re.sub(r"[^A-Za-z0-9 \-]", "", title).split()
    return " ".join(words[:8]) or "Presentation"


class Documents:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        llm: LLMClient,
        files_dir: Path,
        clock: Clock,
        memory: Memory | None = None,
    ) -> None:
        self._sessions = session_factory
        self._llm = llm
        self._dir = files_dir
        self._clock = clock
        self._memory = memory

    def outline(self, topic: str, brief: str, slides: int) -> DeckOutline:
        facts = self._memory.recall([topic, brief], limit=10) if self._memory else []
        context = ("Facts you may use:\n" + "\n".join(facts) + "\n\n") if facts else ""
        return self._llm.structured(
            Task.REASONING,
            "deck",
            system=[load_prompt("deck").text],
            user=(
                f"{context}Topic: {topic}\nWhat it should cover: {brief or topic}\n"
                f"Number of content slides: {slides}"
            ),
            schema=DeckOutline,
            max_tokens=6000,
        )

    def presentation(self, topic: str, brief: str = "", slides: int = 6) -> MadeFile:
        outline = self.outline(topic, brief, max(1, min(slides, MAX_SLIDES)))
        file_id = new_id()
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"{file_id}.pptx"
        build_deck(outline, path)
        name = f"{safe_name(outline.title or topic)}.pptx"
        made = MadeFile(file_id, name, path, PPTX_TYPE, len(outline.slides[:MAX_SLIDES]))
        with self._sessions.begin() as s:
            s.add(
                MemoRow(
                    kind="file",
                    key=file_id,
                    text=name,
                    data={"path": str(path), "media_type": PPTX_TYPE, "slides": made.slides},
                    created_at=self._clock(),
                )
            )
        return made

    def recent(self, limit: int = 10) -> list[MadeFile]:
        """Files made so far that still exist, newest first."""
        with self._sessions() as s:
            rows = list(
                s.scalars(
                    select(MemoRow)
                    .where(MemoRow.kind == "file")
                    .order_by(MemoRow.created_at.desc())
                    .limit(limit * 2)
                )
            )
        out = []
        for row in rows:
            path = Path(str(row.data.get("path", "")))
            if path.is_file():
                media = str(row.data.get("media_type", PPTX_TYPE))
                slides = int(row.data.get("slides", 0) or 0)
                out.append(MadeFile(row.key, row.text, path, media, slides, row.created_at))
        return out[:limit]

    def get(self, file_id: str) -> MadeFile | None:
        if not re.fullmatch(r"[0-9a-f]{32}", file_id):
            return None
        with self._sessions() as s:
            row = s.scalars(
                select(MemoRow).where(MemoRow.kind == "file", MemoRow.key == file_id)
            ).first()
            if row is None:
                return None
            path = Path(str(row.data.get("path", "")))
            if not path.is_file():
                return None
            return MadeFile(file_id, row.text, path, str(row.data.get("media_type", PPTX_TYPE)))


def build_deck(outline: DeckOutline, path: Path) -> None:
    """A designed 16:9 deck: a cover, one slide per section in the layout that suits its
    content, and a closing slide with the ask. Speaker notes on every slide."""
    theme = Theme.from_accent(outline.accent_color)
    deck = Presentation()
    deck.slide_width, deck.slide_height = SLIDE_WIDTH, SLIDE_HEIGHT
    blank = deck.slide_layouts[6]
    slides = outline.slides[:MAX_SLIDES]

    cover = deck.slides.add_slide(blank)
    _cover(cover, outline, theme)
    cover.notes_slide.notes_text_frame.text = outline.subtitle

    for number, item in enumerate(slides, start=2):
        slide = deck.slides.add_slide(blank)
        if item.layout == "section":
            _section(slide, item, theme)
        else:
            _chrome(slide, item, theme, outline.title, number)
            LAYOUTS.get(item.layout, _bullets)(slide, item, theme)
        slide.notes_slide.notes_text_frame.text = item.notes

    closing = deck.slides.add_slide(blank)
    _closing(closing, outline, theme)
    deck.save(str(path))


# --- design -------------------------------------------------------------------------

FONT = "Segoe UI"
W, H = 13.333, 7.5  # inches, 16:9
MARGIN = 0.8


def _rgb(r: int, g: int, b: int) -> RGBColor:
    return RGBColor(r, g, b)  # type: ignore[no-untyped-call]


DARK = _rgb(0x10, 0x1B, 0x33)
INK = _rgb(0x1F, 0x23, 0x2B)
MUTED = _rgb(0x5B, 0x64, 0x75)
SOFT = _rgb(0xF2, 0xF4, 0xF8)
WHITE = _rgb(0xFF, 0xFF, 0xFF)


@dataclass(frozen=True)
class Theme:
    accent: RGBColor
    tint: RGBColor  # a pale version of the accent, for cards
    dark: RGBColor = DARK
    ink: RGBColor = INK
    muted: RGBColor = MUTED
    soft: RGBColor = SOFT
    white: RGBColor = WHITE

    @classmethod
    def from_accent(cls, value: str | None) -> Theme:
        rgb = _hex(value) or (0x2F, 0x6F, 0xED)
        r, g, b = (round(c + (255 - c) * 0.88) for c in rgb)
        return cls(accent=_rgb(*rgb), tint=_rgb(r, g, b))


def _hex(value: str | None) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"#?([0-9a-fA-F]{6})", (value or "").strip())
    if not match:
        return None
    raw = match.group(1)
    rgb = (int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16))
    # A brand colour too pale to read on white falls back to the default.
    return rgb if sum(rgb) < 600 else None


def _box(
    slide: Any, x: float, y: float, w: float, h: float, color: RGBColor, *, round_: bool = False
) -> Any:
    kind = MSO_SHAPE.ROUNDED_RECTANGLE if round_ else MSO_SHAPE.RECTANGLE
    shape = slide.shapes.add_shape(kind, Inches(x), Inches(y), Inches(w), Inches(h))
    shape.fill.solid()
    shape.fill.fore_color.rgb = color
    shape.line.fill.background()
    shape.shadow.inherit = False
    if round_:
        shape.adjustments[0] = 0.08
    return shape


def _dot(slide: Any, x: float, y: float, size: float, color: RGBColor) -> Any:
    shape = slide.shapes.add_shape(MSO_SHAPE.OVAL, Inches(x), Inches(y), Inches(size), Inches(size))
    shape.fill.solid()
    shape.fill.fore_color.rgb = color
    shape.line.fill.background()
    shape.shadow.inherit = False
    return shape


def _text(
    slide: Any,
    x: float,
    y: float,
    w: float,
    h: float,
    text: str | list[str],
    size: int,
    color: RGBColor,
    *,
    bold: bool = False,
    italic: bool = False,
    align: Any = PP_ALIGN.LEFT,
    anchor: Any = MSO_ANCHOR.TOP,
    spacing: int = 0,
) -> Any:
    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    frame = box.text_frame
    frame.word_wrap = True
    frame.vertical_anchor = anchor
    frame.margin_left = frame.margin_right = frame.margin_top = frame.margin_bottom = 0
    lines = text if isinstance(text, list) else [text]
    for n, line in enumerate(lines):
        paragraph = frame.paragraphs[0] if n == 0 else frame.add_paragraph()
        paragraph.alignment = align
        if spacing:
            paragraph.space_after = Pt(spacing)
        run = paragraph.add_run()
        run.text = line
        font = run.font
        font.name = FONT
        font.size = Pt(size)
        font.bold = bold
        font.italic = italic
        font.color.rgb = color
    return box


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split()).rstrip(".")
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _chrome(slide: Any, item: Slide, theme: Theme, deck_title: str, number: int) -> None:
    """What every content slide shares: kicker, headline, accent rule and footer."""
    if item.kicker:
        _text(slide, MARGIN, 0.55, 8, 0.3, item.kicker.upper(), 12, theme.accent, bold=True)
    _text(slide, MARGIN, 0.85, W - 2 * MARGIN, 1.2, _clip(item.title, 90), 30, theme.ink, bold=True)
    _box(slide, MARGIN, 2.05, 0.7, 0.06, theme.accent)
    _text(slide, MARGIN, 7.0, 8, 0.3, _clip(deck_title, 70), 10, theme.muted)
    _text(slide, W - MARGIN - 1, 7.0, 1, 0.3, str(number), 10, theme.muted, align=PP_ALIGN.RIGHT)


def _cover(slide: Any, outline: DeckOutline, theme: Theme) -> None:
    _box(slide, 0, 0, W, H, theme.dark)
    _box(slide, 0, 0, 0.28, H, theme.accent)
    _box(slide, W - 3.2, -1.2, 4.4, 4.4, theme.accent).fill.fore_color.brightness = 0
    accent_corner = slide.shapes[-1]
    accent_corner.rotation = 45
    _text(
        slide,
        1.2,
        2.2,
        9.5,
        2.2,
        _clip(outline.title, 80),
        44,
        theme.white,
        bold=True,
        anchor=MSO_ANCHOR.BOTTOM,
    )
    _box(slide, 1.2, 4.6, 1.0, 0.07, theme.accent)
    _text(slide, 1.2, 4.85, 9.5, 1.0, _clip(outline.subtitle, 120), 20, _rgb(0xC9, 0xD2, 0xE3))
    if outline.presenter:
        _text(slide, 1.2, 6.6, 9, 0.4, _clip(outline.presenter, 80), 14, _rgb(0x9A, 0xA6, 0xBD))


def _section(slide: Any, item: Slide, theme: Theme) -> None:
    _box(slide, 0, 0, W, H, theme.accent)
    if item.kicker:
        _text(slide, 1.2, 2.4, 10, 0.4, item.kicker.upper(), 14, theme.white, bold=True)
    _text(slide, 1.2, 2.9, 10.5, 2.0, _clip(item.title, 80), 40, theme.white, bold=True)


def _bullets(slide: Any, item: Slide, theme: Theme) -> None:
    points = [_clip(b, 110) for b in item.bullets[:MAX_BULLETS] if b.strip()]
    if not points:
        return
    size = 24 if len(points) <= 3 else 20
    row = min(0.95, 4.3 / len(points))
    for n, point in enumerate(points):
        y = 2.45 + n * row
        _box(slide, MARGIN, y + 0.13, 0.16, 0.16, theme.accent)
        _text(slide, MARGIN + 0.45, y, W - 2 * MARGIN - 0.45, row, point, size, theme.ink)


def _two_column(slide: Any, item: Slide, theme: Theme) -> None:
    width = (W - 2 * MARGIN - 0.4) / 2
    sides = [
        (MARGIN, item.left_heading, item.left_points, theme.soft, theme.ink),
        (MARGIN + width + 0.4, item.right_heading, item.right_points, theme.tint, theme.accent),
    ]
    rows = max(len([p for p in points[:4] if p.strip()]) for _, _, points, _, _ in sides)
    height = min(4.25, 1.6 + 0.6 * rows)  # fit the cards to the longer list
    for x, heading, points, fill, head_color in sides:
        _box(slide, x, 2.4, width, height, fill, round_=True)
        _text(slide, x + 0.4, 2.7, width - 0.8, 0.5, _clip(heading, 40), 20, head_color, bold=True)
        lines = [f"•  {_clip(p, 70)}" for p in points[:4] if p.strip()]
        _text(slide, x + 0.4, 3.4, width - 0.8, 3.1, lines, 16, theme.ink, spacing=10)


def _big_numbers(slide: Any, item: Slide, theme: Theme) -> None:
    stats = item.stats[:4]
    if not stats:
        _bullets(slide, item, theme)
        return
    gap = 0.3
    width = (W - 2 * MARGIN - gap * (len(stats) - 1)) / len(stats)
    for n, stat in enumerate(stats):
        x = MARGIN + n * (width + gap)
        _box(slide, x, 2.6, width, 2.7, theme.soft, round_=True)
        _box(slide, x, 2.6, width, 0.08, theme.accent)
        _text(
            slide,
            x + 0.3,
            3.1,
            width - 0.6,
            1.3,
            _clip(stat.value, 14),
            44,
            theme.accent,
            bold=True,
        )
        _text(slide, x + 0.3, 4.5, width - 0.6, 1.3, _clip(stat.label, 60), 16, theme.muted)


def _process(slide: Any, item: Slide, theme: Theme) -> None:
    steps = item.steps[:5]
    if not steps:
        _bullets(slide, item, theme)
        return
    width = (W - 2 * MARGIN) / len(steps)
    size = 0.75
    y = 2.8
    _box(slide, MARGIN + width / 2, y + size / 2 - 0.02, width * (len(steps) - 1), 0.04, theme.tint)
    for n, step in enumerate(steps):
        cx = MARGIN + width * n + width / 2
        dot = _dot(slide, cx - size / 2, y, size, theme.accent)
        frame = dot.text_frame
        frame.text = str(n + 1)
        run = frame.paragraphs[0].runs[0]
        frame.paragraphs[0].alignment = PP_ALIGN.CENTER
        run.font.size, run.font.bold, run.font.name = Pt(20), True, FONT
        run.font.color.rgb = theme.white
        _text(
            slide,
            cx - width / 2 + 0.15,
            y + 1.0,
            width - 0.3,
            0.5,
            _clip(step.title, 30),
            17,
            theme.ink,
            bold=True,
            align=PP_ALIGN.CENTER,
        )
        _text(
            slide,
            cx - width / 2 + 0.15,
            y + 1.55,
            width - 0.3,
            1.8,
            _clip(step.text, 90),
            14,
            theme.muted,
            align=PP_ALIGN.CENTER,
        )


def _timeline(slide: Any, item: Slide, theme: Theme) -> None:
    steps = item.steps[:5]
    if not steps:
        _bullets(slide, item, theme)
        return
    width = (W - 2 * MARGIN) / len(steps)
    line_y = 3.9
    _box(slide, MARGIN, line_y, W - 2 * MARGIN, 0.05, theme.tint)
    for n, step in enumerate(steps):
        cx = MARGIN + width * n + width / 2
        _dot(slide, cx - 0.14, line_y - 0.115, 0.28, theme.accent)
        _text(
            slide,
            cx - width / 2 + 0.1,
            line_y - 1.0,
            width - 0.2,
            0.7,
            _clip(step.title, 24),
            18,
            theme.accent,
            bold=True,
            align=PP_ALIGN.CENTER,
            anchor=MSO_ANCHOR.BOTTOM,
        )
        _text(
            slide,
            cx - width / 2 + 0.1,
            line_y + 0.45,
            width - 0.2,
            1.8,
            _clip(step.text, 90),
            14,
            theme.ink,
            align=PP_ALIGN.CENTER,
        )


def _quote(slide: Any, item: Slide, theme: Theme) -> None:
    _text(slide, MARGIN, 2.2, 1.2, 1.4, "\u201c", 120, theme.accent, bold=True)
    _text(
        slide,
        MARGIN + 1.2,
        2.6,
        W - 2 * MARGIN - 1.2,
        2.6,
        _clip(item.quote, 220),
        28,
        theme.ink,
        italic=True,
    )
    if item.attribution:
        _text(slide, MARGIN + 1.2, 5.4, 9, 0.5, f"— {_clip(item.attribution, 70)}", 16, theme.muted)


def _closing(slide: Any, outline: DeckOutline, theme: Theme) -> None:
    _box(slide, 0, 0, W, H, theme.dark)
    _box(slide, 0, 0, 0.28, H, theme.accent)
    _text(slide, 1.2, 0.9, 10, 0.4, "NEXT STEPS", 14, theme.accent, bold=True)
    _text(
        slide,
        1.2,
        1.35,
        11,
        1.3,
        _clip(outline.closing_title or "Next steps", 80),
        36,
        theme.white,
        bold=True,
    )
    for n, point in enumerate(p for p in outline.closing_points[:4] if p.strip()):
        y = 3.0 + n * 0.85
        dot = _dot(slide, 1.2, y, 0.5, theme.accent)
        frame = dot.text_frame
        frame.text = str(n + 1)
        frame.paragraphs[0].alignment = PP_ALIGN.CENTER
        run = frame.paragraphs[0].runs[0]
        run.font.size, run.font.bold, run.font.name = Pt(16), True, FONT
        run.font.color.rgb = theme.white
        _text(slide, 2.0, y + 0.05, 10, 0.6, _clip(point, 90), 20, _rgb(0xE6, 0xEA, 0xF2))
    _text(slide, 1.2, 6.6, 6, 0.4, "Thank you", 16, _rgb(0x9A, 0xA6, 0xBD), bold=True)


LAYOUTS: dict[str, Any] = {
    "bullets": _bullets,
    "two_column": _two_column,
    "big_numbers": _big_numbers,
    "process": _process,
    "timeline": _timeline,
    "quote": _quote,
}
