import json
import os
import uuid
from datetime import datetime, timezone
import discord
from discord.ext import commands

TOKEN = open("dctoken.txt").read()
BACKUP_ROOT = "backup"

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

def safe_name(name: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in " _-" else "_" for c in name)
    return (cleaned.strip() or "channel")[:80]

async def archive_channel(channel, guild_dir) -> tuple:
    channel_name = safe_name(getattr(channel, "name", str(channel.id)))
    channel_file = os.path.join(guild_dir, f"{channel_name}_{channel.id}.json")
    files_dir = os.path.join(guild_dir, "files")
    os.makedirs(files_dir, exist_ok=True)

    messages = []
    count = 0
    async for message in channel.history(limit=None, oldest_first=True):
        attachments = []
        for attachment in message.attachments:
            file_uuid = str(uuid.uuid4())
            disk_path = os.path.join(files_dir, f"{file_uuid}{os.path.splitext(attachment.filename)[1]}")
            await attachment.save(disk_path)
            attachments.append({"uuid": file_uuid, "original_filename": attachment.filename, "size": attachment.size, "content_type": attachment.content_type, "path": disk_path})

        messages.append({"message_id": str(message.id), "author": {"id": str(message.author.id), "name": str(message.author)}, "timestamp": message.created_at.isoformat(), "edited": message.edited_at.isoformat() if message.edited_at else None, "content": message.content, "jump_url": message.jump_url, "attachments": attachments})
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


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")


@bot.event
async def on_guild_join(guild):
    class FakeCtx:
        def __init__(self) -> None:
            self.guild = guild
            self.send = guild.text_channels[0].send
    
    await archive(FakeCtx())

@bot.command(name="archive")
@commands.has_permissions(administrator=True)
async def archive(ctx):
    await ctx.send("Starting archive...")
    guild = ctx.guild
    guild_dir = os.path.join(BACKUP_ROOT, f"{safe_name(guild.name)}_{guild.id}")
    os.makedirs(guild_dir, exist_ok=True)

    total_messages = 0
    channels_done = 0

    for channel in guild.text_channels:
        try:
            count, _ = await archive_channel(channel, guild_dir)
            total_messages += count
            channels_done += 1
        except:
            pass

    for channel in guild.text_channels:
        try:
            async for thread in channel.threads:
                count, _ = await archive_channel(thread, guild_dir)
                total_messages += count
                channels_done += 1
        except:
            pass

    await ctx.send(f"Archive complete! {channels_done} channels, {total_messages} messages.")


if __name__ == "__main__":
    bot.run(TOKEN)