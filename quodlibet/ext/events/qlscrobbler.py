# QLScrobbler: an Audioscrobbler client plugin for Quod Libet.
# version 0.11
# (C) 2005-2016 by Joshua Kwan <joshk@triplehelix.org>,
#                  Joe Wreschnig <piman@sacredchao.net>,
#                  Franz Pletyz <fpletz@franz-pletz.org>,
#                  Nicholas J. Michalek <djphazer@gmail.com>,
#                  Steven Robertson <steven@strobe.cc>
#     2012-2024    Nick Boultbee
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

import json
import os
import threading
import time
from hashlib import md5
from http.client import HTTPException
from urllib.error import HTTPError
from urllib.parse import urlencode

from gi.repository import Gtk, GLib

import quodlibet
from quodlibet import _
from quodlibet import app, qltk
from quodlibet.pattern import Pattern
from quodlibet.query import Query
from quodlibet.plugins.events import EventPlugin
from quodlibet.plugins import PluginConfig
from quodlibet.qltk.entry import ValidatingEntry, UndoEntry
from quodlibet.qltk.msg import Message
from quodlibet.qltk import Icons
from quodlibet.util.dprint import print_d, print_w
from quodlibet.util.picklehelper import pickle_load, pickle_dump, PickleError
from quodlibet.util.urllib import urlopen

SERVICES = {
    "Last.fm": "https://ws.audioscrobbler.com/2.0/",
    "Libre.fm": "https://libre.fm/2.0/",
}

DEFAULT_SERVICE = "Last.fm"
DEFAULT_TITLEPAT = "<title><version| (<version>)>"
DEFAULT_ALBUMARTISTPAT = "<albumartist>"
DEFAULT_ARTISTPAT = "<artist|<artist>|<composer|<composer>|<performer>>>"

plugin_config = PluginConfig("scrobbler")
defaults = plugin_config.defaults
defaults.set("service", DEFAULT_SERVICE)
defaults.set("titlepat", "")
defaults.set("artistpat", "")
defaults.set("albumartistpat", "")
defaults.set("url", "")
defaults.set("username", "")
defaults.set("password", "")
defaults.set("exclude", "")
defaults.set("offline", False)
defaults.set("api_key", "")
defaults.set("api_secret", "")


def config_get_url():
    """Gets the URL for the currently configured service.
    This logic was used often enough to be split out from generic config
    """

    # TODO: share this between the classes better
    service = plugin_config.get("service")
    if service in SERVICES:
        return SERVICES[service]
    return plugin_config.get("url")


def config_get_title_pattern():
    return plugin_config.get("titlepat") or DEFAULT_TITLEPAT


def config_get_artist_pattern():
    return plugin_config.get("artistpat") or DEFAULT_ARTISTPAT


def config_get_albumartist_pattern():
    return plugin_config.get("albumartistpat") or DEFAULT_ALBUMARTISTPAT


