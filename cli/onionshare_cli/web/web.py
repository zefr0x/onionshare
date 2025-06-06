# -*- coding: utf-8 -*-
"""
OnionShare | https://onionshare.org/

Copyright (C) 2014-2022 Micah Lee, et al. <micah@micahflee.com>

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""
import logging
import mimetypes
import os
import queue
import requests
import shutil
from packaging.version import Version
from waitress.server import create_server

import flask
from flask import (
    Flask,
    request,
    render_template,
    abort,
    make_response,
    send_file,
    __version__ as flask_version,
)
from flask_compress import Compress
from flask_socketio import SocketIO

from .share_mode import ShareModeWeb
from .receive_mode import ReceiveModeWeb, ReceiveModeWSGIMiddleware, ReceiveModeRequest
from .website_mode import WebsiteModeWeb
from .chat_mode import ChatModeWeb


# Stub out flask's show_server_banner function, to avoiding showing warnings that
# are not applicable to OnionShare
def stubbed_show_server_banner(env, debug, app_import_path=None, eager_loading=None):
    pass


try:
    flask.cli.show_server_banner = stubbed_show_server_banner
except Exception:
    pass


class WaitressException(Exception):
    """
    There was a problem starting the waitress web server.
    """


class Web:
    """
    The Web object is the OnionShare web server, powered by flask
    """

    REQUEST_LOAD = 0
    REQUEST_STARTED = 1
    REQUEST_PROGRESS = 2
    REQUEST_CANCELED = 3
    REQUEST_UPLOAD_INCLUDES_MESSAGE = 4
    REQUEST_UPLOAD_FILE_RENAMED = 5
    REQUEST_UPLOAD_SET_DIR = 6
    REQUEST_UPLOAD_FINISHED = 7
    REQUEST_UPLOAD_CANCELED = 8
    REQUEST_INDIVIDUAL_FILE_STARTED = 9
    REQUEST_INDIVIDUAL_FILE_PROGRESS = 10
    REQUEST_INDIVIDUAL_FILE_CANCELED = 11
    REQUEST_ERROR_DATA_DIR_CANNOT_CREATE = 12
    REQUEST_OTHER = 13

    def __init__(self, common, is_gui, mode_settings, mode="share"):
        self.common = common
        self.common.log("Web", "__init__", f"is_gui={is_gui}, mode={mode}")

        self.settings = mode_settings

        # Flask guesses the MIME type of files from a database on the operating
        # system.
        # Some operating systems, or applications that can modify the database
        # (such as the Windows Registry) can treat .js files as text/plain,
        # which breaks the chat app due to X-Content-Type-Options: nosniff.
        #
        # It's probably #notourbug but we can fix it by forcing the mimetype.
        # https://github.com/onionshare/onionshare/issues/1443
        mimetypes.add_type("text/javascript", ".js")

        self.waitress = None

        # The flask app
        self.app = Flask(
            __name__,
            static_folder=self.common.get_resource_path("static"),
            static_url_path=f"/static_{self.common.random_string(16)}",  # randomize static_url_path to avoid making /static unusable
            template_folder=self.common.get_resource_path("templates"),
        )
        self.compress = Compress()
        self.compress.init_app(self.app)

        self.app.secret_key = self.common.random_string(8)
        self.generate_static_url_path()

        # Verbose mode?
        if self.common.verbose:
            self.verbose_mode()

        # Are we running in GUI mode?
        self.is_gui = is_gui

        # If the user stops the server while a transfer is in progress, it should
        # immediately stop the transfer. In order to make it thread-safe, stop_q
        # is a queue. If anything is in it, then the user stopped the server
        self.stop_q = queue.Queue()

        # Are we using receive mode?
        self.mode = mode
        if self.mode == "receive":
            # Use custom WSGI middleware, to modify environ
            self.app.wsgi_app = ReceiveModeWSGIMiddleware(self.app.wsgi_app, self)
            # Use a custom Request class to track upload progress
            self.app.request_class = ReceiveModeRequest

        # Starting in Flask 0.11, render_template_string autoescapes template variables
        # by default. To prevent content injection through template variables in
        # earlier versions of Flask, we force autoescaping in the Jinja2 template
        # engine if we detect a Flask version with insecure default behavior.
        if Version(flask_version) < Version("0.11"):
            # Monkey-patch in the fix from https://github.com/pallets/flask/commit/99c99c4c16b1327288fd76c44bc8635a1de452bc
            Flask.select_jinja_autoescape = self._safe_select_jinja_autoescape

        self.security_headers = [
            ("X-Frame-Options", "DENY"),
            ("X-Xss-Protection", "1; mode=block"),
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"),
            ("Server", "OnionShare"),
        ]

        self.q = queue.Queue()

        self.done = False

        # shutting down the server only works within the context of flask, so the easiest way to do it is over http
        self.shutdown_password = self.common.random_string(16)

        # Keep track if the server is running
        self.running = False

        # Define the web app routes
        self.define_common_routes()

        # Create the mode web object, which defines its own routes
        self.share_mode = None
        self.receive_mode = None
        self.website_mode = None
        self.chat_mode = None
        if self.mode == "share":
            self.share_mode = ShareModeWeb(self.common, self)
        elif self.mode == "receive":
            self.receive_mode = ReceiveModeWeb(self.common, self)
        elif self.mode == "website":
            self.website_mode = WebsiteModeWeb(self.common, self)
        elif self.mode == "chat":
            if self.common.verbose:
                try:
                    self.socketio = SocketIO(
                        async_mode="gevent", logger=True, engineio_logger=True
                    )
                except ValueError:
                    self.socketio = SocketIO(logger=True, engineio_logger=True)
            else:
                try:
                    self.socketio = SocketIO(async_mode="gevent")
                except ValueError:
                    self.socketio = SocketIO()
            self.socketio.init_app(self.app)
            self.chat_mode = ChatModeWeb(self.common, self)

        self.cleanup_tempdirs = []

    def get_mode(self):
        if self.mode == "share":
            return self.share_mode
        elif self.mode == "receive":
            return self.receive_mode
        elif self.mode == "website":
            return self.website_mode
        elif self.mode == "chat":
            return self.chat_mode
        else:
            return None

    def generate_static_url_path(self):
        # The static URL path has a 128-bit random number in it to avoid having name
        # collisions with files that might be getting shared
        self.static_url_path = f"/static_{self.common.random_string(16)}"
        self.common.log(
            "Web",
            "generate_static_url_path",
            f"new static_url_path is {self.static_url_path}",
        )

        # Update the flask route to handle the new static URL path
        self.app.static_url_path = self.static_url_path
        self.app.add_url_rule(
            self.static_url_path + "/<path:filename>",
            view_func=self.app.send_static_file,
        )

    def define_common_routes(self):
        """
        Common web app routes between all modes.
        """

        @self.app.after_request
        def add_security_headers(r):
            """
            Add security headers to a response
            """
            for header, value in self.security_headers:
                r.headers.set(header, value)

            # Set a CSP header unless in website mode and the user has disabled it
            default_csp = "default-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'self'; img-src 'self' data:;"
            if self.mode != "website" or (
                not self.settings.get("website", "disable_csp")
                and not self.settings.get("website", "custom_csp")
            ):
                r.headers.set("Content-Security-Policy", default_csp)
            else:
                if self.settings.get("website", "custom_csp"):
                    r.headers.set(
                        "Content-Security-Policy",
                        self.settings.get("website", "custom_csp"),
                    )
            return r

        @self.app.errorhandler(404)
        def not_found(e):
            mode = self.get_mode()
            history_id = mode.cur_history_id
            mode.cur_history_id += 1
            return self.error404(history_id)

        @self.app.errorhandler(405)
        def method_not_allowed(e):
            mode = self.get_mode()
            history_id = mode.cur_history_id
            mode.cur_history_id += 1
            return self.error405(history_id)

        @self.app.errorhandler(500)
        def method_not_allowed(e):
            mode = self.get_mode()
            history_id = mode.cur_history_id
            mode.cur_history_id += 1
            return self.error500(history_id)

        if self.mode != "website":

            @self.app.route("/favicon.ico")
            def favicon():
                return send_file(
                    f"{self.common.get_resource_path('static')}/img/favicon.ico"
                )

    def error403(self):
        self.add_request(Web.REQUEST_OTHER, request.path)
        return render_template("403.html", static_url_path=self.static_url_path), 403

    def error404(self, history_id):
        mode = self.get_mode()
        if mode.supports_file_requests:
            self.add_request(
                self.REQUEST_INDIVIDUAL_FILE_STARTED,
                request.path,
                {"id": history_id, "status_code": 404},
            )

        self.add_request(Web.REQUEST_OTHER, request.path)
        return render_template("404.html", static_url_path=self.static_url_path), 404

    def error405(self, history_id):
        mode = self.get_mode()
        if mode.supports_file_requests:
            self.add_request(
                self.REQUEST_INDIVIDUAL_FILE_STARTED,
                request.path,
                {"id": history_id, "status_code": 405},
            )

        self.add_request(Web.REQUEST_OTHER, request.path)
        return render_template("405.html", static_url_path=self.static_url_path), 405

    def error500(self, history_id):
        mode = self.get_mode()
        if mode.supports_file_requests:
            self.add_request(
                self.REQUEST_INDIVIDUAL_FILE_STARTED,
                request.path,
                {"id": history_id, "status_code": 500},
            )

        self.add_request(Web.REQUEST_OTHER, request.path)
        return render_template("500.html", static_url_path=self.static_url_path), 500

    def _safe_select_jinja_autoescape(self, filename):
        if filename is None:
            return True
        return filename.endswith((".html", ".htm", ".xml", ".xhtml"))

    def add_request(self, request_type, path=None, data=None):
        """
        Add a request to the queue, to communicate with the GUI.
        """
        self.q.put({"type": request_type, "path": path, "data": data})

    def verbose_mode(self):
        """
        Turn on verbose mode, which will log flask errors to a file.
        """
        flask_log_filename = os.path.join(self.common.build_data_dir(), "flask.log")
        log_handler = logging.FileHandler(flask_log_filename)
        log_handler.setLevel(logging.WARNING)
        self.app.logger.addHandler(log_handler)

    def start(self, port):
        """
        Start the flask web server.
        """
        self.common.log("Web", "start", f"port={port}")

        # Make sure the stop_q is empty when starting a new server
        while not self.stop_q.empty():
            try:
                self.stop_q.get(block=False)
            except queue.Empty:
                pass

        # In Whonix, listen on 0.0.0.0 instead of 127.0.0.1 (#220)
        if os.path.exists("/usr/share/anon-ws-base-files/workstation"):
            host = "0.0.0.0"
        else:
            host = "127.0.0.1"

        self.running = True
        if self.mode == "chat":
            self.socketio.run(self.app, host=host, port=port)
        else:
            try:
                self.waitress = create_server(
                    self.app,
                    host=host,
                    port=port,
                    clear_untrusted_proxy_headers=True,
                    ident="OnionShare",
                )
                self.waitress.run()
            except Exception as e:
                if not self.waitress.shutdown:
                    raise WaitressException(f"Error starting Waitress: {e}")

    def stop(self, port):
        """
        Stop the flask web server by loading /shutdown.
        """
        self.common.log("Web", "stop", "stopping server")

        # Let the mode know that the user stopped the server
        self.stop_q.put(True)

        # If in chat mode, shutdown the socket server rather than Waitress.
        if self.mode == "chat":
            self.socketio.stop()

        if self.waitress:
            self.waitress_custom_shutdown()

    def cleanup(self):
        """
        Shut everything down and clean up temporary files, etc.
        """
        self.common.log("Web", "cleanup")

        # Clean up the tempfile.NamedTemporaryDirectory objects
        for dir in self.cleanup_tempdirs:
            dir.cleanup()

        self.cleanup_tempdirs = []

    def waitress_custom_shutdown(self):
        """Shutdown the Waitress server immediately"""
        # Code borrowed from https://github.com/Pylons/webtest/blob/4b8a3ebf984185ff4fefb31b4d0cf82682e1fcf7/webtest/http.py#L93-L104
        self.waitress.shutdown = True
        while self.waitress._map:
            triggers = list(self.waitress._map.values())
            for trigger in triggers:
                trigger.handle_close()
        self.waitress.maintenance(0)
        self.waitress.task_dispatcher.shutdown()
        return True
