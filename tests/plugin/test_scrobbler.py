# Copyright 2021 Nick Boultbee
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

import os
import threading
import unittest
from unittest.mock import Mock, patch
from io import BytesIO
from urllib.error import HTTPError
from urllib.parse import parse_qs
from hashlib import md5
from time import sleep, time

from quodlibet import config
from quodlibet.ext.events.qlscrobbler import QLSubmitQueue
from quodlibet.formats import AudioFile
from quodlibet.pattern import Pattern
from quodlibet.util.picklehelper import pickle_load
from tests import run_gtk_loop, init_fake_app, destroy_fake_app
from tests.plugin import PluginTestCase

A_SONG = AudioFile({"~filename": "fake.mp3", "artist": "Foo bar", "title": "The Title"})


class TScrobbler(PluginTestCase):
    @classmethod
    def setUpClass(cls):
        config.init()
        init_fake_app()

    @classmethod
    def tearDownClass(cls):
        destroy_fake_app()
        config.quit()

    def setUp(self):
        self.mod = self.modules["QLScrobbler"]
        self.plugin = self.plugins["QLScrobbler"].cls()
        # It's a class instance, so make sure :(
        self.plugin.queue.queue.clear()
        self.SCROBBLER_CACHE_FILE = self.mod.QLSubmitQueue.SCROBBLER_CACHE_FILE
        try:
            os.unlink(self.SCROBBLER_CACHE_FILE)
        except FileNotFoundError:
            pass

    def tearDown(self):
        self.plugin.disabled()
        self.plugin.queue.stop()
        if self.plugin.queue._thread:
            self.plugin.queue._thread.join(timeout=2)
        del self.mod

    def test_queue(self):
        queue: QLSubmitQueue = self.mod.QLSubmitQueue()
        songs = [A_SONG]
        for song in songs:
            queue.submit(song)
        assert len(queue.queue) == 1
        queue.dump_queue()

        loaded = self.load_queue()
        assert all(
            actual["a"] == expected["artist"] and actual["t"] == expected["title"]
            for actual, expected in zip(loaded, songs, strict=False)
        )

    def load_queue(self) -> list[dict]:
        try:
            with open(self.SCROBBLER_CACHE_FILE, "rb") as f:
                return pickle_load(f)
        except FileNotFoundError:
            return []

    def test_enabled_disabled(self):
        self.plugin.enabled()
        assert self.plugin._tid
        # Disable
        self.plugin.disabled()
        assert not self.plugin._tid

    def test_autosave(self):
        self.plugin.AUTOSAVE_INTERVAL = 0.1
        self.plugin.enabled()
        assert not self.load_queue()
        assert not self.plugin.queue.queue, "Queue not empty in test"
        self.plugin.queue.submit(A_SONG)
        assert len(self.plugin.queue.queue) == 1, "Song wasn't queued"

        queue = self.retry_queue()
        assert len(queue) == 1, "Queued song didn't get persisted"
        self.plugin.disabled()

    def retry_queue(self):
        """Allows GTK looks, flushing of disk buffers etc"""
        queue = None
        start = time()
        while time() - start < 2:
            run_gtk_loop()
            queue = self.load_queue()
            if queue:
                break
            sleep(0.1)
        return queue

    def test_enable_mid_track_and_resume(self):
        player = Mock(song=A_SONG, info=A_SONG, paused=False)
        with (
            patch.object(self.mod.app, "player", player),
            patch.object(self.plugin.queue, "start"),
        ):
            self.plugin.enabled()
            assert self.plugin.start_time > 0
            assert self.plugin.unpaused_time > 0
            assert self.plugin.queue.nowplaying_song["t"] == "The Title"
            self.plugin.plugin_on_paused()
            assert self.plugin.queue.nowplaying_song is None
            assert self.plugin.unpaused_time == 0
            self.plugin.plugin_on_unpaused()
            assert self.plugin.queue.nowplaying_song["t"] == "The Title"

    def test_new_track_replaces_paused_track(self):
        first = AudioFile({"artist": "Artist", "title": "First"})
        second = AudioFile({"artist": "Artist", "title": "Second"})
        player = Mock(song=first, info=first, paused=True)
        with (
            patch.object(self.mod.app, "player", player),
            patch.object(self.plugin.queue, "start"),
        ):
            self.plugin.enabled()
            player.paused = False
            self.plugin.plugin_on_song_started(second)
            self.plugin.plugin_on_paused()
            self.plugin.plugin_on_unpaused()
            assert self.plugin.queue.nowplaying_song["t"] == "Second"
            self.plugin.plugin_on_song_started(None)
            assert self.plugin.nowplaying is None
            assert self.plugin.queue.nowplaying_song is None

    def test_verification_instance_is_isolated(self):
        self.plugin.queue.nowplaying_song = {"a": "Artist", "t": "Track"}
        verifier = self.mod.QLSubmitQueue(load_queue=False)
        with patch.object(verifier, "_check_config"):
            verifier.changed()
        assert self.plugin.queue.nowplaying_song == {"a": "Artist", "t": "Track"}
        assert verifier._condition is not self.plugin.queue._condition
        assert verifier.queue is not self.plugin.queue.queue


