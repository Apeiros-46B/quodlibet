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
from quodlibet.errorreport import errorhook

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
    """Manages the submit queue for scrobbles. Works independently of the
    QLScrobbler plugin being enabled; other plugins may use submit() to queue
    songs for scrobbling.
    """

    SCROBBLER_CACHE_FILE = os.path.join(quodlibet.get_user_dir(), "scrobbler_cache_v2")

    # These objects are shared across instances, to allow other plugins to
    # queue scrobbles in future versions of QL
    queue: list[dict[str, str]] = []
    changed_event = threading.Event()

    def set_nowplaying(self, song):
        """Send a Now Playing notification."""
        self._check_config()
        formatted = self._format_song(song)
        if not formatted or self.nowplaying_song == formatted:
            return
        self.nowplaying_song = formatted
        self.nowplaying_sent = False
        self.changed()

    def submit(self, song, timestamp=0):
        """Submit a song. If 'timestamp' is 0, the current time will
        be used."""
        self._check_config()
        formatted = self._format_song(song)
        if formatted is None:
            return
        if timestamp > 0:
            formatted["i"] = str(timestamp)
        elif timestamp == 0:
            formatted["i"] = str(int(time.time()))
        else:
            # TODO: Forging timestamps for submission from PMPs
            return
        self.queue.append(formatted)
        self.changed()

    def _format_song(self, song):
        """Returns a dict with the keys formatted as required by spec."""
        store = {
            "l": str(int(song.get("~#length", 0))),
            "n": str(song("~#track")),
            "b": song.comma("album"),
            "m": song("musicbrainz_trackid"),
            "t": self.titlepat.format(song),
            "a": self.artpat.format(song),
            "albumArtist": self.albumartpat.format(song),
        }

        # Spec requires title and artist at minimum
        if not (store.get("a") and store.get("t")):
            return None
        return store

    def __init__(self):
        self.nowplaying_song = None
        self.nowplaying_sent = False
        self.handshake_event = None
        self.handshake_delay = 1.0
        self.failures = 0
        self.handshake_sent = 0

        self.broken = False
        self.api_credentials = ("", "")
        self.api_session = None

        self.username, self.password, self.base_url = ("", "", "")

        # These need to be set early for _format_song to work
        self.titlepat = Pattern(config_get_title_pattern())
        self.artpat = Pattern(config_get_artist_pattern())
        self.albumartpat = Pattern(config_get_albumartist_pattern())

        self._load_queue()

    def _load_queue(self):
        try:
            with open(self.SCROBBLER_CACHE_FILE, "rb") as disk_queue_file:
                disk_queue = pickle_load(disk_queue_file)
            os.unlink(self.SCROBBLER_CACHE_FILE)
            self.queue += disk_queue
        except (OSError, PickleError):
            pass

    @classmethod
    def dump_queue(cls):
        if cls.queue:
            print_d(f"Saving scrobble queue to {cls.SCROBBLER_CACHE_FILE}")
            try:
                with open(cls.SCROBBLER_CACHE_FILE, "wb") as disk_queue_file:
                    pickle_dump(cls.queue, disk_queue_file)
            except (OSError, PickleError) as e:
                print_w(f"Couldn't persist scrobble queue ({e})")

    def _check_config(self):
        user = plugin_config.get("username")
        password = plugin_config.get("password")
        url = config_get_url()
        credentials = (plugin_config.get("api_key"), plugin_config.get("api_secret"))
        settings = (user, password, url, credentials)
        if settings != (
            self.username,
            self.password,
            self.base_url,
            self.api_credentials,
        ):
            self.username, self.password, self.base_url, self.api_credentials = settings
            self.broken = False
            self.handshake_sent = False
            self.api_session = None
        if not (user and password and all(credentials) and url.startswith("https://")):
            if not self.broken and (self.queue or self.nowplaying_song):
                self.quick_dialog(
                    _(
                        "Please configure a username, password, API key and API secret "
                        "and an HTTPS API URL in the AudioScrobbler preferences."
                    ),
                    Gtk.MessageType.INFO,
                )
            self.broken = True
        self.offline = plugin_config.getboolean("offline")
        self.titlepat = Pattern(config_get_title_pattern())
        self.artpat = Pattern(config_get_artist_pattern())
        self.albumartpat = Pattern(config_get_albumartist_pattern())

    def changed(self):
        """Signal that settings or queue contents were changed."""
        self._check_config()
        pending = self.queue or (self.nowplaying_song and not self.nowplaying_sent)
        if not self.broken and not self.offline and pending:
            self.changed_event.set()
            return
        self.changed_event.clear()

    def run(self):
        """Submit songs from the queue. Call from a daemon thread."""
        # The spec calls for exponential backoff of failed handshakes, with a
        # minimum of 1m and maximum of 120m delay between attempts.
        self.handshake_sent = False
        self.handshake_event = threading.Event()
        self.handshake_event.set()
        self.handshake_delay = 1

        self.failures = 0

        while True:
            self.changed_event.wait()
            if not self.handshake_sent:
                self.handshake_event.wait()
                if self.send_handshake():
                    self.failures = 0
                    self.handshake_delay = 1
                    self.handshake_sent = True
                else:
                    self.handshake_event.clear()
                    self.handshake_delay = min(self.handshake_delay * 2, 120)
                    GLib.timeout_add(
                        self.handshake_delay * 60 * 1000, self.handshake_event.set
                    )
                    continue
            self.changed_event.wait()
            if self.queue:
                if self.send_submission():
                    self.failures = 0
                else:
                    self.failures += 1
                    self.changed_event.clear()
                    GLib.timeout_add_seconds(60, self.changed)
                    if self.failures >= 3:
                        self.handshake_sent = False
            elif self.nowplaying_song and not self.nowplaying_sent:
                self.nowplaying_sent = self.send_nowplaying()
                if not self.nowplaying_sent:
                    self.changed_event.clear()
                    GLib.timeout_add_seconds(60, self.changed)
            else:
                # Nothing left to do; wait until something changes
                self.changed_event.clear()

    def send_handshake(self, show_dialog=False):
        """Authenticate against the Scrobbling 2.0 API."""
        if self.broken:
            return False
        response = self._api_request(
            "auth.getMobileSession",
            {"username": self.username, "password": self.password},
        )
        session = response.get("session") if response else None
        if isinstance(session, dict) and session.get("key"):
            self.api_session = session["key"]
            self.handshake_sent = True
            return True
        if show_dialog:
            self.quick_dialog(
                _("Authentication failed. Check your account and API credentials."),
                Gtk.MessageType.ERROR,
            )
        return False

    def _api_request(self, method, params):
        params = dict(params, method=method, api_key=self.api_credentials[0])
        signature = "".join(key + str(params[key]) for key in sorted(params))
        params["api_sig"] = md5(
            (signature + self.api_credentials[1]).encode("utf-8")
        ).hexdigest()
        params["format"] = "json"
        try:
            try:
                response = urlopen(
                    self.base_url,
                    urlencode(params).encode("ascii"),
                    timeout=30,
                )
            except HTTPError as error:
                # API errors (including expired sessions) can use HTTP 400/403.
                response = error
            with response:
                result = json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(result, dict):
            return None
        if "error" in result:
            if result["error"] == 9:
                self.handshake_sent = False
            if result["error"] in (4, 10, 26):
                self.broken = True
                self.changed_event.clear()
            print_w(f"Scrobbling API error: {result['error']}")
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
        songs = self.queue[:50]
        if not songs:
            return True
        data = {"sk": self.api_session}
        for idx, song in enumerate(songs):
            data.update(
                {f"{key}[{idx}]": val for key, val in self._api_song(song).items()}
            )
        result = self._api_request("track.scrobble", data)
        if result is not None and "scrobbles" in result:
            del self.queue[: len(songs)]
            return True
        return False

    def send_nowplaying(self):
        data = self._api_song(self.nowplaying_song)
        data.pop("timestamp", None)
        data["sk"] = self.api_session
        result = self._api_request("track.updateNowPlaying", data)
        return result is not None and "nowplaying" in result

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

        def queue_run():
            try:
                self.queue.run()
            except Exception:
                errorhook()

        queue_thread = threading.Thread(None, queue_run)
        queue_thread.setDaemon(True)
        queue_thread.start()
        print_d(f"Started Scrobbler thread {queue_thread}")

        self.start_time = 0
        self.unpaused_time = 0
        self.elapsed = 0
        self.nowplaying = None

        self.exclude = plugin_config.get("exclude")

    def plugin_on_song_ended(self, song, stopped):
        if song is None or not self.__enabled:
            return
        if self.unpaused_time > 0:
            self.elapsed += time.time() - self.unpaused_time
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

    def song_excluded(self, song):
        if self.exclude and Query(self.exclude).search(song):
            print_d("{} is excluded by {}".format(song("~artist~title"), self.exclude))
            return True
        return False

    def send_nowplaying(self, song):
        if not self.song_excluded(song):
            self.queue.set_nowplaying(song)

    def plugin_on_song_started(self, song):
        if song is None:
            return
        self.start_time = int(time.time())
        if app.player.paused:
            self.unpaused_time = 0
        else:
            self.unpaused_time = time.time()
        self.elapsed = 0
        if self.__enabled and not app.player.paused:
            self.send_nowplaying(song)
        else:
            self.nowplaying = song

    def plugin_on_paused(self):
        if self.unpaused_time > 0:
            self.elapsed += time.time() - self.unpaused_time
        self.unpaused_time = 0

    def plugin_on_unpaused(self):
        self.unpaused_time = time.time()
        if self.__enabled and self.nowplaying:
            self.send_nowplaying(self.nowplaying)
            self.nowplaying = None

    def enabled(self):
        self.__enabled = True
        print_d("Plugin enabled - accepting new songs.")
        if not self._tid:
            print_d("Starting scrobble autosaver")
            self._tid = GLib.timeout_add_seconds(
                self.AUTOSAVE_INTERVAL, lambda: QLSubmitQueue.dump_queue() or True
            )

    def disabled(self):
        self.__enabled = False
        print_d("Plugin disabled - not accepting any new songs.")
        if self._tid:
            print_d("Stopping scrobble autosaver")
            GLib.source_remove(self._tid)
            self._tid = None
        QLSubmitQueue.dump_queue()

    def PluginPreferences(self, parent):
        def changed(entry, key):
            if entry.get_property("sensitive"):
                plugin_config.set(key, entry.get_text())

        def combo_changed(widget, urlent):
            service = widget.get_active_text()
            plugin_config.set("service", service)
            urlent.set_sensitive(service not in SERVICES)
            urlent.set_text(config_get_url())

        def check_login(*args):
            queue = QLSubmitQueue()
            queue.changed()
            status = queue.send_handshake(show_dialog=True)
            if status:
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
        table.attach(offline, 0, 2, row, row + 1)

        box.pack_start(qltk.Frame(_("Submission"), child=table), True, True, 0)

        return box
