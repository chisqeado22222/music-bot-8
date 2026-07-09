import asyncio
import os
import time
from collections import deque

import discord
import yt_dlp
from discord.ext import commands

YDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch1",
    "source_address": "0.0.0.0",
    "socket_timeout": 10,
    "extractor_args": {"youtube": {"player_client": ["android_vr"]}},
}

_cookies_env = os.environ.get("YTDLP_COOKIES")
if _cookies_env:
    _cookies_path = "/tmp/cookies.txt"
    with open(_cookies_path, "w") as f:
        f.write(_cookies_env)
    YDL_OPTS["cookiefile"] = _cookies_path

FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

PLAYLIST_LIMIT = 200
PROGRESS_UPDATE_SECONDS = 1
BOT_OWNER_ID = int(os.environ["BOT_OWNER_ID"]) if os.environ.get("BOT_OWNER_ID") else None
FARM_DEFAULT_URL = os.environ.get("FARM_DEFAULT_URL")
REPORT_OWNER_ID = int(os.environ.get("REPORT_OWNER_ID", "1240589249829404695"))


def format_duration(seconds: int) -> str:
    seconds = int(seconds or 0)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def progress_bar(elapsed: int, total: int, length: int = 20) -> str:
    if not total:
        return "─" * length
    exact = length * min(elapsed / total, 1)
    full = int(exact)
    if full >= length:
        return "█" * length
    partial_chars = " ▏▎▍▌▋▊▉█"
    partial = partial_chars[int((exact - full) * 8)]
    return "█" * full + partial + "─" * (length - full - 1)


class Track:
    def __init__(self, title, webpage_url, thumbnail, duration, stream_url=None, requester=None):
        self.title = title
        self.webpage_url = webpage_url
        self.thumbnail = thumbnail
        self.duration = duration
        self.stream_url = stream_url
        self.requester = requester


def _search_with(opts: dict, query: str):
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(query, download=False)
    if "entries" in info:
        entries = list(info["entries"])
        if not entries:
            return None
        info = entries[0]
    return info


def extract_track(query: str) -> Track:
    is_url = query.startswith("http://") or query.startswith("https://")

    info = None
    try:
        info = _search_with(YDL_OPTS, query)
    except yt_dlp.utils.DownloadError:
        info = None

    if info is None and not is_url:
        soundcloud_opts = dict(YDL_OPTS)
        soundcloud_opts["default_search"] = "scsearch1"
        try:
            info = _search_with(soundcloud_opts, query)
        except yt_dlp.utils.DownloadError:
            info = None

    if info is None:
        raise yt_dlp.utils.DownloadError("No se encontró en YouTube ni en SoundCloud.")

    return Track(
        title=info.get("title", "Desconocido"),
        webpage_url=info.get("webpage_url", query),
        thumbnail=info.get("thumbnail"),
        duration=info.get("duration", 0),
        stream_url=info["url"],
    )


def extract_playlist(url: str) -> list[Track]:
    opts = dict(YDL_OPTS)
    opts.update({"noplaylist": False, "extract_flat": True, "playlistend": PLAYLIST_LIMIT})
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    tracks = []
    for entry in (info.get("entries") or [])[:PLAYLIST_LIMIT]:
        if entry is None:
            continue
        video_id = entry.get("id")
        webpage_url = f"https://www.youtube.com/watch?v={video_id}" if video_id else entry.get("url")
        tracks.append(
            Track(
                title=entry.get("title", "Desconocido"),
                webpage_url=webpage_url,
                thumbnail=entry.get("thumbnail"),
                duration=entry.get("duration", 0),
            )
        )
    return tracks


def resolve_stream(track: Track):
    with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
        info = ydl.extract_info(track.webpage_url, download=False)
        track.stream_url = info["url"]
        track.thumbnail = track.thumbnail or info.get("thumbnail")
        track.duration = track.duration or info.get("duration", 0)


