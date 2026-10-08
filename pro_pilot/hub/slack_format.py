"""Parse model answers and render Slack Block Kit with valid mrkdwn."""

from __future__ import annotations

import re
from dataclasses import dataclass

# Slack section block mrkdwn text must stay under 3000 characters.
_BLOCK_CHAR_LIMIT = 2900
_HEADER_CHAR_LIMIT = 150
_STATUS_EMOJI = {
    ":white_check_mark:": "✅",
    ":warning:": "⚠️",
    ":red_circle:": "🔴",
    ":grey_question:": "❔",
    ":information_source:": "ℹ️",
}


@dataclass(frozen=True)
class ParsedAnswer:
    summary: str
    evidence: tuple[str, ...]
    next_steps: tuple[str, ...]


def normalize_slack_mrkdwn(text: str) -> str:
    """Convert common Markdown habits to Slack mrkdwn."""
    parts = re.split(r"(```.*?(?:```|$)|`[^`\n]*`)", text.replace("\r\n", "\n"), flags=re.DOTALL)
    for index in range(0, len(parts), 2):
        part = re.sub(r"\*\*(.+?)\*\*", r"*\1*", parts[index])
        part = re.sub(r"__(.+?)__", r"*\1*", part)
        parts[index] = re.sub(r"^#{1,3}\s+", "", part, flags=re.MULTILINE)
    return "".join(parts).strip()


def parse_answer(text: str) -> ParsedAnswer:
    lines = normalize_slack_mrkdwn(text).split("\n")
    summary: list[str] = []
    evidence: list[str] = []
    next_steps: list[str] = []
    section = "summary"
    in_code_block = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```") or in_code_block:
            if section == "summary":
                summary.append(line)
            else:
                items = evidence if section == "evidence" else next_steps
                if items:
                    items[-1] += "\n" + line
                else:
                    items.append(line)
            if stripped.count("```") % 2:
                in_code_block = not in_code_block
            continue
        if _is_section_header(stripped, "what I found") or _is_section_header(stripped, "evidence"):
            section = "evidence"
            continue
        if _is_section_header(stripped, "what to do next") or _is_section_header(
            stripped, "next steps"
        ):
            section = "next_steps"
            continue
        if not stripped:
            if section == "summary" and summary:
                summary.append("")
            continue
        if section == "summary":
            summary.append(stripped)
        elif section == "evidence":
            evidence.append(_strip_bullet(stripped))
        else:
            next_steps.append(_strip_bullet(stripped))
    return ParsedAnswer(
        summary="\n".join(summary).strip(),
        evidence=tuple(evidence),
        next_steps=tuple(next_steps),
    )


def build_answer_payload(answer: str, footer: str) -> tuple[str, list[dict[str, object]]]:
    """Return notification fallback text and Block Kit blocks for chat.update."""
    parsed = parse_answer(answer)
    blocks: list[dict[str, object]] = []
    if parsed.summary:
        blocks.extend(_summary_blocks(parsed.summary))
    if parsed.evidence:
        bullets = "\n".join(f"• {item}" for item in parsed.evidence)
        blocks.extend(_mrkdwn_section_blocks(f"*What I found*\n{bullets}"))
    if parsed.next_steps:
        bullets = "\n".join(f"• {item}" for item in parsed.next_steps)
        blocks.extend(_mrkdwn_section_blocks(f"*What to do next*\n{bullets}"))
    if not blocks:
        blocks.extend(_mrkdwn_section_blocks(normalize_slack_mrkdwn(answer)))
    blocks.append({"type": "divider"})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]})
    fallback = _fallback_text(parsed, answer, footer)
    return fallback, blocks


def _fallback_text(parsed: ParsedAnswer, raw: str, footer: str) -> str:
    parts: list[str] = []
    if parsed.summary:
        parts.append(parsed.summary)
    if parsed.evidence:
        parts.append("*What I found*\n" + "\n".join(f"• {item}" for item in parsed.evidence))
    if parsed.next_steps:
        parts.append("*What to do next*\n" + "\n".join(f"• {item}" for item in parsed.next_steps))
    body = "\n\n".join(parts) if parts else normalize_slack_mrkdwn(raw)
    return f"{body}\n\n{footer}"


def _is_section_header(line: str, name: str) -> bool:
    cleaned = re.sub(r"[*_`]", "", line).strip().rstrip(":").lower()
    return cleaned == name.lower()


def _strip_bullet(line: str) -> str:
    return re.sub(r"^[\s•\-*]+", "", line).strip()


def _summary_blocks(summary: str) -> list[dict[str, object]]:
    headline, _, body = summary.partition("\n")
    title = re.sub(r"\*([^*]+)\*", r"\1", headline).replace("`", "")
    for shortcode, emoji in _STATUS_EMOJI.items():
        title = title.replace(shortcode, emoji)
    if not title.strip() or len(title) > _HEADER_CHAR_LIMIT:
        return _mrkdwn_section_blocks(summary)
    blocks: list[dict[str, object]] = [
        {"type": "header", "text": {"type": "plain_text", "text": title, "emoji": True}}
    ]
    if body.strip():
        blocks.extend(_mrkdwn_section_blocks(body.strip()))
    return blocks


def _mrkdwn_section_blocks(text: str) -> list[dict[str, object]]:
    chunks = _chunk_text(text, _BLOCK_CHAR_LIMIT)
    return [{"type": "section", "text": {"type": "mrkdwn", "text": chunk}} for chunk in chunks]


def _chunk_text(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        slice_ = remaining[:limit]
        boundary = max(slice_.rfind("\n\n"), slice_.rfind("\n"), slice_.rfind(" "))
        if boundary <= 0:
            boundary = limit
        chunks.append(remaining[:boundary].rstrip())
        remaining = remaining[boundary:].lstrip()
    return chunks
