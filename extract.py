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
import asyncio
import urllib.parse
import urllib.request
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


def _strip_custom_emoji(value: str) -> str:
    """Remove Discord custom emoji markup without changing the label."""
    return re.sub(r"<a?:[A-Za-z0-9_]+:\d+>", "", str(value or ""))


def _normalise_label(value: str) -> str:
    value = _strip_custom_emoji(value).strip()
    value = re.sub(r"^[^\w]+", "", value, flags=re.UNICODE)
    value = value.replace("**", "").replace("__", "")
    value = value.strip().rstrip(":").strip()
    return re.sub(r"[\s_]+", " ", value).casefold()


def _clean_username(value: str) -> str:
    value = _strip_custom_emoji(value).strip()
    value = re.sub(r"^[^\w]+", "", value, flags=re.UNICODE)
    return value.strip().strip("*_")[:MAX_TEXT_VALUE]


def _extract_allowed_fields(message_text: str, embeds: list[dict[str, Any]]) -> dict[str, str]:
    """Extract only approved labels; username may be the embed title."""
    result: dict[str, str] = {}
    for embed in embeds:
        title = embed.get("title")
        if isinstance(title, str) and title.strip() and "Username" not in result:
            username = _clean_username(title)
            if username:
                result["Username"] = username

        for item in embed.get("fields") or []:
            if not isinstance(item, dict):
                continue
            canonical = _ALLOWED_LABELS.get(_normalise_label(item.get("name", "")))
            value = str(item.get("value") or "").strip()
            if canonical and value and canonical not in result:
                result[canonical] = value[:MAX_TEXT_VALUE]

        description = embed.get("description")
        if isinstance(description, str):
            for line in description.splitlines():
                _extract_text_line(line, result)

    for line in (message_text or "").splitlines():
        _extract_text_line(line, result)
    return result


def _extract_text_line(line: str, result: dict[str, str]) -> None:
    """Read a plain-text label line, allowing Unicode/custom emoji prefixes."""
    if ":" not in line:
        return
    raw_label, value = line.split(":", 1)
    canonical = _ALLOWED_LABELS.get(_normalise_label(raw_label))
    value = value.strip().strip("•|")
    if canonical and value and canonical not in result:
        result[canonical] = value[:MAX_TEXT_VALUE]


def _token_link(value: str) -> tuple[Optional[str], Optional[str]]:
    """Return (filename, URL) from a Markdown or raw Discord CDN link."""
    value = str(value or "").strip()
    markdown = re.search(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", value)
    if markdown:
        filename = markdown.group(1).strip().strip("_")
        url = markdown.group(2).rstrip(".,>")
    else:
        match = re.search(r"https?://[^\s<>()]+", value)
        if not match:
            name = re.search(r"([A-Za-z0-9_. -]+\.txt)\b", value, re.IGNORECASE)
            return (name.group(1).strip() if name else None), None
        url = match.group(0).rstrip(".,>")
        filename = urllib.parse.unquote(urllib.parse.urlparse(url).path.rsplit("/", 1)[-1])
    filename = filename.rsplit("/", 1)[-1].strip()
    return (filename[:180] if filename else None), url


def _download_discord_file(url: str, max_bytes: int) -> tuple[bytes, Optional[str]]:
    """Download a linked Discord attachment with host and size checks."""
    allowed_hosts = {"cdn.discordapp.com", "media.discordapp.net"}
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in allowed_hosts:
        raise ExtractError("The Token File link is not a supported Discord CDN link.")
    request = urllib.request.Request(url, headers={"User-Agent": "TriggerRewards-StockImporter/1.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        final = urllib.parse.urlparse(response.geturl())
        if final.scheme != "https" or (final.hostname or "").lower() not in allowed_hosts:
            raise ExtractError("The Token File link redirected outside Discord's CDN.")
        data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ExtractError("The linked Token File is larger than the import size limit.")
        return data, response.headers.get("Content-Type")


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

    token_filename, token_url = _token_link(fields.get("Token File", ""))
    if token_filename:
        fields["Token File"] = token_filename

    for attachment in source_attachments:
        filename = str(getattr(attachment, "filename", "attachment"))[:180]
        if not token_filename or filename.casefold() != token_filename.casefold():
            continue
        size = int(getattr(attachment, "size", 0) or 0)
        if size > max_attachment_bytes:
            warnings.append(f"Skipped {filename}: larger than {max_attachment_bytes // (1024 * 1024)} MB limit.")
            continue
        if total + size > max_total_bytes:
            warnings.append(f"Skipped {filename}: total attachment limit reached.")
            continue
        try:
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

    # The Token File in this embed is a clickable Discord CDN link, not
    # necessarily a normal message attachment. Download it into stock.
    if token_url and token_filename and not any(
        item.filename.casefold() == token_filename.casefold() for item in attachments
    ):
        try:
            remaining = min(max_attachment_bytes, max_total_bytes - total)
            if remaining <= 0:
                raise ExtractError("The total attachment import size limit was reached.")
            data, content_type = await asyncio.to_thread(
                _download_discord_file, token_url, remaining
            )
            attachments.append(ExtractedAttachment(
                filename=token_filename,
                content_type=content_type or "text/plain",
                size=len(data),
                data=data,
            ))
            total += len(data)
        except Exception as exc:
            warnings.append(f"Could not download Token File {token_filename}: {str(exc)[:180]}")


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