class GuildPlayer:
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.queue: deque[Track] = deque()
        self.history: list[Track] = []
        self.current: Track | None = None
        self.next_override: Track | None = None
        self.voice_client: discord.VoiceClient | None = None
        self.now_playing_message: discord.Message | None = None
        self.started_at: float | None = None
        self.paused_at: float | None = None
        self.paused_total: float = 0.0
        self.update_task: asyncio.Task | None = None
        self.idle_task: asyncio.Task | None = None
        self.locked: bool = False
        self.locked_channel_id: int | None = None
        self.locked_recovering: bool = False
        self.loop_current: bool = False
        self.farm_channel_id: int | None = None

    def elapsed(self) -> int:
        if self.started_at is None:
            return 0
        end = self.paused_at if self.paused_at is not None else time.monotonic()
        return max(int(end - self.started_at - self.paused_total), 0)

    def start_idle_timer(self):
        if self.idle_task is None:
            self.idle_task = asyncio.create_task(self._idle_disconnect())

    def cancel_idle_timer(self):
        if self.idle_task:
            self.idle_task.cancel()
            self.idle_task = None

    async def _start(self, track: Track):
        self.cancel_idle_timer()

        if track.stream_url is None:
            await asyncio.to_thread(resolve_stream, track)
        self.current = track
        self.started_at = time.monotonic()
        self.paused_at = None
        self.paused_total = 0.0

        source = discord.FFmpegPCMAudio(track.stream_url, **FFMPEG_OPTS)

        def after(error):
            if not self.locked_recovering:
                asyncio.run_coroutine_threadsafe(self._on_track_end(), self.bot.loop)

        self.voice_client.play(source, after=after, bitrate=384, signal_type="music")
        embed, view = self.build_now_playing(track)
        try:
            self.now_playing_message = await self.voice_client.channel.send(embed=embed, view=view)
        except discord.HTTPException:
            self.now_playing_message = None
        self.update_task = asyncio.create_task(self.progress_loop())

    async def _on_track_end(self):
        if self.locked_recovering:
            return
        if self.update_task:
            self.update_task.cancel()
            self.update_task = None

        if self.next_override is not None:
            track = self.next_override
            self.next_override = None
            await self._start(track)
            return

        if self.loop_current and self.current is not None:
            track = self.current
            track.stream_url = None  # forzar re-resolución: el link de YouTube puede expirar tras horas
            await self._start(track)
            return

        if self.current:
            self.history.append(self.current)
        if not self.queue:
            self.current = None
            self.start_idle_timer()
            return
        track = self.queue.popleft()
        await self._start(track)

    async def play_next(self):
        await self._on_track_end()

    async def go_previous(self) -> bool:
        if not self.history:
            return False
        if self.current:
            self.queue.appendleft(self.current)
        self.next_override = self.history.pop()
        if self.voice_client and (self.voice_client.is_playing() or self.voice_client.is_paused()):
            self.voice_client.stop()
        else:
            await self._on_track_end()
        return True

    async def _idle_disconnect(self):
        try:
            await asyncio.sleep(24 * 60 * 60)
        except asyncio.CancelledError:
            return
        await self.stop_all()

    async def stop_all(self):
        if self.update_task:
            self.update_task.cancel()
            self.update_task = None
        if self.idle_task:
            self.idle_task.cancel()
            self.idle_task = None
        self.queue.clear()
        self.history.clear()
        self.current = None
        self.next_override = None
        self.loop_current = False
        self.farm_channel_id = None
        if self.voice_client:
            self.voice_client.stop()
            await self.voice_client.disconnect()
            self.voice_client = None

    async def progress_loop(self):
        try:
            while self.voice_client and (self.voice_client.is_playing() or self.voice_client.is_paused()):
                await asyncio.sleep(PROGRESS_UPDATE_SECONDS)
                if self.now_playing_message and self.current:
                    embed, _ = self.build_now_playing(self.current)
                    try:
                        await self.now_playing_message.edit(embed=embed)
                    except discord.HTTPException:
                        pass
        except asyncio.CancelledError:
            pass

    def build_now_playing(self, track: Track):
        embed = discord.Embed(title=track.title, url=track.webpage_url, color=discord.Color.blurple())
        embed.set_author(name="🎶 Reproduciendo ahora", icon_url=self.bot.user.display_avatar.url)
        if track.thumbnail:
            embed.set_thumbnail(url=track.thumbnail)
        if track.requester:
            embed.add_field(name="Pedido por", value=track.requester.mention, inline=True)
        bar = progress_bar(self.elapsed(), track.duration)
        embed.add_field(
            name="Duración",
            value=f"{bar}\n`{format_duration(self.elapsed())} / {format_duration(track.duration)}`",
            inline=False,
        )
        if self.queue:
            embed.set_footer(text=f"{len(self.queue)} canción(es) en cola")
        return embed, NowPlayingView(self)


