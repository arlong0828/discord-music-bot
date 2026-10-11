import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import bot


class _FakeYoutubeDL:
    last_query = None
    last_options = None

    def __init__(self, options):
        type(self).last_options = options

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def extract_info(self, query, download=False):
        del download
        type(self).last_query = query
        return {
            "entries": [
                {
                    "title": f"Song {index}",
                    "webpage_url": f"https://example.com/{index}",
                    "duration": index,
                }
                for index in range(1, 6)
            ]
        }


class BatchSearchTests(unittest.TestCase):
    def test_playmany_slash_command_is_registered(self):
        self.assertIn("playmany", {command.name for command in bot.bot.tree.get_commands()})

    @patch.object(bot.yt_dlp, "YoutubeDL", _FakeYoutubeDL)
    def test_text_batch_search_requests_count_and_preserves_result_order(self):
        tracks = bot._extract_many("League of Legends Worlds Anthem official", 42, 3)

        self.assertEqual(_FakeYoutubeDL.last_query, "ytsearch3:League of Legends Worlds Anthem official")
        self.assertEqual([track.title for track in tracks], ["Song 1", "Song 2", "Song 3"])
        self.assertTrue(all(track.requester_id == 42 for track in tracks))

    @patch.object(bot.yt_dlp, "YoutubeDL", _FakeYoutubeDL)
    def test_batch_url_respects_requested_count(self):
        tracks = bot._extract_many("https://www.youtube.com/playlist?list=example", 42, 2)

        self.assertEqual(_FakeYoutubeDL.last_query, "https://www.youtube.com/playlist?list=example")
        self.assertEqual([track.title for track in tracks], ["Song 1", "Song 2"])

    @patch.object(bot.yt_dlp, "YoutubeDL", _FakeYoutubeDL)
    def test_internal_or_non_youtube_url_is_rejected_before_extraction(self):
        with self.assertRaisesRegex(ValueError, "YouTube"):
            bot._extract_many("http://127.0.0.1/admin", 42, 2)

    @patch.object(bot.yt_dlp, "YoutubeDL", _FakeYoutubeDL)
    def test_batch_count_is_clamped_to_supported_range(self):
        tracks = bot._extract_many("songs", 42, 100)

        self.assertEqual(_FakeYoutubeDL.last_query, "ytsearch25:songs")
        self.assertEqual(_FakeYoutubeDL.last_options["playlistend"], 25)
        self.assertEqual(len(tracks), 5)


class PlaybackRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_clear_during_stream_resolution_never_starts_track(self):
        resolution_started = bot.asyncio.Event()
        allow_resolution = bot.asyncio.Event()

        async def delayed_resolution(_url):
            resolution_started.set()
            await allow_resolution.wait()
            return "https://cdn.example/audio"

        voice = SimpleNamespace(
            is_connected=lambda: True,
            is_playing=lambda: False,
            is_paused=lambda: False,
            play=Mock(),
            stop=Mock(),
            disconnect=AsyncMock(),
        )
        guild = SimpleNamespace(id=7, voice_client=voice)
        player = bot.GuildPlayer(bot.bot, guild)
        player.queue.add([bot.Track("current", "https://example.com/current", 1)])

        with patch.object(bot, "resolve_stream_url", side_effect=delayed_resolution):
            starting = bot.asyncio.create_task(player.start_if_idle())
            await resolution_started.wait()
            clearing = bot.asyncio.create_task(player.clear_and_disconnect())
            await bot.asyncio.sleep(0)
            allow_resolution.set()
            await clearing
            await starting

        voice.play.assert_not_called()

    async def test_playback_error_retries_current_track_once_before_next(self):
        guild = SimpleNamespace(id=7, voice_client=None)
        player = bot.GuildPlayer(bot.bot, guild)
        current = bot.Track("current", "https://example.com/current", 1)
        player.current = current
        player.queue.add([bot.Track("next", "https://example.com/next", 2)])
        player.start_if_idle = AsyncMock()

        await player._after_track(current, player._playback_generation, RuntimeError("stream stalled"))

        self.assertIsNone(player.current)
        self.assertIs(player.queue.pop(), current)
        self.assertEqual(current.playback_retries, 1)
        player.start_if_idle.assert_awaited_once()

    async def test_retry_notification_failure_does_not_stop_playback_progress(self):
        guild = SimpleNamespace(id=7, voice_client=None)
        player = bot.GuildPlayer(bot.bot, guild)
        current = bot.Track("current", "https://example.com/current", 1)
        player.current = current
        player.text_channel = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("Discord unavailable")))
        player.start_if_idle = AsyncMock()

        await player._after_track(current, player._playback_generation, RuntimeError("stream stalled"))

        self.assertIs(player.queue.pop(), current)
        player.start_if_idle.assert_awaited_once()

    async def test_skip_during_retry_notification_remains_cancelled(self):
        notification_started = bot.asyncio.Event()
        release_notification = bot.asyncio.Event()

        async def delayed_send(*_args, **_kwargs):
            notification_started.set()
            await release_notification.wait()

        guild = SimpleNamespace(id=7, voice_client=None)
        player = bot.GuildPlayer(bot.bot, guild)
        current = bot.Track("current", "https://example.com/current", 1)
        player.current = current
        player.text_channel = SimpleNamespace(send=delayed_send)
        player.start_if_idle = AsyncMock()

        finishing = bot.asyncio.create_task(
            player._after_track(current, player._playback_generation, RuntimeError("stream stalled"))
        )
        await notification_started.wait()
        player._skip_requested = True
        release_notification.set()
        await finishing

        self.assertTrue(player._skip_requested)

    async def test_explicit_skip_does_not_retry_failed_current_track(self):
        guild = SimpleNamespace(id=7, voice_client=None)
        player = bot.GuildPlayer(bot.bot, guild)
        current = bot.Track("current", "https://example.com/current", 1)
        player.current = current
        player.queue.add([bot.Track("next", "https://example.com/next", 2)])
        player._skip_requested = True
        player.start_if_idle = AsyncMock()

        await player._after_track(current, player._playback_generation, RuntimeError("stream stalled"))

        self.assertEqual(player.queue.pop().title, "next")
        self.assertEqual(current.playback_retries, 0)

    async def test_skip_marks_current_track_cancelled_while_voice_is_idle(self):
        voice = SimpleNamespace(is_playing=lambda: False, is_paused=lambda: False, stop=Mock())
        guild = SimpleNamespace(id=7, voice_client=voice)
        player = bot.GuildPlayer(bot.bot, guild)
        player.current = bot.Track("current", "https://example.com/current", 1)

        await player.skip()

        self.assertTrue(player._skip_requested)
        voice.stop.assert_not_called()

    async def test_skip_during_stream_resolution_never_starts_cancelled_track(self):
        resolution_started = bot.asyncio.Event()
        allow_resolution = bot.asyncio.Event()

        async def delayed_resolution(_url):
            resolution_started.set()
            await allow_resolution.wait()
            return "https://cdn.example/audio"

        voice = SimpleNamespace(
            is_connected=lambda: True,
            is_playing=lambda: False,
            is_paused=lambda: False,
            play=Mock(),
            stop=Mock(),
            disconnect=AsyncMock(),
        )
        guild = SimpleNamespace(id=7, voice_client=voice)
        player = bot.GuildPlayer(bot.bot, guild)
        player.queue.add([bot.Track("current", "https://example.com/current", 1)])

        with patch.object(bot, "resolve_stream_url", side_effect=delayed_resolution):
            starting = bot.asyncio.create_task(player.start_if_idle())
            await resolution_started.wait()
            await player.skip()
            allow_resolution.set()
            await starting

        voice.play.assert_not_called()

    async def test_second_playback_error_advances_without_another_retry(self):
        guild = SimpleNamespace(id=7, voice_client=None)
        player = bot.GuildPlayer(bot.bot, guild)
        current = bot.Track("current", "https://example.com/current", 1, playback_retries=1)
        player.current = current
        player.queue.add([bot.Track("next", "https://example.com/next", 2)])
        player.start_if_idle = AsyncMock()

        await player._after_track(current, player._playback_generation, RuntimeError("stream stalled again"))

        self.assertEqual(player.queue.pop().title, "next")
        player.start_if_idle.assert_awaited_once()


class DisconnectRaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_failure_does_not_leave_player_closing(self):
        voice = SimpleNamespace(
            is_connected=lambda: True,
            is_playing=lambda: False,
            is_paused=lambda: False,
            disconnect=AsyncMock(side_effect=RuntimeError("disconnect failed")),
        )
        guild = SimpleNamespace(id=7, voice_client=voice)
        player = bot.GuildPlayer(bot.bot, guild)

        with self.assertRaisesRegex(RuntimeError, "disconnect failed"):
            await player.clear_and_disconnect()

        self.assertFalse(player._closing)

    async def test_clear_racing_enqueue_cancels_new_queue_and_connection(self):
        connect_started = bot.asyncio.Event()
        allow_connect = bot.asyncio.Event()
        guild = SimpleNamespace(id=7, voice_client=None)
        async def disconnect_voice(force=True):
            del force
            guild.voice_client = None

        voice = SimpleNamespace(
            is_connected=lambda: True,
            is_playing=lambda: False,
            is_paused=lambda: False,
            play=Mock(),
            disconnect=AsyncMock(side_effect=disconnect_voice),
        )

        async def connect_new(self_deaf=True):
            del self_deaf
            connect_started.set()
            await allow_connect.wait()
            guild.voice_client = voice

        player = bot.GuildPlayer(bot.bot, guild)
        channel = SimpleNamespace(connect=connect_new)
        with patch.object(bot, "resolve_stream_url", AsyncMock(return_value="https://cdn.example/audio")):
            enqueue = bot.asyncio.create_task(
                player.enqueue(
                    [bot.Track("new", "https://example.com/new", 1)],
                    SimpleNamespace(send=AsyncMock()),
                    voice_channel=channel,
                )
            )
            await connect_started.wait()
            clearing = bot.asyncio.create_task(player.clear_and_disconnect())
            allow_connect.set()
            removed = await clearing
            await enqueue

        self.assertEqual(removed, 1)
        self.assertEqual(len(player.queue), 0)
        self.assertIsNone(player.current)
        voice.play.assert_not_called()
        voice.disconnect.assert_awaited_once()

    async def test_stale_after_callback_cannot_clear_replacement_track(self):
        guild = SimpleNamespace(id=7, voice_client=None)
        player = bot.GuildPlayer(bot.bot, guild)
        old = bot.Track("old", "https://example.com/old", 1)
        replacement = bot.Track("replacement", "https://example.com/replacement", 2)
        player.current = replacement
        player._playback_generation = 2
        player.start_if_idle = AsyncMock()

        await player._after_track(old, 1, None)

        self.assertIs(player.current, replacement)
        player.start_if_idle.assert_not_awaited()

    async def test_enqueue_during_empty_queue_disconnect_reconnects_and_plays(self):
        disconnect_started = bot.asyncio.Event()
        allow_disconnect = bot.asyncio.Event()
        guild = SimpleNamespace(id=7, voice_client=None)

        async def disconnect_old(force=True):
            del force
            disconnect_started.set()
            await allow_disconnect.wait()
            guild.voice_client = None

        old_voice = SimpleNamespace(
            is_connected=lambda: True,
            is_playing=lambda: False,
            is_paused=lambda: False,
            disconnect=disconnect_old,
        )
        new_voice = SimpleNamespace(
            is_connected=lambda: True,
            is_playing=lambda: False,
            is_paused=lambda: False,
            play=Mock(),
        )

        async def connect_new(self_deaf=True):
            del self_deaf
            guild.voice_client = new_voice

        voice_channel = SimpleNamespace(connect=connect_new)
        guild.voice_client = old_voice
        player = bot.GuildPlayer(bot.bot, guild)
        bot.players[guild.id] = player

        with (
            patch.object(bot, "resolve_stream_url", AsyncMock(return_value="https://cdn.example/audio")),
            patch.object(bot.discord, "FFmpegPCMAudio", return_value=object()),
            patch.object(bot.discord, "PCMVolumeTransformer", side_effect=lambda source, volume: source),
        ):
            empty_queue = bot.asyncio.create_task(player.start_if_idle())
            await disconnect_started.wait()
            enqueue = bot.asyncio.create_task(
                player.enqueue(
                    [bot.Track("new", "https://example.com/new", 1)],
                    SimpleNamespace(send=AsyncMock()),
                    voice_channel=voice_channel,
                )
            )
            allow_disconnect.set()
            await empty_queue
            await enqueue

        new_voice.play.assert_called_once()
        self.assertEqual(player.current.title, "new")
        self.assertIs(bot.players[guild.id], player)


class ResponseSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_queue_disables_mentions_for_external_track_titles(self):
        guild = SimpleNamespace(id=99)
        response = SimpleNamespace(send_message=AsyncMock())
        interaction = SimpleNamespace(guild=guild, response=response)
        player = SimpleNamespace(
            current=bot.Track("@everyone current", "https://youtube.com/watch?v=1", 1),
            queue=bot.TrackQueue(),
        )
        player.queue.add([bot.Track("<@123> next", "https://youtube.com/watch?v=2", 1)])
        bot.players[guild.id] = player

        await bot.show_queue.callback(interaction)

        kwargs = response.send_message.await_args.kwargs
        self.assertEqual(
            kwargs["allowed_mentions"].to_dict(),
            bot.discord.AllowedMentions.none().to_dict(),
        )


if __name__ == "__main__":
    unittest.main()
