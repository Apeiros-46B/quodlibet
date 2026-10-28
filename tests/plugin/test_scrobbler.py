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
        del self.mod

    def test_queue(self):
        queue: QLSubmitQueue = self.mod.QLSubmitQueue()
        thread = threading.Thread(target=queue.run, daemon=True)
        thread.start()
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


class TScrobblerAPI(unittest.TestCase):
    def setUp(self):
        self.queue = object.__new__(QLSubmitQueue)
        self.queue.queue = []
        self.queue.api_credentials = ("key", "secret")
        self.queue.api_session = "session"
        self.queue.handshake_sent = True
        self.queue.base_url = "https://ws.audioscrobbler.com/2.0/"
        self.queue.changed_event = threading.Event()

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
