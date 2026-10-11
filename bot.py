from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Iterable
from urllib.parse import urlparse

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from music_queue import SkipVotes, Track, TrackQueue

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("discord-music-bot")

TOKEN = os.getenv("DISCORD_TOKEN")
DEV_GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", "0") or 0)
MAX_PLAYLIST_ITEMS = int(os.getenv("MAX_PLAYLIST_ITEMS", "50"))
MAX_BATCH_ITEMS = 25
VOLUME = max(0.0, min(float(os.getenv("DEFAULT_VOLUME", "0.5")), 2.0))

YDL_OPTIONS = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "noplaylist": False,
    "playlistend": MAX_PLAYLIST_ITEMS,
    "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
}
FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTIONS = "-vn -loglevel warning"
YOUTUBE_HOSTS = {"youtube.com", "youtu.be"}


def _format_duration(seconds: int | None) -> str:
    if not seconds:
        return "直播／未知"
    minutes, second = divmod(int(seconds), 60)
    hours, minute = divmod(minutes, 60)
    return f"{hours}:{minute:02d}:{second:02d}" if hours else f"{minute}:{second:02d}"


def _validated_lookup(query: str) -> str:
    parsed = urlparse(query)
    if parsed.scheme not in {"http", "https"}:
        return query
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if hostname in YOUTUBE_HOSTS or hostname.endswith(".youtube.com"):
        return query
    raise ValueError("只接受 YouTube 網址；其他內容請使用歌名搜尋。")


def _tracks_from_data(data: dict, requester_id: int, limit: int) -> list[Track]:
    entries: Iterable[dict] = data.get("entries") or [data]
    tracks: list[Track] = []
    for entry in entries:
        if not entry:
            continue
        webpage_url = entry.get("webpage_url") or entry.get("original_url") or entry.get("url")
        if not webpage_url:
            continue
        tracks.append(
            Track(
                title=entry.get("title") or "未知曲目",
                webpage_url=webpage_url,
                requester_id=requester_id,
                duration=entry.get("duration"),
            )
        )
    if not tracks:
        raise RuntimeError("找不到可播放的音訊")
    return tracks[:limit]


def _extract(query: str, requester_id: int) -> list[Track]:
    with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
        data = ydl.extract_info(_validated_lookup(query), download=False)
    return _tracks_from_data(data, requester_id, MAX_PLAYLIST_ITEMS)


def _extract_many(query: str, requester_id: int, limit: int) -> list[Track]:
    limit = max(1, min(int(limit), MAX_BATCH_ITEMS))
    parsed = urlparse(query)
    lookup = _validated_lookup(query) if parsed.scheme in {"http", "https"} else f"ytsearch{limit}:{query}"
    options = {**YDL_OPTIONS, "playlistend": limit}
    with yt_dlp.YoutubeDL(options) as ydl:
        data = ydl.extract_info(lookup, download=False)
    return _tracks_from_data(data, requester_id, limit)


def _fresh_stream_url(webpage_url: str) -> str:
    options = {**YDL_OPTIONS, "noplaylist": True}
    with yt_dlp.YoutubeDL(options) as ydl:
        data = ydl.extract_info(webpage_url, download=False)
    url = data.get("url")
    if not url:
        raise RuntimeError("無法取得可播放的音訊串流")
    return url


async def resolve_tracks(query: str, requester_id: int) -> list[Track]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _extract, query, requester_id)


async def resolve_many_tracks(query: str, requester_id: int, limit: int) -> list[Track]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _extract_many, query, requester_id, limit)


async def resolve_stream_url(webpage_url: str) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _fresh_stream_url, webpage_url)