class QLSubmitQueue:
    """One submission worker and its state, independent of playback events.

    The condition protects queue/configuration state, never network I/O.
    Deadlines are independent so a failed backlog cannot starve now-playing.
    """

    SCROBBLER_CACHE_FILE = os.path.join(quodlibet.get_user_dir(), "scrobbler_cache_v2")
    RETRY_SECONDS = 60

    def __init__(self, load_queue=True):
        self._condition = threading.Condition(threading.RLock())
        self._thread = None
        self._stopped = False
        self._generation = 0
        self._auth_retry = self._submit_retry = self._nowplaying_retry = 0
        self._auth_delay = self.RETRY_SECONDS
        self.queue = []
        self.nowplaying_song = None
        self.nowplaying_sent = False
        self.handshake_sent = False
        self.broken = False
        self.offline = False
        self.api_credentials = ("", "")
        self.api_session = None
        self.username, self.password, self.base_url = ("", "", "")
        self.titlepat = Pattern(config_get_title_pattern())
        self.artpat = Pattern(config_get_artist_pattern())
        self.albumartpat = Pattern(config_get_albumartist_pattern())
        if load_queue:
            self._load_queue()

    def start(self):
        self.changed()
        with self._condition:
            if self._thread is None or not self._thread.is_alive():
                self._stopped = False
                self._thread = threading.Thread(
                    target=self.run, name="AudioScrobbler", daemon=True
                )
                self._thread.start()
                print_d("Scrobbler: worker started")

    def stop(self):
        """Stop after any in-flight request; primarily useful for tests."""
        with self._condition:
            self._stopped = True
            self._condition.notify_all()

    def set_nowplaying(self, song):
        with self._condition:
            self._check_config()
            # Every playback occurrence gets a new object, including repeats.
            self.nowplaying_song = self._format_song(song) if song is not None else None
            self.nowplaying_sent = False
            self._nowplaying_retry = 0
            print_d(
                f"Scrobbler: now-playing replaced; pending={bool(self.nowplaying_song)}"
            )
            self._condition.notify_all()

    def submit(self, song, timestamp=0):
        with self._condition:
            self._check_config()
            formatted = self._format_song(song)
            if formatted is None or timestamp < 0:
                print_d(
                    "Scrobbler: submission skipped (missing tags or invalid timestamp)"
                )
                return
            formatted["i"] = str(timestamp or int(time.time()))
            self.queue.append(formatted)
            print_d(f"Scrobbler: queued track; queue={len(self.queue)}")
            self._condition.notify_all()

    def _format_song(self, song):
        store = {
            "l": str(int(song.get("~#length", 0))),
            "n": str(song("~#track")),
            "b": song.comma("album"),
            "m": song("musicbrainz_trackid"),
            "t": self.titlepat.format(song),
            "a": self.artpat.format(song),
            "albumArtist": self.albumartpat.format(song),
        }
        return store if store["a"] and store["t"] else None

    def _load_queue(self):
        try:
            with open(self.SCROBBLER_CACHE_FILE, "rb") as queue_file:
                disk_queue = pickle_load(queue_file)
            self.queue.extend(disk_queue)
            print_d(f"Scrobbler: restored queue; queue={len(self.queue)}")
        except (OSError, PickleError):
            pass

    def dump_queue(self):
        with self._condition:
            try:
                if self.queue:
                    with open(self.SCROBBLER_CACHE_FILE, "wb") as queue_file:
                        pickle_dump(self.queue, queue_file)
                elif os.path.exists(self.SCROBBLER_CACHE_FILE):
                    # Don't restore previously submitted tracks on next startup.
                    os.unlink(self.SCROBBLER_CACHE_FILE)
            except (OSError, PickleError) as error:
                print_w(f"Couldn't persist scrobble queue ({type(error).__name__})")

    def _check_config(self):
        # Called on the main thread, with the condition held.
        settings = (
            plugin_config.get("username"),
            plugin_config.get("password"),
            config_get_url(),
            (plugin_config.get("api_key"), plugin_config.get("api_secret")),
        )
        if settings != (
            self.username,
            self.password,
            self.base_url,
            self.api_credentials,
        ):
            self.username, self.password, self.base_url, self.api_credentials = settings
            self._generation += 1
            self.broken = False
            self.handshake_sent = False
            self.api_session = None
            self._auth_retry = self._submit_retry = self._nowplaying_retry = 0
            self._auth_delay = self.RETRY_SECONDS
            self.nowplaying_sent = False
            print_d(
                "Scrobbler: account settings changed; authentication and retries reset"
            )
        self.broken = self.broken or not (
            self.username
            and self.password
            and all(self.api_credentials)
            and self.base_url.startswith("https://")
        )
        offline = plugin_config.getboolean("offline")
        if offline != self.offline:
            self._auth_retry = self._submit_retry = self._nowplaying_retry = 0
            self.nowplaying_sent = False
            print_d(f"Scrobbler: offline={offline}")
        self.offline = offline
        self.titlepat = Pattern(config_get_title_pattern())
        self.artpat = Pattern(config_get_artist_pattern())
        self.albumartpat = Pattern(config_get_albumartist_pattern())

    def changed(self, retry=False):
        with self._condition:
            if retry:
                self.broken = False
                self.handshake_sent = False
                self._auth_retry = self._submit_retry = self._nowplaying_retry = 0
                self._auth_delay = self.RETRY_SECONDS
            self._check_config()
            pending = bool(self.nowplaying_song) and not self.nowplaying_sent
            print_d(
                f"Scrobbler: wake; queue={len(self.queue)}, "
                f"now-playing={pending}, "
                f"blocked={self.broken}, offline={self.offline}"
            )
            self._condition.notify_all()

    def _next_action(self):
        """Called under the condition. Return an action or a wait timeout."""
        now = time.monotonic()
        pending_np = self.nowplaying_song is not None and not self.nowplaying_sent
        if self.broken or self.offline or not (self.queue or pending_np):
            return None, None
        if not self.handshake_sent:
            return (
                ("auth", None)
                if now >= self._auth_retry
                else (None, self._auth_retry - now)
            )
        deadlines = []
        if pending_np:
            if now >= self._nowplaying_retry:
                return "nowplaying", None
            deadlines.append(self._nowplaying_retry)
        if self.queue:
            if now >= self._submit_retry:
                return "submit", None
            deadlines.append(self._submit_retry)
        return None, min(deadlines) - now

    def _finish_action(self, action, success):
        now = time.monotonic()
        if action == "auth":
            self._auth_retry = 0 if success else now + self._auth_delay
            self._auth_delay = (
                self.RETRY_SECONDS if success else min(self._auth_delay * 2, 7200)
            )
            if success:
                self._submit_retry = self._nowplaying_retry = 0
        elif action == "submit":
            self._submit_retry = 0 if success else now + self.RETRY_SECONDS
        else:
            self._nowplaying_retry = 0 if success else now + self.RETRY_SECONDS
        deadline = {
            "auth": self._auth_retry,
            "submit": self._submit_retry,
            "nowplaying": self._nowplaying_retry,
        }[action]
        print_d(
            f"Scrobbler: {action} completed; success={success}, "
            f"queue={len(self.queue)}, "
            f"retry-in={max(0, deadline - now):.0f}s"
        )

    def run(self):
        while True:
            with self._condition:
                if self._stopped:
                    return
                action, timeout = self._next_action()
                if action is None:
                    self._condition.wait(timeout)
                    continue
                generation = self._generation
                nowplaying = self.nowplaying_song
            try:
                success = {
                    "auth": self.send_handshake,
                    "submit": self.send_submission,
                    "nowplaying": self.send_nowplaying,
                }[action]()
            except Exception as error:
                # A single unexpected failure must not kill the worker. Avoid
                # exception text: third-party exceptions can contain credentials.
                print_w(
                    f"Scrobbler: {action} raised {type(error).__name__}; will retry"
                )
                success = False
            with self._condition:
                if generation == self._generation:
                    if action != "nowplaying" or nowplaying is self.nowplaying_song:
                        self._finish_action(action, success)

    def send_handshake(self, show_dialog=False):
        with self._condition:
            if self.broken:
                return False
            generation = self._generation
            params = {"username": self.username, "password": self.password}
        response = self._api_request(
            "auth.getMobileSession", params, generation=generation
        )
        session = response.get("session") if response else None
        with self._condition:
            if generation != self._generation:
                return False
            if isinstance(session, dict) and session.get("key"):
                self.api_session = session["key"]
                self.handshake_sent = True
                print_d("Scrobbler: authenticated")
                return True
        if show_dialog:
            self.quick_dialog(
                _("Authentication failed. Check your account and API credentials."),
                Gtk.MessageType.ERROR,
            )
        return False

    def _api_request(self, method, params, generation=None):
        with self._condition:
            if generation is not None and generation != self._generation:
                return None
            generation = self._generation
            api_key, secret = self.api_credentials
            url = self.base_url
        params = dict(params, method=method, api_key=api_key)
        signature = "".join(key + str(params[key]) for key in sorted(params))
        params["api_sig"] = md5((signature + secret).encode("utf-8")).hexdigest()
        params["format"] = "json"
        started = time.monotonic()
        print_d(f"Scrobbler: request {method} started")
        try:
            try:
                response = urlopen(url, urlencode(params).encode("ascii"), timeout=30)
            except HTTPError as error:
                response = error
            with response:
                result = json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError, HTTPException) as error:
            print_w(f"Scrobbler: {method} failed ({type(error).__name__}); will retry")
            return None
        print_d(
            f"Scrobbler: request {method} finished in {time.monotonic() - started:.2f}s"
        )
        with self._condition:
            if generation != self._generation:
                print_d("Scrobbler: discarded response after account settings changed")
                return None
            if not isinstance(result, dict):
                print_w(f"Scrobbler: {method} returned an invalid response")
                return None
            if "error" in result:
                code = result["error"]
                if code == 9:
                    self.handshake_sent = False
                    self.api_session = None
                    self._auth_retry = 0
                if code in (4, 10, 26):
                    self.broken = True
                print_w(f"Scrobbler: {method} API error {code}; blocked={self.broken}")
                return None
        return result

    @staticmethod
    def _api_song(song):
        # Preserve cached scrobbles written by previous plugin versions.
        fields = {
            "a": "artist",
            "t": "track",
            "b": "album",
            "n": "trackNumber",
            "m": "mbid",
            "l": "duration",
            "i": "timestamp",
            "albumArtist": "albumArtist",
        }
        return {fields[key]: value for key, value in song.items() if key in fields}

    def send_submission(self):
        with self._condition:
            songs = self.queue[:50]
            generation = self._generation
            data = {"sk": self.api_session}
        if not songs:
            return True
        for idx, song in enumerate(songs):
            data.update(
                {f"{key}[{idx}]": val for key, val in self._api_song(song).items()}
            )
        result = self._api_request("track.scrobble", data, generation=generation)
        with self._condition:
            if (
                generation == self._generation
                and result is not None
                and "scrobbles" in result
            ):
                del self.queue[: len(songs)]
                self.dump_queue()
                stats = result["scrobbles"]
                if isinstance(stats, dict):
                    counts = stats.get("@attr", {})
                    if isinstance(counts, dict):
                        print_d(
                            "Scrobbler: batch response "
                            f"accepted={counts.get('accepted')}, "
                            f"ignored={counts.get('ignored')}"
                        )
                print_d(
                    f"Scrobbler: submitted batch={len(songs)}; queue={len(self.queue)}"
                )
                return True
        return False

    def send_nowplaying(self):
        with self._condition:
            song = self.nowplaying_song
            generation = self._generation
            if song is None:
                return True
            data = self._api_song(song)
            data["sk"] = self.api_session
        result = self._api_request(
            "track.updateNowPlaying", data, generation=generation
        )
        success = result is not None and "nowplaying" in result
        with self._condition:
            if generation == self._generation and song is self.nowplaying_song:
                self.nowplaying_sent = success
            else:
                print_d("Scrobbler: newer now-playing state preserved after response")
        return success

    def quick_dialog_helper(self, dialog_type, msg):
        dialog = Message(dialog_type, app.window, "QLScrobbler", msg)
        dialog.connect("response", lambda dia, resp: dia.destroy())
        dialog.show()

    def quick_dialog(self, msg, dialog_type):
        GLib.idle_add(self.quick_dialog_helper, dialog_type, msg)


