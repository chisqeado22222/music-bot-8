import asyncio
import logging
import os
import shutil

import discord
from discord.ext import commands

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("musicbot")

ffmpeg_path = shutil.which("ffmpeg")
log.info(f"DIAGNOSTICO FFMPEG: {'encontrado en ' + ffmpeg_path if ffmpeg_path else 'NO ENCONTRADO en PATH'}")

deno_path = shutil.which("deno")
log.info(f"DIAGNOSTICO DENO: {'encontrado en ' + deno_path if deno_path else 'NO ENCONTRADO en PATH'}")

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix=["n", "#"], intents=intents, help_command=None)


@bot.event
async def on_ready():
    log.info(f"Conectado como {bot.user} ({bot.user.id})")


async def main():
    token = os.environ["DISCORD_TOKEN"]
    async with bot:
        await bot.load_extension("music")
        await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