class GuildPlayer:
    def __init__(self, bot: commands.Bot, guild: discord.Guild) -> None:
        self.bot = bot
        self.guild = guild
        self.queue = TrackQueue()
        self.votes = SkipVotes()
        self.current: Track | None = None
        self.text_channel: discord.abc.Messageable | None = None
        self.loop = asyncio.get_running_loop()
        self._play_lock = asyncio.Lock()
        self._closing = False
        self._skip_requested = False
        self._playback_generation = 0

    @property
    def voice(self) -> discord.VoiceClient | None:
        return self.guild.voice_client

    async def connect(self, channel: discord.VoiceChannel | discord.StageChannel) -> None:
        async with self._play_lock:
            await self._connect_unlocked(channel)

    async def _connect_unlocked(self, channel: discord.VoiceChannel | discord.StageChannel) -> None:
        if self.voice and self.voice.is_connected():
            if self.voice.channel != channel:
                await self.voice.move_to(channel)
            return
        await channel.connect(self_deaf=True)

    async def enqueue(
        self,
        tracks: list[Track],
        channel: discord.abc.Messageable,
        *,
        voice_channel: discord.VoiceChannel | discord.StageChannel | None = None,
    ) -> None:
        async with self._play_lock:
            if voice_channel is not None:
                await self._connect_unlocked(voice_channel)
            self.text_channel = channel
            self.queue.add(tracks)
        await self.start_if_idle()

    async def start_if_idle(self) -> None:
        async with self._play_lock:
            while True:
                voice = self.voice
                if not voice or not voice.is_connected() or voice.is_playing() or voice.is_paused() or self.current:
                    return
                track = self.queue.pop()
                if not track:
                    await self.disconnect()
                    return
                self.current = track
                self._skip_requested = False
                self._playback_generation += 1
                generation = self._playback_generation
                self.votes.reset()
                try:
                    stream_url = await resolve_stream_url(track.webpage_url)
                    if self._closing or self.current is not track or not voice.is_connected():
                        return
                    if self._skip_requested:
                        self.current = None
                        self._skip_requested = False
                        continue
                    source = discord.PCMVolumeTransformer(
                        discord.FFmpegPCMAudio(stream_url, before_options=FFMPEG_BEFORE, options=FFMPEG_OPTIONS),
                        volume=VOLUME,
                    )
                    voice.play(
                        source,
                        after=lambda error, item=track, token=generation: self._after_callback(item, token, error),
                    )
                    if self.text_channel:
                        try:
                            await self.text_channel.send(
                                f"🎵 正在播放：**{track.title}**（{_format_duration(track.duration)}）",
                                allowed_mentions=discord.AllowedMentions.none(),
                            )
                        except Exception:
                            logger.warning("Unable to send now-playing notification", exc_info=True)
                    return
                except Exception:
                    logger.exception("Unable to start track")
                    self.current = None
                    if self.text_channel:
                        try:
                            await self.text_channel.send("⚠️ 此曲目無法播放，已嘗試下一首。")
                        except Exception:
                            logger.warning("Unable to send playback failure notification", exc_info=True)

    def _after_callback(self, track: Track, generation: int, error: Exception | None) -> None:
        if error:
            logger.error("Voice playback error: %s", error)
        future = asyncio.run_coroutine_threadsafe(self._after_track(track, generation, error), self.loop)
        future.add_done_callback(lambda item: item.exception() if not item.cancelled() else None)

    async def _after_track(self, track: Track, generation: int, error: Exception | None = None) -> None:
        if generation != self._playback_generation or self.current is not track:
            return
        finished = track
        self.current = None
        self.votes.reset()
        if self._closing:
            return
        suppress_retry = self._skip_requested
        self._skip_requested = False
        if error is not None and not suppress_retry and finished.playback_retries < 1:
            finished.playback_retries += 1
            self.queue.add_front(finished)
            if self.text_channel:
                try:
                    await self.text_channel.send(
                        f"⚠️ **{finished.title}** 串流中斷，正在重新取得來源並重試一次。",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except Exception:
                    logger.warning("Unable to send retry notification", exc_info=True)
        await self.start_if_idle()

    async def skip(self) -> None:
        voice = self.voice
        if self.current:
            self._skip_requested = True
        if voice and (voice.is_playing() or voice.is_paused()):
            voice.stop()

    async def clear_and_disconnect(self) -> int:
        self._closing = True
        self._playback_generation += 1
        voice = self.voice
        if voice and (voice.is_playing() or voice.is_paused()):
            voice.stop()
        async with self._play_lock:
            removed = self.queue.clear()
            self.current = None
            try:
                await self.disconnect()
            finally:
                self._closing = False
            return removed

    async def disconnect(self) -> None:
        voice = self.voice
        if voice and voice.is_connected():
            await voice.disconnect(force=True)


intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
players: dict[int, GuildPlayer] = {}


def get_player(guild: discord.Guild) -> GuildPlayer:
    return players.setdefault(guild.id, GuildPlayer(bot, guild))


def member_voice_channel(interaction: discord.Interaction):
    member = interaction.user
    return getattr(getattr(member, "voice", None), "channel", None)


def same_voice_channel(interaction: discord.Interaction) -> bool:
    guild = interaction.guild
    return bool(guild and guild.voice_client and member_voice_channel(interaction) == guild.voice_client.channel)


@bot.tree.command(name="play", description="搜尋歌曲或播放網址／播放清單")
@app_commands.describe(query="歌名、YouTube 網址或播放清單網址")
async def play(interaction: discord.Interaction, query: str) -> None:
    if not interaction.guild:
        await interaction.response.send_message("此指令只能在伺服器使用。", ephemeral=True)
        return
    channel = member_voice_channel(interaction)
    if not channel:
        await interaction.response.send_message("請先加入語音頻道。", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    try:
        tracks = await resolve_tracks(query, interaction.user.id)
        player = get_player(interaction.guild)
        await player.enqueue(tracks, interaction.channel, voice_channel=channel)
        await interaction.followup.send(f"✅ 已加入 **{len(tracks)}** 首曲目。")
    except Exception:
        logger.exception("Play command failed")
        await interaction.followup.send("❌ 找不到或無法播放該內容。", ephemeral=True)


@bot.tree.command(name="playmany", description="依主題搜尋並依序加入多首歌曲")
@app_commands.describe(query="搜尋主題或播放清單網址", count="加入歌曲數量（1～25）")
async def play_many(
    interaction: discord.Interaction,
    query: str,
    count: app_commands.Range[int, 1, MAX_BATCH_ITEMS] = 10,
) -> None:
    if not interaction.guild:
        await interaction.response.send_message("此指令只能在伺服器使用。", ephemeral=True)
        return
    channel = member_voice_channel(interaction)
    if not channel:
        await interaction.response.send_message("請先加入語音頻道。", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    try:
        tracks = await resolve_many_tracks(query, interaction.user.id, count)
        player = get_player(interaction.guild)
        await player.enqueue(tracks, interaction.channel, voice_channel=channel)
        await interaction.followup.send(f"✅ 已依序加入 **{len(tracks)}** 首曲目。")
    except Exception:
        logger.exception("Playmany command failed")
        await interaction.followup.send("❌ 找不到或無法播放該內容。", ephemeral=True)


@bot.tree.command(name="queue", description="顯示播放佇列")
async def show_queue(interaction: discord.Interaction) -> None:
    if not interaction.guild or interaction.guild.id not in players:
        await interaction.response.send_message("目前沒有播放佇列。", ephemeral=True)
        return
    player = players[interaction.guild.id]
    lines = []
    if player.current:
        lines.append(f"播放中：**{player.current.title}**")
    lines.extend(f"{index}. {track.title}" for index, track in enumerate(player.queue.snapshot()[:15], 1))
    await interaction.response.send_message(
        "\n".join(lines) or "目前沒有播放佇列。",
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(name="skip", description="跳過目前歌曲；點播者／管理員可直接跳過")
async def skip(interaction: discord.Interaction) -> None:
    if not interaction.guild or interaction.guild.id not in players or not same_voice_channel(interaction):
        await interaction.response.send_message("請先加入 Bot 所在的語音頻道。", ephemeral=True)
        return
    player = players[interaction.guild.id]
    if not player.current:
        await interaction.response.send_message("目前沒有播放歌曲。", ephemeral=True)
        return
    member = interaction.user
    can_force = member.id == player.current.requester_id or getattr(member.guild_permissions, "manage_guild", False)
    listeners = [item for item in player.voice.channel.members if not item.bot]
    if can_force:
        await player.skip()
        await interaction.response.send_message("⏭️ 已跳過目前歌曲。")
        return
    votes, required, passed = player.votes.vote(member.id, len(listeners))
    if passed:
        await player.skip()
        await interaction.response.send_message(f"⏭️ 投票通過（{votes}/{required}），已跳過。")
    else:
        await interaction.response.send_message(f"🗳️ 已投票跳過（{votes}/{required}）。")


@bot.tree.command(name="pause", description="暫停播放")
async def pause(interaction: discord.Interaction) -> None:
    voice = interaction.guild.voice_client if interaction.guild else None
    if voice and voice.is_playing() and same_voice_channel(interaction):
        voice.pause()
        await interaction.response.send_message("⏸️ 已暫停。")
    else:
        await interaction.response.send_message("目前沒有可暫停的歌曲。", ephemeral=True)


@bot.tree.command(name="resume", description="繼續播放")
async def resume(interaction: discord.Interaction) -> None:
    voice = interaction.guild.voice_client if interaction.guild else None
    if voice and voice.is_paused() and same_voice_channel(interaction):
        voice.resume()
        await interaction.response.send_message("▶️ 已繼續播放。")
    else:
        await interaction.response.send_message("目前沒有暫停中的歌曲。", ephemeral=True)


@bot.tree.command(name="clearqueue", description="清空佇列並離開語音頻道")
@app_commands.checks.has_permissions(manage_guild=True)
async def clear_queue(interaction: discord.Interaction) -> None:
    if not interaction.guild or interaction.guild.id not in players:
        await interaction.response.send_message("目前沒有播放佇列。", ephemeral=True)
        return
    count = await players[interaction.guild.id].clear_and_disconnect()
    await interaction.response.send_message(f"🧹 已清除 {count} 首等待曲目並離開語音頻道。")


@bot.tree.command(name="leave", description="停止播放並離開語音頻道")
@app_commands.checks.has_permissions(manage_guild=True)
async def leave(interaction: discord.Interaction) -> None:
    if not interaction.guild or interaction.guild.id not in players:
        await interaction.response.send_message("Bot 目前不在語音頻道。", ephemeral=True)
        return
    await players[interaction.guild.id].clear_and_disconnect()
    await interaction.response.send_message("👋 已停止播放並離開語音頻道。")


@bot.event
async def on_ready() -> None:
    if DEV_GUILD_ID:
        guild = discord.Object(id=DEV_GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
    else:
        synced = await bot.tree.sync()
    logger.info("Logged in as %s; synced %d commands", bot.user, len(synced))


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is required. Copy .env.example to .env and configure it.")
    bot.run(TOKEN, log_handler=None)