class TScrobblerAPI(unittest.TestCase):
    def setUp(self):
        config.init()
        self.queue = QLSubmitQueue(load_queue=False)
        self.queue.api_credentials = ("key", "secret")
        self.queue.api_session = "session"
        self.queue.handshake_sent = True
        self.queue.base_url = "https://ws.audioscrobbler.com/2.0/"

    def tearDown(self):
        self.queue.stop()
        if self.queue._thread:
            self.queue._thread.join(timeout=2)
        config.quit()

    def test_compilation_submission(self):
        self.queue.queue = [
            {
                "a": "Track Artist",
                "albumArtist": "Various Artists",
                "t": "Title",
                "b": "Compilation",
                "i": "123",
            }
        ]
        self.queue._api_request = Mock(return_value={"scrobbles": {}})
        assert self.queue.send_submission()
        method, data = self.queue._api_request.call_args.args
        assert method == "track.scrobble"
        assert data["artist[0]"] == "Track Artist"
        assert data["albumArtist[0]"] == "Various Artists"
        assert data["timestamp[0]"] == "123"
        assert not self.queue.queue

    def test_nowplaying(self):
        self.queue.nowplaying_song = {
            "a": "Track Artist",
            "albumArtist": "Album Artist",
            "t": "Title",
        }
        self.queue._api_request = Mock(return_value={"nowplaying": {}})
        assert self.queue.send_nowplaying()
        method, data = self.queue._api_request.call_args.args
        assert method == "track.updateNowPlaying"
        assert data["artist"] == "Track Artist"
        assert data["albumArtist"] == "Album Artist"

    def test_failure_retains_queue(self):
        self.queue.queue = [{"a": "Artist", "t": "Title", "i": "123"}]
        self.queue._api_request = Mock(return_value=None)
        assert not self.queue.send_submission()
        assert len(self.queue.queue) == 1

    def test_old_queue_without_album_artist(self):
        assert self.queue._api_song({"a": "Artist", "t": "Title"}) == {
            "artist": "Artist",
            "track": "Title",
        }

    def test_format_compilation_and_album_artist_pattern(self):
        from quodlibet.ext.events.qlscrobbler import (
            DEFAULT_ARTISTPAT,
            DEFAULT_TITLEPAT,
            DEFAULT_ALBUMARTISTPAT,
        )

        self.queue.artpat = Pattern(DEFAULT_ARTISTPAT)
        self.queue.titlepat = Pattern(DEFAULT_TITLEPAT)
        self.queue.albumartpat = Pattern(DEFAULT_ALBUMARTISTPAT)
        song = AudioFile(
            {
                "~filename": "compilation.mp3",
                "title": "Song",
                "artist": "Track Artist",
                "albumartist": "Various Artists",
            }
        )
        result = self.queue._format_song(song)
        assert result["a"] == "Track Artist"
        assert result["albumArtist"] == "Various Artists"
        self.queue.albumartpat = Pattern("<albumartist> (compilation)")
        assert (
            self.queue._format_song(song)["albumArtist"]
            == "Various Artists (compilation)"
        )
        del song["albumartist"]
        self.queue.albumartpat = Pattern(DEFAULT_ALBUMARTISTPAT)
        assert self.queue._format_song(song)["albumArtist"] == ""

    def test_request_signature_and_utf8(self):
        response = BytesIO(b'{"nowplaying": {}}')
        with patch(
            "quodlibet.ext.events.qlscrobbler.urlopen", return_value=response
        ) as send:
            assert self.queue._api_request(
                "track.updateNowPlaying", {"artist": "Björk"}
            )
        url, body = send.call_args.args
        assert url == self.queue.base_url
        params = {
            key: values[0] for key, values in parse_qs(body.decode("ascii")).items()
        }
        assert params.pop("format") == "json"
        signature = params.pop("api_sig")
        assert params["artist"] == "Björk"
        assert (
            signature
            == md5(
                (
                    "".join(key + params[key] for key in sorted(params)) + "secret"
                ).encode("utf-8")
            ).hexdigest()
        )

    def test_expired_session_http_error(self):
        error = HTTPError(
            self.queue.base_url, 403, "Forbidden", {}, BytesIO(b'{"error": 9}')
        )
        with patch("quodlibet.ext.events.qlscrobbler.urlopen", side_effect=error):
            assert self.queue._api_request("track.scrobble", {"sk": "expired"}) is None
        assert not self.queue.handshake_sent

    def test_batch_limit(self):
        self.queue.queue = [{"a": "Artist", "t": "Title", "i": "123"}] * 51
        self.queue._api_request = Mock(return_value={"scrobbles": {}})
        assert self.queue.send_submission()
        data = self.queue._api_request.call_args.args[1]
        assert "artist[49]" in data
        assert "artist[50]" not in data
        assert len(self.queue.queue) == 1

    def test_authentication(self):
        self.queue.broken = False
        self.queue.username = "user"
        self.queue.password = "password"
        self.queue._api_request = Mock(return_value={"session": {"key": "new-session"}})
        assert self.queue.send_handshake()
        assert self.queue.api_session == "new-session"
        self.queue._api_request.assert_called_once_with(
            "auth.getMobileSession",
            {"username": "user", "password": "password"},
            generation=self.queue._generation,
        )

    def test_credentials_change_invalidates_session(self):
        from quodlibet.ext.events import qlscrobbler

        self.queue.username = "user"
        self.queue.password = "old-password"
        self.queue.broken = False
        self.queue.nowplaying_song = None
        settings = {
            "username": "user",
            "password": "new-password",
            "api_key": "new-key",
            "api_secret": "new-secret",
            "titlepat": "",
            "artistpat": "",
            "albumartistpat": "<composer>",
        }
        with (
            patch.object(qlscrobbler.plugin_config, "get", side_effect=settings.get),
            patch.object(qlscrobbler.plugin_config, "getboolean", return_value=True),
            patch.object(
                qlscrobbler, "config_get_url", return_value=self.queue.base_url
            ),
        ):
            self.queue._check_config()
        assert not self.queue.handshake_sent
        assert self.queue.api_session is None
        assert self.queue.api_credentials == ("new-key", "new-secret")
        assert (
            self.queue.albumartpat.format(AudioFile({"composer": "Composer"}))
            == "Composer"
        )

    def test_malformed_response(self):
        with patch(
            "quodlibet.ext.events.qlscrobbler.urlopen",
            return_value=BytesIO(b"not json"),
        ):
            assert self.queue._api_request("track.scrobble", {"sk": "session"}) is None

    def test_truncated_http_response_is_retryable(self):
        from http.client import IncompleteRead
        from unittest.mock import MagicMock

        response = MagicMock()
        response.read.side_effect = IncompleteRead(b"partial", 100)
        with patch("quodlibet.ext.events.qlscrobbler.urlopen", return_value=response):
            assert self.queue._api_request("track.updateNowPlaying", {}) is None

    def test_new_track_during_request_remains_pending(self):
        first = AudioFile({"artist": "Artist", "title": "First"})
        second = AudioFile({"artist": "Artist", "title": "Second"})
        self.queue._check_config = Mock()
        self.queue.set_nowplaying(first)

        def request(*args, **kwargs):
            self.queue.set_nowplaying(second)
            return {"nowplaying": {}}

        self.queue._api_request = Mock(side_effect=request)
        assert self.queue.send_nowplaying()
        assert self.queue.nowplaying_song["t"] == "Second"
        assert not self.queue.nowplaying_sent

    def test_repeated_track_gets_fresh_notification(self):
        self.queue._check_config = Mock()
        self.queue.set_nowplaying(A_SONG)
        previous = self.queue.nowplaying_song
        self.queue.nowplaying_sent = True
        self.queue.set_nowplaying(A_SONG)
        assert self.queue.nowplaying_song == previous
        assert self.queue.nowplaying_song is not previous
        assert not self.queue.nowplaying_sent

    def test_failed_backlog_does_not_block_nowplaying(self):
        self.queue._check_config = Mock()
        self.queue.queue.append({"a": "Artist", "t": "Old", "i": "123"})
        submitted = threading.Event()

        def submit():
            submitted.set()
            return False

        self.queue.send_submission = Mock(side_effect=submit)
        self.queue.start()
        assert submitted.wait(2)
        delivered = threading.Event()

        def request(*args, **kwargs):
            delivered.set()
            return {"nowplaying": {}}

        self.queue._api_request = Mock(side_effect=request)
        self.queue.set_nowplaying(A_SONG)
        assert delivered.wait(2)

    def test_worker_survives_unexpected_exception(self):
        self.queue._check_config = Mock()
        self.queue.queue.append({"a": "Artist", "t": "Old", "i": "123"})
        delivered = threading.Event()
        failed = threading.Event()

        def submit():
            failed.set()
            raise RuntimeError("simulated failure")

        self.queue.send_submission = Mock(side_effect=submit)
        self.queue.start()
        assert failed.wait(2)
        # An independent now-playing request must still get serviced.
        self.queue._api_request = Mock(
            side_effect=lambda *a, **kw: (delivered.set() or {"nowplaying": {}})
        )
        self.queue.set_nowplaying(A_SONG)
        assert delivered.wait(2)
        assert self.queue._thread.is_alive()

    def test_auth_backoff_resets_after_settings_change(self):
        from quodlibet.ext.events import qlscrobbler

        self.queue._auth_retry = time() + 7200
        self.queue._submit_retry = time() + 60
        self.queue._auth_delay = 7200
        settings = {
            "username": "new-user",
            "password": "new-password",
            "api_key": "new-key",
            "api_secret": "new-secret",
            "artistpat": "",
            "titlepat": "",
            "albumartistpat": "",
        }
        with (
            patch.object(qlscrobbler.plugin_config, "get", side_effect=settings.get),
            patch.object(qlscrobbler.plugin_config, "getboolean", return_value=False),
            patch.object(
                qlscrobbler, "config_get_url", return_value=self.queue.base_url
            ),
        ):
            self.queue.changed()
        assert self.queue._auth_retry == self.queue._submit_retry == 0
        assert self.queue._auth_delay == self.queue.RETRY_SECONDS
        assert self.queue._next_action() == (None, None)
        self.queue.queue.append({"a": "Artist", "t": "Track"})
        assert self.queue._next_action() == ("auth", None)

    def test_stale_authentication_does_not_install_session(self):
        self.queue.username = "user"
        self.queue.password = "password"

        def request(*args, **kwargs):
            with self.queue._condition:
                self.queue._generation += 1
            return {"session": {"key": "obsolete"}}

        self.queue._api_request = Mock(side_effect=request)
        assert not self.queue.send_handshake()
        assert self.queue.api_session == "session"

    def test_offline_blocks_requests_and_nowplaying_can_be_cleared(self):
        self.queue._check_config = Mock()
        self.queue.set_nowplaying(A_SONG)
        self.queue.offline = True
        assert self.queue._next_action() == (None, None)
        self.queue.offline = False
        assert self.queue._next_action() == ("nowplaying", None)
        self.queue.set_nowplaying(None)
        assert self.queue._next_action() == (None, None)

    def test_empty_queue_removes_saved_cache(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            self.queue.SCROBBLER_CACHE_FILE = os.path.join(directory, "queue")
            self.queue.queue.append({"a": "Artist", "t": "Track"})
            self.queue.dump_queue()
            assert os.path.exists(self.queue.SCROBBLER_CACHE_FILE)
            self.queue.queue.clear()
            self.queue.dump_queue()
            assert not os.path.exists(self.queue.SCROBBLER_CACHE_FILE)

    def test_reauthentication_resets_submission_deadlines(self):
        self.queue._submit_retry = self.queue._nowplaying_retry = 123456
        self.queue._finish_action("auth", True)
        assert self.queue._submit_retry == self.queue._nowplaying_retry == 0

    def test_one_worker_per_queue(self):
        self.queue._check_config = Mock()
        self.queue.start()
        worker = self.queue._thread
        self.queue.start()
        assert self.queue._thread is worker
        assert worker.is_alive()

    def test_restored_queue_starts_without_playback_event(self):
        self.queue._check_config = Mock()
        self.queue.queue.append({"a": "Artist", "t": "Cached", "i": "123"})
        sent = threading.Event()

        def submit():
            self.queue.queue.clear()
            sent.set()
            return True

        self.queue.send_submission = Mock(side_effect=submit)
        self.queue.start()
        assert sent.wait(2)

    def test_request_before_account_change_is_never_sent(self):
        with patch("quodlibet.ext.events.qlscrobbler.urlopen") as send:
            assert (
                self.queue._api_request("auth.getMobileSession", {}, generation=-1)
                is None
            )
        send.assert_not_called()

    def test_debug_logs_exclude_credentials(self):
        from quodlibet.ext.events import qlscrobbler

        self.queue.username = "PRIVATE-USERNAME"
        self.queue.password = "PRIVATE-PASSWORD"
        self.queue.api_credentials = ("PRIVATE-KEY", "PRIVATE-SECRET")
        self.queue.api_session = "PRIVATE-SESSION"
        with (
            patch.object(qlscrobbler, "print_d") as debug,
            patch.object(qlscrobbler, "print_w") as warning,
            patch.object(
                qlscrobbler, "urlopen", side_effect=OSError("PRIVATE-PASSWORD")
            ),
        ):
            assert not self.queue.send_handshake()
        logged = str(debug.call_args_list) + str(warning.call_args_list)
        assert "Scrobbler:" in logged
        assert "PRIVATE-" not in logged

    def test_failed_nowplaying_does_not_block_scrobbles(self):
        self.queue._check_config = Mock()
        self.queue.set_nowplaying(A_SONG)
        self.queue.queue.append({"a": "Artist", "t": "Track", "i": "123"})
        self.queue.send_nowplaying = Mock(return_value=False)
        submitted = threading.Event()

        def submit():
            self.queue.queue.clear()
            submitted.set()
            return True

        self.queue.send_submission = Mock(side_effect=submit)
        self.queue.start()
        assert submitted.wait(2)
        self.queue.send_nowplaying.assert_called_once()

    def test_recovered_session_retries_nowplaying_immediately(self):
        self.queue._check_config = Mock()
        self.queue.set_nowplaying(A_SONG)
        self.queue.username, self.queue.password = "user", "password"
        delivered = threading.Event()
        responses = [
            BytesIO(b'{"error": 9}'),
            BytesIO(b'{"session": {"key": "fresh-session"}}'),
            BytesIO(b'{"nowplaying": {}}'),
        ]
        original_finish = self.queue._finish_action

        def finish(action, success):
            original_finish(action, success)
            if action == "nowplaying" and success:
                delivered.set()

        self.queue._finish_action = finish
        with patch(
            "quodlibet.ext.events.qlscrobbler.urlopen", side_effect=responses
        ) as send:
            self.queue.start()
            assert delivered.wait(2)
            assert send.call_count == 3
            assert self.queue.nowplaying_sent
            assert self.queue.api_session == "fresh-session"

    def test_loaded_cache_survives_until_successful_submission(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            path = os.path.join(directory, "queue")
            self.queue.SCROBBLER_CACHE_FILE = path
            self.queue.queue.append({"a": "Artist", "t": "Track", "i": "123"})
            self.queue.dump_queue()
            with patch.object(QLSubmitQueue, "SCROBBLER_CACHE_FILE", path):
                restored = QLSubmitQueue()
            restored.SCROBBLER_CACHE_FILE = path
            assert len(restored.queue) == 1
            assert os.path.exists(path)
            restored._api_request = Mock(return_value={"scrobbles": {}})
            assert restored.send_submission()
            assert not os.path.exists(path)
