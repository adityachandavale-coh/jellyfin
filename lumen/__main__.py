"""Command line entry point: ``python3 -m lumen``."""

import argparse
import getpass
import logging
import os
import signal
import sys
import time

from . import __version__, config as config_module, probe, transcode
from .auth import Auth, AuthError
from .db import Database

DEFAULT_CONFIG = "/etc/lumen/config.ini"


def build_parser():
    parser = argparse.ArgumentParser(
        prog="lumen",
        description="Lumen — a small self-hosted media streaming server.",
    )
    parser.add_argument(
        "-c", "--config", default=os.environ.get("LUMEN_CONFIG", DEFAULT_CONFIG),
        help="path to config.ini (default: %(default)s)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("--version", action="version", version="lumen " + __version__)

    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the HTTP server (default)")
    sub.add_parser("scan", help="scan the library once and exit")
    sub.add_parser("check", help="validate config and report what is available")

    users = sub.add_parser("users", help="list users")
    users.set_defaults(users_action="list")

    add = sub.add_parser("useradd", help="create a user")
    add.add_argument("username")
    add.add_argument("--admin", action="store_true")
    add.add_argument("--password", help="read from a prompt if omitted")

    passwd = sub.add_parser("passwd", help="change a user's password")
    passwd.add_argument("username")
    passwd.add_argument("--password", help="read from a prompt if omitted")

    delete = sub.add_parser("userdel", help="delete a user")
    delete.add_argument("username")

    return parser


def setup_logging(verbose):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
    )
    # systemd adds its own timestamps.
    if os.environ.get("INVOCATION_ID"):
        for handler in logging.getLogger().handlers:
            handler.setFormatter(logging.Formatter("%(levelname)-7s %(name)s: %(message)s"))


def read_password(supplied, confirm=True):
    if supplied:
        return supplied
    if not sys.stdin.isatty():
        raise SystemExit("no password given and stdin is not a terminal")
    password = getpass.getpass("Password: ")
    if confirm and password != getpass.getpass("Confirm password: "):
        raise SystemExit("passwords do not match")
    return password


def main(argv=None):
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    log = logging.getLogger("lumen")

    cfg = config_module.load(args.config)
    if args.config and not os.path.exists(args.config):
        log.warning("config %s not found; using defaults", args.config)

    command = args.command or "serve"

    if command == "check":
        return command_check(cfg)

    db = Database(cfg.database)
    auth = Auth(db, cfg)

    if command == "users":
        rows = auth.list_users()
        if not rows:
            print("no users yet — create one with: lumen useradd <name> --admin")
            return 0
        for row in rows:
            print("%-24s %s" % (
                row["username"], "admin" if row["is_admin"] else "user"
            ))
        return 0

    if command == "useradd":
        try:
            auth.create_user(args.username, read_password(args.password), args.admin)
        except AuthError as exc:
            raise SystemExit("error: %s" % exc)
        print("created %s" % args.username)
        return 0

    if command == "passwd":
        try:
            auth.set_password(args.username, read_password(args.password))
        except AuthError as exc:
            raise SystemExit("error: %s" % exc)
        print("password updated for %s (existing sessions were revoked)" % args.username)
        return 0

    if command == "userdel":
        try:
            auth.delete_user(args.username)
        except AuthError as exc:
            raise SystemExit("error: %s" % exc)
        print("deleted %s" % args.username)
        return 0

    if command == "scan":
        from .scanner import Scanner
        status = Scanner(db, cfg).scan()
        print("scanned %(scanned)d, added %(added)d, updated %(updated)d, "
              "removed %(removed)d, errors %(errors)d" % status)
        return 1 if status["errors"] else 0

    return command_serve(cfg, db, auth, log)


def command_check(cfg):
    print("lumen %s" % __version__)
    print("config:        %s%s" % (
        cfg.path or "(defaults)",
        "" if not cfg.path or os.path.exists(cfg.path) else "  [MISSING]",
    ))
    print("listen:        %s:%d" % (cfg.host, cfg.port))
    print("database:      %s" % cfg.database)
    print("cache:         %s" % cfg.cache_dir)
    print("ffmpeg:        %s" % (
        "found" if transcode.ffmpeg_available(cfg.ffmpeg) else "NOT FOUND"))
    print("ffprobe:       %s" % (
        "found" if probe.available(cfg.ffprobe) else "NOT FOUND"))
    print("transcoding:   %s" % ("enabled" if cfg.transcode_enabled else "disabled"))

    if not cfg.media_dirs:
        print("media dirs:    NONE CONFIGURED — set library.media_dirs")
        return 1
    problems = 0
    print("media dirs:")
    for directory in cfg.media_dirs:
        if not os.path.isdir(directory):
            print("  %s  [MISSING]" % directory)
            problems += 1
        elif not os.access(directory, os.R_OK | os.X_OK):
            print("  %s  [UNREADABLE]" % directory)
            problems += 1
        else:
            print("  %s  ok" % directory)
    return 1 if problems else 0


def command_serve(cfg, db, auth, log):
    from . import server

    if not cfg.media_dirs:
        log.warning(
            "no media directories configured; set library.media_dirs in %s",
            cfg.path or DEFAULT_CONFIG,
        )
    if cfg.transcode_enabled and not transcode.ffmpeg_available(cfg.ffmpeg):
        log.warning(
            "ffmpeg (%r) not found; only direct play will work. "
            "Install ffmpeg or set transcode.enabled=false", cfg.ffmpeg
        )
    if auth.user_count() == 0:
        log.warning(
            "no users exist yet — open the web UI to create the first admin, "
            "or run: lumen useradd <name> --admin"
        )

    app, httpd = server.serve(cfg, db)
    log.info("lumen %s listening on http://%s:%d", __version__, cfg.host, cfg.port)

    def shutdown(signum, _frame):
        log.info("signal %s received, shutting down", signum)
        app.scanner.stop()
        app.transcode.stop_all()
        # shutdown() blocks until the serve loop exits, so it cannot be called
        # from inside the handler thread.
        import threading
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, shutdown)
        except (OSError, ValueError, AttributeError):
            pass

    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        app.transcode.stop_all()
        httpd.server_close()
        log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
