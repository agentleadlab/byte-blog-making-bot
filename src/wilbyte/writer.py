"""Write new copy in the house voice, grounded in past copy.

The corpus supplies the voice, the format supplies the constraints, and the
brief supplies the idea. All three go into one call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, REPO_ROOT
from .corpus import Corpus, Piece
from .formats import Format

PROMPT_PATH = REPO_ROOT / "prompts" / "house_voice.md"


class WriterError(RuntimeError):
    """Raised when copy generation fails or returns unusable output."""


@dataclass
class Variant:
    """One option, as a mapping of field key -> text."""

    fields: dict[str, str]

    def get(self, key: str) -> str:
        return self.fields.get(key, "")


@dataclass
class CopyResult:
    format: Format
    brief: str
    variants: list[Variant]
    notes: str = ""
    examples_used: list[Piece] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def grounded(self) -> bool:
        return bool(self.examples_used)


def load_house_prompt(path: Path | None = None) -> str:
    prompt_path = path or PROMPT_PATH
    if not prompt_path.exists():
        raise WriterError(f"House voice prompt not found: {prompt_path}")
    return prompt_path.read_text(encoding="utf-8")


def build_system_prompt(fmt: Format, *, path: Path | None = None) -> str:
    """House voice plus the rules for this one format."""
    limits = "\n".join(
        f"- **{f.label}** ({f.key}): {f.guidance}"
        + (f" Max {f.max_chars} characters." if f.max_chars else "")
        for f in fmt.fields
    )
    return (
        f"{load_house_prompt(path)}\n\n"
        f"## This request: {fmt.label}\n\n"
        f"{fmt.guidance}\n\n"
        f"### Fields to produce\n{limits}\n\n"
        f"Return {fmt.variants} variants."
    )


def build_user_message(brief: str, examples: list[Piece], fmt: Format) -> str:
    parts = []

    if examples:
        parts.append(
            "Here is copy Agent Lead Lab has already published. Study the voice "
            "and write in it.\n"
        )
        for index, piece in enumerate(examples, start=1):
            parts.append(f"--- EXAMPLE {index} ---\n{piece.for_prompt()}\n")
    else:
        parts.append(
            "No past copy is available for reference yet, so write from the house "
            "voice guidance alone and keep it conservative.\n"
        )

    parts.append(f"--- BRIEF ---\n{brief.strip()}\n")
    parts.append(
        f"Write {fmt.variants} {fmt.label} variants for this brief. "
        "Call emit_copy with the result."
    )
    return "\n".join(parts)


def generate(
    brief: str,
    fmt: Format,
    config: Config,
    corpus: Corpus,
    *,
    label: str | None = None,
    prompt_path: Path | None = None,
) -> CopyResult:
    """Retrieve relevant past copy, then write new copy in that voice."""
    if not brief.strip():
        raise WriterError("Give me something to write about.")

    config.secrets.require("anthropic_api_key")

    examples = corpus.search(brief, label=label or fmt.key)
    tool = {
        "name": "emit_copy",
        "description": f"Return {fmt.variants} {fmt.label} variants.",
        "input_schema": fmt.output_schema(),
    }

    from anthropic import Anthropic

    client = Anthropic(api_key=config.secrets.anthropic_api_key)
    try:
        response = client.messages.create(
            model=config.copy.model,
            max_tokens=config.copy.max_tokens,
            system=build_system_prompt(fmt, path=prompt_path),
            tools=[tool],
            tool_choice={"type": "tool", "name": "emit_copy"},
            messages=[{"role": "user", "content": build_user_message(brief, examples, fmt)}],
        )
    except Exception as exc:
        raise WriterError(f"Anthropic request failed: {exc}") from exc

    payload = _extract(response)
    return parse_result(payload, fmt, brief, examples)


def _extract(response) -> dict:
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "emit_copy":
            return dict(block.input)
    raise WriterError(
        f"Model did not return copy (stop reason: {getattr(response, 'stop_reason', '?')})."
    )


def parse_result(payload: dict, fmt: Format, brief: str, examples: list[Piece]) -> CopyResult:
    """Validate the model output and flag any field that overran its limit."""
    raw_variants = payload.get("variants") or []
    if not raw_variants:
        raise WriterError("Model returned no variants.")

    limits = {f.key: f.max_chars for f in fmt.fields if f.max_chars}
    variants: list[Variant] = []
    warnings: list[str] = []

    for index, raw in enumerate(raw_variants, start=1):
        if not isinstance(raw, dict):
            continue
        fields = {}
        for spec in fmt.fields:
            value = str(raw.get(spec.key) or "").strip()
            limit = limits.get(spec.key)
            # Report the overrun rather than silently trimming - a cut-off SMS
            # is a different message, and the operator should decide.
            if limit and len(value) > limit:
                warnings.append(
                    f"variant {index} {spec.label.lower()} is {len(value)} chars "
                    f"(limit {limit})"
                )
            fields[spec.key] = value
        if any(fields.values()):
            variants.append(Variant(fields=fields))

    if not variants:
        raise WriterError("Model returned variants with no content.")

    return CopyResult(
        format=fmt,
        brief=brief.strip(),
        variants=variants,
        notes=str(payload.get("notes") or "").strip(),
        examples_used=examples,
        warnings=warnings,
    )


def render_text(result: CopyResult) -> str:
    """Plain-text rendering, for the CLI and for saving to disk."""
    lines = [f"{result.format.label} — {result.brief}", ""]
    for index, variant in enumerate(result.variants, start=1):
        lines.append(f"--- Variant {index} ---")
        for spec in result.format.fields:
            value = variant.get(spec.key)
            if not value:
                continue
            if spec.multiline:
                lines.append(f"{spec.label}:")
                lines.append(value)
            else:
                lines.append(f"{spec.label}: {value}")
        lines.append("")
    if result.notes:
        lines.extend(["Notes:", result.notes, ""])
    if result.examples_used:
        lines.append(f"Grounded in {len(result.examples_used)} past piece(s).")
    return "\n".join(lines)