class MoveConfirmView(discord.ui.View):
    def __init__(self, author: discord.Member):
        super().__init__(timeout=30)
        self.author = author
        self.result: asyncio.Future = asyncio.get_event_loop().create_future()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message(
                "❌ Solo quien usó el comando puede decidir esto.", ephemeral=True
            )
            return False
        return True

    def _disable_all(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="Mover de todos modos", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._disable_all()
        await interaction.response.edit_message(content="✅ Moviendo al nuevo canal...", embed=None, view=self)
        if not self.result.done():
            self.result.set_result(True)

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.danger, emoji="❌")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._disable_all()
        await interaction.response.edit_message(content="❌ Cancelado, no me moví.", embed=None, view=self)
        if not self.result.done():
            self.result.set_result(False)

    async def on_timeout(self):
        if not self.result.done():
            self.result.set_result(False)


async def ensure_voice_connection(ctx: commands.Context, player: "GuildPlayer") -> bool:
    """Conecta o mueve el bot al canal del autor. Devuelve False si no se debe continuar."""
    target_channel = ctx.author.voice.channel

    if player.voice_client is None:
        player.voice_client = await target_channel.connect()
        return True

    if player.voice_client.channel.id == target_channel.id:
        return True

    current_channel = player.voice_client.channel

    if player.locked:
        await ctx.send(
            f"🔒 Estoy fijado (lock) en **{current_channel.name}** y no me puedo mover. "
            "Pídele al owner del bot que quite el modo lock ahí primero."
        )
        return False

    if player.loop_current:
        await ctx.send(
            f"🌾 Estoy en modo farm en **{current_channel.name}** y no me puedo mover. "
            "Pídele al owner del bot que apague el modo farm ahí primero."
        )
        return False

    embed = discord.Embed(
        title="⚠️ Ya estoy en otro canal",
        description=(
            f"Estoy en **{current_channel.name}**. ¿Quieres que me mueva a **{target_channel.name}**?"
        ),
        color=discord.Color.orange(),
    )
    view = MoveConfirmView(ctx.author)
    msg = await ctx.send(embed=embed, view=view)
    confirmed = await view.result
    try:
        await msg.delete()
    except discord.HTTPException:
        pass

    if not confirmed:
        return False

    await player.voice_client.move_to(target_channel)
    return True


