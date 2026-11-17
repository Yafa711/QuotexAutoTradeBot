"""Private Telegram signal intake. Stores parsed UTC-08 signals; never places trades."""
import hashlib
import logging
import os
import re
from datetime import datetime, time, timedelta, timezone

from telethon import TelegramClient, events
from telethon.sessions import StringSession

log = logging.getLogger("telegram-signal-bridge")
SOURCE_TZ = timezone(timedelta(hours=-8), name="UTC-08")

PAIR_RE = re.compile(r"\b([A-Z]{6}(?:-OTC)?)\b", re.I)
ASSET_RE = re.compile(r"(?m)^\s*[💳📊]\s*([A-Z0-9][A-Z0-9._/-]*)", re.I)
TIME_RE = re.compile(r"\b(\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM)?)\b", re.I)
DURATION_RE = re.compile(r"(?:⌛️?\s*)?(?:(\d{1,3})\s*(?:minutes?|mins?|m)\b|\bM(\d{1,3})\b)", re.I)

def parse_signal(raw):
    text = (raw or "").strip()
    if not text:
        return None
    parts = [p.strip() for p in text.split(";")]
    if len(parts) >= 4 and re.fullmatch(r"M\d+", parts[0], re.I):
        pair = PAIR_RE.fullmatch(parts[1])
        direction = parts[3].upper()
        direction = "PUT" if direction in {"PUT", "SELL", "DOWN"} else "CALL" if direction in {"CALL", "BUY", "UP"} else None
        duration = int(parts[0][1:])
        entry_text = parts[2]
        if not pair or not direction or not 1 <= duration <= 240:
            return None
    else:
        pair_match = ASSET_RE.search(text) or PAIR_RE.search(text)
        direction_match = re.search(r"\b(PUT|DOWN|SELL|CALL|UP|BUY)\b", text, re.I)
        duration_match = DURATION_RE.search(text)
        time_match = TIME_RE.search(text)
        if not (pair_match and direction_match and duration_match and time_match):
            return None
        pair = pair_match
        direction = "PUT" if direction_match.group(1).upper() in {"PUT", "SELL", "DOWN"} else "CALL"
        duration = int(duration_match.group(1) or duration_match.group(2))
        entry_text = time_match.group(1)
        if not 1 <= duration <= 240:
            return None
    parsed_time = None
    for fmt in ("%H:%M:%S", "%H:%M", "%I:%M:%S %p", "%I:%M %p"):
        try:
            parsed_time = datetime.strptime(entry_text.strip().upper(), fmt).time()
            break
        except ValueError:
            pass
    if parsed_time is None:
        return None
    return {"symbol": pair.group(1).upper(), "direction": direction,
            "duration_minutes": duration, "entry_time": parsed_time.strftime("%H:%M:%S")}

def resolve_entry(entry_time, now=None):
    now = (now or datetime.now(SOURCE_TZ)).astimezone(SOURCE_TZ)
    t = datetime.strptime(entry_time, "%H:%M:%S").time()
    candidate = datetime.combine(now.date(), t, tzinfo=SOURCE_TZ)
    if candidate < now:
        late = (now - candidate).total_seconds()
        if late <= 10:
            return candidate
        if late > 12 * 3600:
            candidate += timedelta(days=1)
        else:
            return None
    return candidate

async def start_signal_listener(collection):
    """Start listener only when a Telethon StringSession is configured."""
    session = os.getenv("TELEGRAM_SESSION_STRING", "").strip()
    target = os.getenv("TELEGRAM_TARGET_CHAT", "Eyad Trader Vip").strip()
    api_id = int(os.getenv("TELEGRAM_API_ID", "0") or "0")
    api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
    if not (session and api_id > 0 and api_hash and target):
        log.warning("Telegram signal intake disabled: configure TELEGRAM_SESSION_STRING, TELEGRAM_API_ID, TELEGRAM_API_HASH and TELEGRAM_TARGET_CHAT in host secrets.")
        return None
    client = TelegramClient(StringSession(session), api_id, api_hash)
    await client.start()
    if target.lstrip("-").isdigit():
        entity = await client.get_entity(int(target))
    else:
        try:
            entity = await client.get_entity(target)
        except Exception:
            found = [d.entity async for d in client.iter_dialogs() if d.name and d.name.casefold() == target.casefold()]
            if len(found) != 1:
                await client.disconnect()
                raise RuntimeError("Telegram target must resolve to exactly one joined chat.")
            entity = found[0]

    @client.on(events.NewMessage(chats=entity))
    async def on_signal(event):
        raw = event.raw_text or ""
        parsed = parse_signal(raw)
        if not parsed:
            return
        entry_at = resolve_entry(parsed["entry_time"])
        fingerprint = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        document = {
            **parsed, "fingerprint": fingerprint, "raw_text": raw,
            "source_chat_id": str(entity.id),
            "received_at_utc": datetime.now(timezone.utc),
            "entry_at_utc": entry_at.astimezone(timezone.utc) if entry_at else None,
            "status": "queued" if entry_at else "expired",
        }
        try:
            await collection.update_one({"fingerprint": fingerprint},
                {"$setOnInsert": document}, upsert=True)
            log.info("Telegram signal stored: %s %s %s", parsed["symbol"], parsed["direction"], document["status"])
        except Exception:
            log.exception("Failed to persist incoming Telegram signal.")

    log.info("Private Telegram signal listener connected to configured source.")
    return client
