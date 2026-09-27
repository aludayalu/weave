import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
import discord
import uvicorn
from discord.ext import commands
from fastapi import FastAPI

current_file = Path(__file__).resolve()
project_root = current_file.parents[1]
sys.path.insert(0, str(project_root))
import litedb

TOKEN = open("dctoken.txt").read().strip()
BACKUP_ROOT = "backup"
API_HOST = "127.0.0.1"
API_PORT = 1111

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
state_db = litedb.get_conn("state")
app = FastAPI()


def safe_name(name: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in " _-" else "_" for c in name)
    return (cleaned.strip() or "channel")[:80]


async def archive_channel(channel, guild_dir, start_from=None) -> tuple:
    channel_name = safe_name(getattr(channel, "name", str(channel.id)))
    channel_file = os.path.join(guild_dir, f"{channel_name}_{channel.id}.json")
    files_dir = os.path.join(guild_dir, "files")
    os.makedirs(files_dir, exist_ok=True)

    messages = []
    count = 0
    history_kwargs = {"limit": None, "oldest_first": True}
    if start_from is not None:
        history_kwargs["after"] = start_from
    async for message in channel.history(**history_kwargs):
        attachments = []
        for attachment in message.attachments:
            file_uuid = str(uuid.uuid4())
            disk_path = os.path.join(files_dir, f"{file_uuid}{os.path.splitext(attachment.filename)[1]}")
            await attachment.save(disk_path)
            attachments.append({
                "uuid": file_uuid,
                "original_filename": attachment.filename,
                "size": attachment.size,
                "content_type": attachment.content_type,
                "path": disk_path,
            })

        messages.append({
            "message_id": str(message.id),
            "author": {
                "id": str(message.author.id),
                "name": str(message.author),
            },
            "timestamp": message.created_at.isoformat(),
            "edited": message.edited_at.isoformat() if message.edited_at else None,
            "content": message.content,
            "jump_url": message.jump_url,
            "attachments": attachments,
        })
        count += 1

    record = {
        "channel": {
            "id": str(channel.id),
            "name": channel_name,
            "type": str(channel.type),
        },
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "message_count": count,
        "messages": messages,
    }

    with open(channel_file, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)

    return count, channel_file

async def archive(ctx, start_from=None):
    guild = ctx.guild
    guild_dir = os.path.join(BACKUP_ROOT, f"{safe_name(guild.name)}_{guild.id}")
    os.makedirs(guild_dir, exist_ok=True)

    total_messages = 0
    channels_done = 0

    for channel in guild.text_channels:
        try:
            count, _ = await archive_channel(channel, guild_dir, start_from)
            total_messages += count
            channels_done += 1
        except Exception as e:
            print(f"Failed {channel.name}: {e!r}")

    for channel in guild.text_channels:
        for thread in channel.threads:
            try:
                count, _ = await archive_channel(thread, guild_dir, start_from)
                total_messages += count
                channels_done += 1
            except Exception as e:
                print(f"Failed thread {thread.name}: {e!r}")

    for voice in guild.voice_channels:
        try:
            count, _ = await archive_channel(voice, guild_dir, start_from)
            total_messages += count
            channels_done += 1
        except Exception as e:
            print(f"Failed voice text {voice.name}: {e!r}")

    return [channels_done, total_messages]

@app.get("/guilds")
async def api_guilds():
    return {"guilds": [{"id": g.id, "name": g.name} for g in bot.guilds]}

@app.get("/archive_guild")
async def api_archive_guild(guild_id: int, start_from: str = None):
    guild = bot.get_guild(guild_id)
    if guild is None:
        return {"error": "guild not found"}

    start_dt = None
    if start_from:
        start_dt = datetime.fromisoformat(start_from).astimezone(timezone.utc)

    class FakeCtx:
        pass

    ctx = FakeCtx()
    ctx.guild = guild

    target = guild.system_channel or (guild.text_channels[0] if guild.text_channels else None)
    ctx.send = target.send if target else (lambda *a, **k: None)

    try:
        [channels_done, total_messages] = await archive(ctx, start_from=start_dt)
    except Exception as e:
        return {"error": f"archive failed: {e!r}"}

    return {"channels_done": channels_done, "total_messages": total_messages}

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    seen_guilds = list(state_db.get("guilds_seen", []))
    newly_seen = []

    for guild in bot.guilds:
        if guild.id in seen_guilds:
            continue

        target = guild.system_channel or (guild.text_channels[0] if guild.text_channels else None)
        send = target.send if target else (lambda *a, **k: None)

        class FakeCtx:
            pass

        ctx = FakeCtx()
        ctx.guild = guild

        print("Starting archive...")
        [channels_done, total_messages] = await archive(ctx, start_from=datetime(2015, 1, 1, tzinfo=timezone.utc))
        print(f"Archive complete! {channels_done} channels, {total_messages} messages.")
        newly_seen.append(guild.id)

    if newly_seen:
        seen_guilds.extend(newly_seen)
        state_db.set("guilds_seen", seen_guilds)


@bot.event
async def on_guild_join(guild):
    seen_guilds = list(state_db.get("guilds_seen", []))
    if guild.id in seen_guilds:
        return

    target = guild.system_channel or (guild.text_channels[0] if guild.text_channels else None)
    send = target.send if target else (lambda *a, **k: None)

    class FakeCtx:
        pass

    ctx = FakeCtx()
    ctx.guild = guild

    print("Starting archive...")
    [channels_done, total_messages] = await archive(ctx, start_from=datetime(2015, 1, 1, tzinfo=timezone.utc))
    print(f"Archive complete! {channels_done} channels, {total_messages} messages.")

    seen_guilds.append(guild.id)
    state_db.set("guilds_seen", seen_guilds)


async def main():
    config = uvicorn.Config(app, host=API_HOST, port=API_PORT, log_level="info")
    server = uvicorn.Server(config)
    await asyncio.gather(server.serve(), bot.start(TOKEN))


if __name__ == "__main__":
    asyncio.run(main())