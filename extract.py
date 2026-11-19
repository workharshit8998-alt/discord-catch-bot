"""Trigger Rewards embed/reward importer.

This module only extracts data from a Discord message in the current server.
It does not write to SQLite or deliver rewards; main.py should preview the
returned payload and then pass its fields into the existing stock workflow.

Requires discord.py 2.x and a bot with View Channel + Read Message History.
For message content, enable Message Content Intent in the Developer Portal
and in the bot intents.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

_MESSAGE_LINK = re.compile(
    r"https?://(?:www\.)?discord(?:app)?\.com/channels/(?P<guild>\d{17,20})/(?P<channel>\d{17,20})/(?P<message>\d{17,20})/?(?:\?.*)?$",
    re.IGNORECASE,
)

DEFAULT_MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 24 * 1024 * 1024
MAX_TEXT_VALUE = 3500


class ExtractError(ValueError):
    """Raised when a message cannot safely be imported."""


@dataclass
class ExtractedAttachment:
    filename: str
    content_type: Optional[str]
    size: int
    data: bytes


@dataclass
class ExtractedReward:
    source_message_id: int
    source_channel_id: int
    source_guild_id: int
    author: str
    content: str
    embeds: list[dict[str, Any]] = field(default_factory=list)
    fields: dict[str, str] = field(default_factory=dict)
    attachments: list[ExtractedAttachment] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def stock_fields(self) -> dict[str, str]:
        """Return only the six approved reward fields for stock storage."""
        allowed = ("Username", "Email", "Password", "Type", "Capes", "Token File")
        return {key: self.fields[key] for key in allowed if self.fields.get(key)}


def parse_message_link(link: str) -> tuple[int, int, int]:
    """Parse a Discord message link into (guild_id, channel_id, message_id)."""
    match = _MESSAGE_LINK.fullmatch((link or "").strip())
    if not match:
        raise ExtractError("That is not a valid Discord message link. Use Copy Message Link.")
    return tuple(int(match.group(k)) for k in ("guild", "channel", "message"))


def _embed_to_dict(embed: Any) -> dict[str, Any]:
    try:
        data = embed.to_dict()
    except Exception:
        data = {}
    return data if isinstance(data, dict) else {}


def _message_text(message: Any) -> str:
    pieces: list[str] = []
    content = (getattr(message, "content", None) or "").strip()
    if content:
        pieces.append(content)
    # Discord forward snapshots differ by discord.py version. Read them
    # defensively when exposed by the installed library.
    snapshots = getattr(message, "message_snapshots", None) or getattr(message, "snapshots", None) or []
    for snapshot in snapshots:
        original = getattr(snapshot, "message", snapshot)
        snap_content = (getattr(original, "content", None) or "").strip()
        if snap_content and snap_content not in pieces:
            pieces.append(snap_content)
    return "\n\n".join(pieces)[:MAX_TEXT_VALUE]


def _collect_embeds(message: Any) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    for embed in getattr(message, "embeds", None) or []:
        data = _embed_to_dict(embed)
        if data:
            collected.append(data)
    snapshots = getattr(message, "message_snapshots", None) or getattr(message, "snapshots", None) or []
    for snapshot in snapshots:
        original = getattr(snapshot, "message", snapshot)
        for embed in getattr(original, "embeds", None) or []:
            data = _embed_to_dict(embed)
            if data and data not in collected:
                collected.append(data)
    return collected[:10]


def _flatten_embed_fields(embeds: list[dict[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for index, embed in enumerate(embeds, start=1):
        prefix = f"Embed {index} " if len(embeds) > 1 else ""
        for key in ("title", "description"):
            value = embed.get(key)
            if isinstance(value, str) and value.strip():
                label = "Title" if key == "title" else "Description"
                result[(prefix + label)[:60]] = value.strip()[:MAX_TEXT_VALUE]
        author = embed.get("author") or {}
        if isinstance(author, dict) and author.get("name"):
            result[(prefix + "Author")[:60]] = str(author["name"])[:MAX_TEXT_VALUE]
        for item in embed.get("fields") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "Field")[:60]
            value = str(item.get("value") or "").strip()
            if value:
                label = (prefix + name)[:60]
                # Avoid overwriting duplicate field labels.
                suffix = 2
                base = label
                while label in result:
                    label = f"{base[:54]} #{suffix}"
                    suffix += 1
                result[label] = value[:MAX_TEXT_VALUE]
        footer = embed.get("footer") or {}
        if isinstance(footer, dict) and footer.get("text"):
            result[(prefix + "Footer")[:60]] = str(footer["text"])[:MAX_TEXT_VALUE]
    return result


_ALLOWED_LABELS = {
    "username": "Username", "email": "Email", "e-mail": "Email",
    "password": "Password", "type": "Type", "capes": "Capes",
    "token file": "Token File", "token_file": "Token File",
}

def _extract_allowed_fields(message_text: str, embeds: list[dict[str, Any]]) -> dict[str, str]:
    """Extract only the six approved labels; ignore all other embed data."""
    result: dict[str, str] = {}
    sources = [message_text or ""]
    for embed in embeds:
        for key in ("title", "description"):
            value = embed.get(key)
            if isinstance(value, str):
                sources.append(value)
        for item in embed.get("fields") or []:
            if not isinstance(item, dict):
                continue
            label = str(item.get("name") or "").strip().rstrip(":").casefold()
            canonical = _ALLOWED_LABELS.get(label)
            value = str(item.get("value") or "").strip()
            if canonical and value and canonical not in result:
                result[canonical] = value[:MAX_TEXT_VALUE]

    labels = r"Username|Email|E-mail|Password|Type|Capes|Token[ _]File"
    line_pattern = re.compile(rf"^\s*[^\w]*({labels})\s*:\s*(.*?)\s*$", re.IGNORECASE)
    for source in sources:
        for line in source.splitlines():
            match = line_pattern.match(line)
            if not match:
                continue
            canonical = _ALLOWED_LABELS.get(match.group(1).strip().casefold())
            value = match.group(2).strip().strip("•|")
            if canonical and value and canonical not in result:
                result[canonical] = value[:MAX_TEXT_VALUE]
    return result


async def extract_reward_from_message_link(
    bot: Any,
    guild: Any,
    message_link: str,
    *,
    allowed_channel_id: Optional[int] = None,
    max_attachment_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
) -> ExtractedReward:
    """Fetch and extract a forwarded message from a channel in this guild.

    The link must point to the current guild. If allowed_channel_id is set,
    only that import channel is accepted. Attachment bytes are fetched now,
    because Discord CDN URLs may expire; caller should persist them before save.
    """
    guild_id, channel_id, message_id = parse_message_link(message_link)
    if guild is None or int(guild.id) != guild_id:
        raise ExtractError("Import only works with a message forwarded into this server.")
    if allowed_channel_id is not None and channel_id != int(allowed_channel_id):
        raise ExtractError("That message is not in the configured reward-import channel.")

    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(channel_id)
        except Exception as exc:
            raise ExtractError("I cannot access that channel. Check View Channel permission.") from exc
    if not hasattr(channel, "fetch_message"):
        raise ExtractError("The link must point to a text channel or thread message.")
    try:
        message = await channel.fetch_message(message_id)
    except Exception as exc:
        raise ExtractError("I could not read that message. Check Read Message History permission.") from exc

    content = _message_text(message)
    embeds = _collect_embeds(message)
    fields = _extract_allowed_fields(content, embeds)
    attachments: list[ExtractedAttachment] = []
    warnings: list[str] = []
    total = 0
    source_attachments = list(getattr(message, "attachments", None) or [])
    snapshots = getattr(message, "message_snapshots", None) or getattr(message, "snapshots", None) or []
    for snapshot in snapshots:
        original = getattr(snapshot, "message", snapshot)
        for attachment in getattr(original, "attachments", None) or []:
            if all(getattr(x, "id", None) != getattr(attachment, "id", None) for x in source_attachments):
                source_attachments.append(attachment)

    token_filename = fields.get("Token File", "").strip().replace("\\\\", "/").split("/")[-1].casefold()
    for attachment in source_attachments:
        filename = str(getattr(attachment, "filename", "attachment"))[:180]
        if not token_filename or filename.casefold() != token_filename:
            continue
        size = int(getattr(attachment, "size", 0) or 0)
        if size > max_attachment_bytes:
            warnings.append(f"Skipped {filename}: larger than {max_attachment_bytes // (1024 * 1024)} MB limit.")
            continue
        if total + size > max_total_bytes:
            warnings.append(f"Skipped {filename}: total attachment limit reached.")
            continue
        try:
            data = await attachment.read(use_cached=True)
        except TypeError:
            data = await attachment.read()
        except Exception:
            warnings.append(f"Could not download {filename}; check attachment access.")
            continue
        if not isinstance(data, (bytes, bytearray)):
            warnings.append(f"Skipped {filename}: attachment data was unavailable.")
            continue
        data = bytes(data)
        if len(data) > max_attachment_bytes or total + len(data) > max_total_bytes:
            warnings.append(f"Skipped {filename}: downloaded size exceeded the limit.")
            continue
        attachments.append(ExtractedAttachment(
            filename=filename,
            content_type=getattr(attachment, "content_type", None),
            size=len(data),
            data=data,
        ))
        total += len(data)

    if not fields and not attachments:
        raise ExtractError("No supported reward fields were found. Expected Username, Email, Password, Type, Capes and Token File.")

    author = getattr(getattr(message, "author", None), "display_name", None) or "Unknown"
    return ExtractedReward(
        source_message_id=message_id,
        source_channel_id=channel_id,
        source_guild_id=guild_id,
        author=str(author)[:100],
        content=content,
        embeds=embeds,
        fields=fields,
        attachments=attachments,
        warnings=warnings,
    )