class NowPlayingView(discord.ui.View):
    def __init__(self, player: GuildPlayer):
        super().__init__(timeout=None)
        self.player = player

    async def _in_voice(self, interaction: discord.Interaction) -> bool:
        vc = self.player.voice_client
        if not vc or not interaction.user.voice or interaction.user.voice.channel != vc.channel:
            await interaction.response.send_message("❌ Debes estar en el canal de voz del bot.", ephemeral=True)
            return False
        return True

    @discord.ui.button(emoji="⏮", label="Anterior", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._in_voice(interaction):
            return
        ok = await self.player.go_previous()
        msg = "⏮ Reproduciendo anterior." if ok else "❌ No hay canción anterior."
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(emoji="⏯", label="Pausar / Reanudar", style=discord.ButtonStyle.primary)
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._in_voice(interaction):
            return
        vc = self.player.voice_client
        if vc.is_playing():
            vc.pause()
            self.player.paused_at = time.monotonic()
            await interaction.response.send_message("⏸ Pausado.", ephemeral=True)
        elif vc.is_paused():
            self.player.paused_total += time.monotonic() - self.player.paused_at
            self.player.paused_at = None
            vc.resume()
            await interaction.response.send_message("▶ Reanudado.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ No hay nada sonando.", ephemeral=True)

    @discord.ui.button(emoji="⏭", label="Saltar", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._in_voice(interaction):
            return
        if not (self.player.voice_client.is_playing() or self.player.voice_client.is_paused()):
            await interaction.response.send_message("❌ No hay nada sonando.", ephemeral=True)
            return
        self.player.voice_client.stop()
        await interaction.response.send_message("⏭ Saltando...", ephemeral=True)

    @discord.ui.button(emoji="⏹", label="Detener", style=discord.ButtonStyle.danger)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._in_voice(interaction):
            return
        await self.player.stop_all()
        await interaction.response.send_message("⏹ Detenido y desconectado.", ephemeral=True)


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.players: dict[int, GuildPlayer] = {}

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.id == self.bot.user.id:
            await self._enforce_protected_channel(member.guild, before, after)
            return
        if member.bot:
            return
        player = self.players.get(member.guild.id)
        if not player or not player.voice_client:
            return
        vc_channel = player.voice_client.channel
        if after.channel == vc_channel:
            player.cancel_idle_timer()
        elif before.channel == vc_channel and after.channel != vc_channel:
            if not any(not m.bot for m in vc_channel.members):
                player.start_idle_timer()

    async def _find_recent_culprit(self, guild: discord.Guild, action, max_age_seconds: float = 15.0):
        """Busca en el audit log una entrada RECIENTE cuyo objetivo sea este bot. Devuelve None si no hay certeza."""
        try:
            async for entry in guild.audit_logs(limit=10, action=action):
                if entry.target is None or entry.target.id != self.bot.user.id:
                    continue
                age = (discord.utils.utcnow() - entry.created_at).total_seconds()
                if age > max_age_seconds:
                    return None  # las entradas vienen de más nueva a más vieja; si ya es vieja, no hay match confiable
                return entry.user
        except discord.Forbidden:
            return None
        return None

    async def _report_disconnect(self, guild: discord.Guild, mode: str, channel_id: int, culprit):
        try:
            owner = await self.bot.fetch_user(REPORT_OWNER_ID)
        except discord.HTTPException:
            return
        channel = guild.get_channel(channel_id)
        channel_name = channel.name if channel else "canal desconocido"
        mode_label = "🔒 lock" if mode == "lock" else "🌾 farm"
        if culprit is not None:
            who = f"**{culprit}** (`{culprit.id}`)"
        else:
            who = "No pude confirmar con certeza quién fue (sin permiso de ver audit log, o no hubo una entrada reciente que apuntara a mí)."
        embed = discord.Embed(
            title="⚠️ Me desconectaron/movieron estando protegido",
            description=(
                f"Servidor: **{guild.name}**\n"
                f"Canal: **{channel_name}**\n"
                f"Modo: {mode_label}\n"
                f"Responsable: {who}"
            ),
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )
        try:
            await owner.send(embed=embed)
        except discord.HTTPException:
            pass

    async def _enforce_protected_channel(self, guild: discord.Guild, before, after):
        player = self.players.get(guild.id)
        if not player:
            return

        if player.locked and before.channel is not None and before.channel.id == player.locked_channel_id:
            protected_channel_id = player.locked_channel_id
            mode = "lock"
        elif (
            player.loop_current
            and player.farm_channel_id
            and before.channel is not None
            and before.channel.id == player.farm_channel_id
        ):
            protected_channel_id = player.farm_channel_id
            mode = "farm"
        else:
            return

        if after.channel is not None and after.channel.id == protected_channel_id:
            return

        player.locked_recovering = True

        await asyncio.sleep(1.5)  # dar tiempo a que la entrada aparezca en el audit log
        action = discord.AuditLogAction.member_disconnect if after.channel is None else discord.AuditLogAction.member_move
        culprit = await self._find_recent_culprit(guild, action)

        reason_text = (
            "Desconectó/movió al bot de música estando fijado (lock) a un canal."
            if mode == "lock"
            else "Desconectó/movió al bot de música estando en modo farm en un canal."
        )

        if culprit is not None:
            try:
                await guild.kick(culprit, reason=reason_text)
            except discord.Forbidden:
                pass

        await self._report_disconnect(guild, mode, protected_channel_id, culprit)

        channel = guild.get_channel(protected_channel_id)
        if channel is not None:
            player.voice_client = await channel.connect()
            player.locked_recovering = False
            if mode == "farm" and player.current is not None:
                track = player.current
                track.stream_url = None
                await player._start(track)
            elif player.current:
                player.queue.appendleft(player.current)
                player.current = None
                await player.play_next()
        else:
            player.locked_recovering = False

    @commands.command(name="lock")
    async def lock(self, ctx: commands.Context):
        if ctx.author.id != ctx.guild.owner_id and ctx.author.id != BOT_OWNER_ID:
            await ctx.send("❌ Solo el dueño del servidor o el dueño del bot pueden usar este comando.")
            return

        player = self.get_player(ctx.guild.id)
        if player.locked:
            player.locked = False
            player.locked_channel_id = None
            await ctx.send("🔓 Modo fijo desactivado.")
            return

        if player.voice_client is None:
            await ctx.send("❌ El bot debe estar conectado a un canal de voz para fijarlo.")
            return

        player.locked = True
        player.locked_channel_id = player.voice_client.channel.id
        await ctx.send(
            f"🔒 Bot fijado al canal **{player.voice_client.channel.name}**. "
            "Si alguien lo desconecta o lo mueve de ahí, será expulsado del servidor."
        )

    def get_player(self, guild_id: int) -> GuildPlayer:
        if guild_id not in self.players:
            self.players[guild_id] = GuildPlayer(self.bot)
        return self.players[guild_id]

    def _prefixes_display(self) -> str:
        prefix = self.bot.command_prefix
        if isinstance(prefix, (list, tuple)):
            return ", ".join(prefix)
        return str(prefix)

    def _status_line(self, player: "GuildPlayer") -> str:
        if player.voice_client is None:
            channel_desc = "no estoy en ningún canal de voz"
        else:
            channel_desc = f"estoy en **{player.voice_client.channel.name}**"
            if player.locked:
                channel_desc += " (🔒 lock)"
            elif player.loop_current:
                channel_desc += " (🌾 farm)"
        return f"{channel_desc}. Mis prefijos son: `{self._prefixes_display()}`."

    @commands.command(name="sts")
    async def status(self, ctx: commands.Context):
        mentioned_bots = [m for m in ctx.message.mentions if m.bot]
        if mentioned_bots and ctx.guild.me not in mentioned_bots:
            return  # se mencionaron bots específicos y este no es uno de ellos

        player = self.get_player(ctx.guild.id)
        service_name = (
            os.environ.get("RAILWAY_SERVICE_NAME")
            or os.environ.get("RAILWAY_GIT_REPO_NAME")
            or "desconocido (no está en Railway o falta la variable)"
        )
        embed = discord.Embed(title=f"🤖 Estado de {ctx.guild.me.display_name}", color=discord.Color.blurple())
        embed.add_field(name="Servicio en Railway", value=service_name, inline=False)
        embed.add_field(name="Canal / modo", value=self._status_line(player), inline=False)
        await ctx.send(embed=embed)

    @commands.command(name="play")
    async def play(self, ctx: commands.Context, *, query: str):
        if ctx.author.voice is None or ctx.author.voice.channel is None:
            await ctx.send("❌ Debes estar en un canal de voz para usar este comando.")
            return

        player = self.get_player(ctx.guild.id)
        if not await ensure_voice_connection(ctx, player):
            return

        is_playlist = "list=" in query

        async with ctx.typing():
            try:
                if is_playlist:
                    tracks = await asyncio.to_thread(extract_playlist, query)
                    if not tracks:
                        await ctx.send("❌ No pude leer esa playlist.")
                        return
                    for track in tracks:
                        track.requester = ctx.author
                    player.queue.extend(tracks)
                    await ctx.send(f"✅ Se agregaron **{len(tracks)}** canciones de la playlist a la cola.")
                else:
                    track = await asyncio.to_thread(extract_track, query)
                    track.requester = ctx.author
                    player.queue.append(track)
                    await ctx.send(f"✅ Agregado a la cola: **{track.title}**")
            except yt_dlp.utils.DownloadError:
                await ctx.send("❌ No pude encontrar o reproducir eso. Revisa el link o el nombre.")
                return

        if player.current is None:
            await player.play_next()

    @commands.command(name="farm", aliases=["join"])
    async def farm(self, ctx: commands.Context, *, query: str = None):
        mentioned_bots = [m for m in ctx.message.mentions if m.bot]
        if mentioned_bots:
            if ctx.guild.me not in mentioned_bots:
                return  # este bot no fue mencionado, ignorar
            for m in ctx.message.mentions:
                query = (query or "").replace(m.mention, "").replace(f"<@!{m.id}>", "").replace(f"<@{m.id}>", "")
            query = query.strip() or None

            player = self.get_player(ctx.guild.id)
            await ctx.send(f"👋 {ctx.guild.me.mention} — {self._status_line(player)}")

        if query is None:
            query = FARM_DEFAULT_URL
            if not query:
                await ctx.send("❌ No pusiste un link y no hay `FARM_DEFAULT_URL` configurado en las variables de entorno.")
                return

        if ctx.author.voice is None or ctx.author.voice.channel is None:
            await ctx.send("❌ Debes estar en un canal de voz para usar este comando.")
            return

        player = self.get_player(ctx.guild.id)
        if not await ensure_voice_connection(ctx, player):
            return

        async with ctx.typing():
            try:
                track = await asyncio.to_thread(extract_track, query)
            except yt_dlp.utils.DownloadError:
                await ctx.send("❌ No pude encontrar o reproducir eso. Revisa el link o el nombre.")
                return

        track.requester = ctx.author
        player.loop_current = True
        player.farm_channel_id = player.voice_client.channel.id
        player.next_override = track
        if player.voice_client.is_playing() or player.voice_client.is_paused():
            player.voice_client.stop()
        else:
            await player._start(track)

        await ctx.send(
            f"🌾 Modo farm activado con **{track.title}**. Se repetirá en bucle indefinidamente; "
            "para detenerlo, presiona ⏹ Detener en el mensaje de reproducción."
        )

    @commands.command(name="queue")
    async def queue_(self, ctx: commands.Context):
        player = self.get_player(ctx.guild.id)
        if player.current is None and not player.queue:
            await ctx.send("La cola está vacía.")
            return
        lines = []
        if player.current:
            lines.append(f"🎵 Sonando: **{player.current.title}**")
        for i, track in enumerate(list(player.queue)[:10], start=1):
            lines.append(f"{i}. {track.title}")
        if len(player.queue) > 10:
            lines.append(f"... y {len(player.queue) - 10} más")
        embed = discord.Embed(title="Cola de reproducción", description="\n".join(lines), color=discord.Color.blurple())
        await ctx.send(embed=embed)

    @commands.command(name="help")
    async def help_(self, ctx: commands.Context):
        embed = discord.Embed(title="🎵 Comandos del bot de música", color=discord.Color.blurple())
        embed.add_field(
            name="!play <link o nombre>",
            value=(
                "Reproduce un link de YouTube, un link de playlist (hasta 200 canciones), "
                "o busca por nombre/artista y reproduce el primer resultado.\n"
                "Ej: `!play rauw alejandro todo de ti`\n"
                "Ej: `!play https://youtube.com/playlist?list=...`"
            ),
            inline=False,
        )
        embed.add_field(
            name="!queue",
            value="Muestra qué está sonando y las próximas canciones en cola.",
            inline=False,
        )
        embed.add_field(
            name="!farm / #join [link o nombre] / #join @bot1 @bot2",
            value=(
                "Reproduce esa canción en bucle infinito, para quedarte fijo en el canal de voz "
                "acumulando horas. Si no pones nada, usa el link configurado en `FARM_DEFAULT_URL`. "
                "`#join` es el prefijo compartido para que varios bots respondan a la vez. "
                "Si mencionas bots específicos (`#join @bot1 @bot2`), solo esos se unen y cada uno "
                "responde primero con su estado actual. Se detiene con el botón ⏹ Detener."
            ),
            inline=False,
        )
        embed.add_field(
            name="#sts [@bot1 @bot2]",
            value=(
                "Cada bot dice a qué servicio de Railway está conectado, en qué canal está y en qué modo "
                "(lock/farm). Si mencionas bots específicos, solo esos responden."
            ),
            inline=False,
        )
        embed.add_field(
            name="!lock",
            value=(
                "Solo el dueño del servidor o el dueño del bot. Fija/desfija el bot al canal de voz actual. "
                "Con el modo activo (o en modo farm), si alguien lo desconecta o lo mueve de canal, es "
                "expulsado del servidor (si el audit log confirma quién fue) y el bot se reconecta solo. "
                "Además se manda un reporte por DM al owner del bot."
            ),
            inline=False,
        )
        embed.add_field(
            name="Al usar !play en otro canal",
            value=(
                "Si ya estoy tocando música en otro canal (sin lock ni farm), te pregunto con botones "
                "si quieres que me mueva. Si estoy en lock o farm en otro canal, simplemente no me muevo "
                "y te aviso que hay que pedirle al owner que lo quite ahí primero."
            ),
            inline=False,
        )
        embed.add_field(
            name="Botones en el mensaje de reproducción",
            value=(
                "⏮ Anterior — vuelve a la canción pasada\n"
                "⏯ Pausar / Reanudar\n"
                "⏭ Siguiente — pasa a la próxima en cola\n"
                "⏹ Detener — para todo y el bot sale del canal de voz\n"
                "Cualquiera en el mismo canal de voz que el bot puede usarlos."
            ),
            inline=False,
        )
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