class QLScrobbler(EventPlugin):
    PLUGIN_ID = "QLScrobbler"
    PLUGIN_NAME = _("AudioScrobbler Submission")
    PLUGIN_DESC = _(
        "Audioscrobbler client for Last.fm, Libre.fm and other "
        "Scrobbling 2.0 services."
    )
    PLUGIN_ICON = Icons.NETWORK_WORKGROUP

    AUTOSAVE_INTERVAL = 30
    """How often, in seconds, to save the queue (if any) to disk"""

    def __init__(self):
        self.__enabled = False
        self.queue = QLSubmitQueue()
        self._tid = None

        self.start_time = 0
        self.unpaused_time = 0
        self.elapsed = 0
        self.nowplaying = None

        self.exclude = plugin_config.get("exclude")

    def settings_changed(self, *args):
        self.exclude = plugin_config.get("exclude")
        self.queue.changed()
        if self.__enabled:
            song = getattr(app.player, "info", None) or app.player.song
            self.send_nowplaying(song if not app.player.paused else None)

    def plugin_on_song_ended(self, song, stopped):
        if song is None or not self.__enabled:
            return
        if self.unpaused_time > 0:
            self.elapsed += time.monotonic() - self.unpaused_time
            self.unpaused_time = 0
        # Spec: * don't submit when song length < 00:30
        #       * submit at end of playback (not in the middle, as with v1.1)
        #       * submit if played for >= .5*length or >= 240s
        # we check 'elapsed' rather than 'length' to work around wrong ~#length
        if self.elapsed < 30:
            return
        if self.elapsed < 240 and self.elapsed <= 0.5 * song.get("~#length", 0):
            return
        print_d(f"Checking against filter {self.exclude}")
        if self.exclude and Query(self.exclude).search(song):
            print_d("Not submitting: {}".format(song("~artist~title")))
            return
        self.queue.submit(song, self.start_time)
        self.queue.set_nowplaying(None)

    def song_excluded(self, song):
        if self.exclude and Query(self.exclude).search(song):
            print_d("{} is excluded by {}".format(song("~artist~title"), self.exclude))
            return True
        return False

    def send_nowplaying(self, song):
        self.queue.set_nowplaying(
            song if song is not None and not self.song_excluded(song) else None
        )

    def plugin_on_song_started(self, song):
        self.nowplaying = song
        self.queue.set_nowplaying(None)
        if song is None:
            self.unpaused_time = 0
            self.elapsed = 0
            return
        self.start_time = int(time.time())
        if app.player.paused:
            self.unpaused_time = 0
        else:
            self.unpaused_time = time.monotonic()
        self.elapsed = 0
        if self.__enabled and not app.player.paused:
            self.send_nowplaying(song)

    def plugin_on_paused(self):
        if self.unpaused_time > 0:
            self.elapsed += time.monotonic() - self.unpaused_time
        self.unpaused_time = 0
        self.queue.set_nowplaying(None)
        print_d("Scrobbler: playback paused")

    def plugin_on_unpaused(self):
        self.unpaused_time = time.monotonic()
        if self.__enabled and self.nowplaying:
            self.send_nowplaying(self.nowplaying)
        print_d("Scrobbler: playback resumed")

    def enabled(self):
        self.__enabled = True
        print_d("Scrobbler: plugin enabled")
        self.queue.start()
        self.queue.changed(retry=True)
        self.plugin_on_song_started(
            getattr(app.player, "info", None) or app.player.song
        )
        if not self._tid:
            print_d("Starting scrobble autosaver")
            self._tid = GLib.timeout_add_seconds(
                self.AUTOSAVE_INTERVAL, lambda: self.queue.dump_queue() or True
            )

    def disabled(self):
        self.__enabled = False
        print_d("Scrobbler: plugin disabled; queued submissions may still drain")
        self.queue.set_nowplaying(None)
        self.nowplaying = None
        self.unpaused_time = 0
        if self._tid:
            print_d("Stopping scrobble autosaver")
            GLib.source_remove(self._tid)
            self._tid = None
        self.queue.dump_queue()

    def PluginPreferences(self, parent):
        def changed(entry, key):
            if entry.get_property("sensitive"):
                plugin_config.set(key, entry.get_text())
                self.settings_changed()

        def combo_changed(widget, urlent):
            service = widget.get_active_text()
            plugin_config.set("service", service)
            urlent.set_sensitive(service not in SERVICES)
            urlent.set_text(config_get_url())
            self.settings_changed()

        def check_login(*args):
            # Verification has isolated state and never loads the pending queue.
            queue = QLSubmitQueue(load_queue=False)
            queue.changed(retry=True)
            status = queue.send_handshake(show_dialog=True)
            if status:
                self.queue.changed(retry=True)
                queue.quick_dialog(
                    _("Authentication successful."), Gtk.MessageType.INFO
                )

        box = Gtk.VBox(spacing=12)

        # first frame
        table = Gtk.Table(n_rows=7, n_columns=2)
        table.props.expand = False
        table.set_col_spacings(6)
        table.set_row_spacings(6)

        labels = []
        label_names = [
            _("_Service:"),
            _("_URL:"),
            _("User_name:"),
            _("_Password:"),
            _("API key:"),
            _("API secret:"),
        ]
        for idx, name in enumerate(label_names):
            label = Gtk.Label(label=name)
            label.set_alignment(0.0, 0.5)
            label.set_use_underline(True)
            table.attach(
                label,
                0,
                1,
                idx,
                idx + 1,
                xoptions=Gtk.AttachOptions.FILL | Gtk.AttachOptions.SHRINK,
            )
            labels.append(label)

        row = 0
        service_combo = Gtk.ComboBoxText()
        table.attach(service_combo, 1, 2, row, row + 1)
        cur_service = plugin_config.get("service")

        # Translators: Other service
        other_label = _("Other…")
        for idx, serv in enumerate(sorted(SERVICES.keys()) + [other_label]):
            service_combo.append_text(serv)
            if cur_service == serv:
                service_combo.set_active(idx)
        if service_combo.get_active() == -1:
            service_combo.set_active(0)
        labels[row].set_mnemonic_widget(service_combo)
        row += 1

        # url
        entry = UndoEntry()
        entry.set_text(plugin_config.get("url"))
        entry.connect("changed", changed, "url")
        service_combo.connect("changed", combo_changed, entry)
        service_combo.emit("changed")
        table.attach(entry, 1, 2, row, row + 1)
        labels[row].set_mnemonic_widget(entry)
        row += 1

        # username
        entry = UndoEntry()
        entry.set_text(plugin_config.get("username"))
        entry.connect("changed", changed, "username")
        table.attach(entry, 1, 2, row, row + 1)
        labels[row].set_mnemonic_widget(entry)
        row += 1

        # password
        entry = UndoEntry()
        entry.set_text(plugin_config.get("password"))
        entry.set_visibility(False)
        entry.connect("changed", changed, "password")
        table.attach(entry, 1, 2, row, row + 1)
        labels[row].set_mnemonic_widget(entry)
        row += 1

        for key in ("api_key", "api_secret"):
            entry = UndoEntry()
            entry.set_text(plugin_config.get(key))
            entry.set_visibility(key != "api_secret")
            entry.set_tooltip_text(
                _(
                    "Enter the API key and secret issued by your scrobbling service "
                    "to authenticate with its Scrobbling 2.0 API."
                )
            )
            entry.connect("changed", changed, key)
            table.attach(entry, 1, 2, row, row + 1)
            labels[row].set_mnemonic_widget(entry)
            row += 1

        # verify data
        button = qltk.Button(_("_Verify account data"), Icons.DIALOG_INFORMATION)
        button.connect("clicked", check_login)
        table.attach(button, 0, 2, row, row + 1)

        box.pack_start(qltk.Frame(_("Account"), child=table), True, True, 0)

        # second frame
        table = Gtk.Table(n_rows=5, n_columns=2)
        table.props.expand = False
        table.set_col_spacings(6)
        table.set_row_spacings(6)

        label_names = [
            _("_Artist pattern:"),
            _("Album artist pattern:"),
            _("_Title pattern:"),
            _("Exclude _filter:"),
        ]

        labels = []
        for idx, name in enumerate(label_names):
            label = Gtk.Label(label=name)
            label.set_alignment(0.0, 0.5)
            label.set_use_underline(True)
            table.attach(
                label,
                0,
                1,
                idx,
                idx + 1,
                xoptions=Gtk.AttachOptions.FILL | Gtk.AttachOptions.SHRINK,
            )
            labels.append(label)

        row = 0
        # artist pattern
        entry = UndoEntry()
        entry.set_text(plugin_config.get("artistpat"))
        entry.connect("changed", changed, "artistpat")
        table.attach(entry, 1, 2, row, row + 1)
        entry.set_tooltip_text(
            _(
                "The pattern used to format "
                "the artist name for submission. Leave blank for "
                "default."
            )
        )
        labels[row].set_mnemonic_widget(entry)
        row += 1

        # album artist pattern
        entry = UndoEntry()
        entry.set_text(plugin_config.get("albumartistpat"))
        entry.connect("changed", changed, "albumartistpat")
        entry.set_tooltip_text(
            _(
                "The pattern used to format the album artist for submission. "
                "Leave blank to use the albumartist tag."
            )
        )
        table.attach(entry, 1, 2, row, row + 1)
        labels[row].set_mnemonic_widget(entry)
        row += 1

        # title pattern
        entry = UndoEntry()
        entry.set_text(plugin_config.get("titlepat"))
        entry.connect("changed", changed, "titlepat")
        table.attach(entry, 1, 2, row, row + 1)
        entry.set_tooltip_text(
            _(
                "The pattern used to format "
                "the title for submission. Leave blank for default."
            )
        )
        labels[row].set_mnemonic_widget(entry)
        row += 1

        # exclude filter
        entry = ValidatingEntry(Query.validator)
        entry.set_text(plugin_config.get("exclude"))
        entry.set_tooltip_text(_("Songs matching this filter will not be submitted"))
        entry.connect("changed", changed, "exclude")
        table.attach(entry, 1, 2, row, row + 1)
        labels[row].set_mnemonic_widget(entry)
        row += 1

        # offline mode
        offline = plugin_config.ConfigCheckButton(
            _("_Offline mode (don't submit anything)"), "offline", populate=True
        )
        offline.connect("toggled", self.settings_changed)
        table.attach(offline, 0, 2, row, row + 1)

        box.pack_start(qltk.Frame(_("Submission"), child=table), True, True, 0)

        return box
