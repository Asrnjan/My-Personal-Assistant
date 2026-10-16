"""Versioned prompt templates stored as Markdown next to this module."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from importlib import resources


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    text: str


@cache
def load_prompt(name: str) -> Prompt:
    """Load ``<name>.md``. Its first line must be ``<!-- version: X -->``."""
    raw = resources.files(__package__).joinpath(f"{name}.md").read_text(encoding="utf-8")
    first, _, body = raw.partition("\n")
    prefix, suffix = "<!-- version: ", " -->"
    if not (first.startswith(prefix) and first.endswith(suffix)):
        raise ValueError(f"prompt {name!r} must start with '{prefix}X{suffix}'")
    return Prompt(name=name, version=first[len(prefix) : -len(suffix)], text=body.strip())


def untrusted(label: str, content: str) -> str:
    """Wrap external content (emails, tasks, invites) so the model treats it as data only."""
    safe = content.replace("</untrusted", "&lt;/untrusted")
    return f'<untrusted source="{label}">\n{safe}\n</untrusted>'
