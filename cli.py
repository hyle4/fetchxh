from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import textwrap
import threading
from datetime import datetime

import requests

from .client import (
    TimelineTextPost,
    fetch_following_posts,
    fetch_for_you_posts,
    refresh_runtime_config,
    renew_runtime_config,
)


def _configure_stdout() -> None:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _a(code: str) -> str:
    return f"\033[{code}m" if sys.stdout.isatty() else ""


RESET = _a("0")
BOLD = _a("1")
DIM = _a("2")
GHOST = _a("38;2;38;38;38")
PASTEL_RED = _a("38;2;255;140;140")
PASTEL_PNK = _a("2") + _a("38;2;195;130;145")
PASTEL_GRN = _a("38;2;140;220;160")
PASTEL_BLU = _a("38;2;140;190;255")
PASTEL_YLW = _a("38;2;255;210;130")
PASTEL_VLT = _a("38;2;205;160;255")
DARK_ORANGE = _a("38;2;185;110;35")

AUTHOR = BOLD + _a("36")
TS = _a("38;2;38;38;38")
CONTENT = _a("38;2;176;176;176")
MEDIA_TAG = f"{DARK_ORANGE}[m]{RESET}"
_ANSI_RE = re.compile(r"\033\[[0-9;]*m")
_SPIN_FRAMES = [".", "..", "...", ":.", ":", ":.", "...", ".."]


def _strip(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _hotkey(label: str, color: str) -> str:
    return f"{BOLD}{color}{label}{RESET}"


def _layout() -> tuple[int, str, str]:
    cols = shutil.get_terminal_size((100, 40)).columns
    padding = max(4, cols // 10)
    content_w = min(92, max(56, cols - padding * 2))
    margin = " " * ((cols - content_w) // 2)
    rule = GHOST + ("- " * (content_w // 2)).rstrip() + RESET
    return content_w, margin, rule


def _status(msg: str) -> None:
    _, m, _ = _layout()
    print(f"{m}{DIM}{msg}{RESET}", flush=True)


class _Spinner:
    def __init__(self, msg: str) -> None:
        self._msg = msg
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        _, m, _ = _layout()
        i = 0
        while not self._stop.is_set():
            frame = _SPIN_FRAMES[i % len(_SPIN_FRAMES)]
            print(f"\r{m}{DIM}{self._msg}  {frame}{RESET}   ", end="", flush=True)
            i += 1
            self._stop.wait(0.18)
        _, m, _ = _layout()
        clearlen = len(m) + len(self._msg) + 12
        print(f"\r{' ' * clearlen}\r", end="", flush=True)

    def __enter__(self) -> "_Spinner":
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join(timeout=1)


def _section_header(title: str, cw: int, m: str, rule: str) -> list[str]:
    label = f"  {PASTEL_PNK}{title}{RESET}  "
    label_plain = f"  {title}  "
    pad = max(0, (cw - len(label_plain)) // 2)
    return [
        m + rule,
        m + " " * pad + label,
        m + rule,
    ]


def _fmt_ts(value: datetime) -> str:
    try:
        current = value.astimezone()
    except ValueError:
        current = value
    return current.strftime("%Y-%m-%d %H:%M")


def _render_post(post: TimelineTextPost, cw: int, m: str) -> list[str]:
    handle = f"@{post.account_handle}"
    ts_str = _fmt_ts(post.posted_at)
    gap = cw - len(handle) - len(ts_str)
    header = f"{AUTHOR}{handle}{RESET}{' ' * max(1, gap)}{TS}{ts_str}{RESET}"
    wrapped = textwrap.wrap(post.text, width=cw) if post.text else [""]
    lines = [m + header, ""]
    body_lines = [f"{CONTENT}{line}{RESET}" for line in wrapped]
    if post.has_media:
        if body_lines:
            last_plain = wrapped[-1]
            if len(last_plain) + 4 <= cw:
                body_lines[-1] = f"{body_lines[-1]}  {MEDIA_TAG}"
            else:
                body_lines.append(MEDIA_TAG)
        else:
            body_lines.append(MEDIA_TAG)
    lines.extend(m + line for line in body_lines)
    return lines


def print_feeds(for_you: list[TimelineTextPost], following: list[TimelineTextPost]) -> None:
    if sys.platform == "win32":
        os.system("")

    cw, m, rule = _layout()

    print()
    for line in _section_header("FOR YOU", cw, m, rule):
        print(line)
    print()

    for post in for_you:
        for line in _render_post(post, cw, m):
            print(line)
        print(m + rule)

    print()
    for line in _section_header("FOLLOWING", cw, m, rule):
        print(line)
    print()

    for post in following:
        for line in _render_post(post, cw, m):
            print(line)
        print(m + rule)


def _prompt_after_feed() -> str:
    _, m, _ = _layout()
    print()

    while True:
        try:
            choice = input(
                f"{m}  [{_hotkey('r', PASTEL_BLU)}] Refresh"
                f"   [{_hotkey('m', PASTEL_VLT)}] Menu"
                f"   [{_hotkey('q', PASTEL_RED)}] Quit   "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return "quit"
        if choice in ("r", "refresh", ""):
            return "refresh"
        if choice in ("m", "menu", "back"):
            return "menu"
        if choice in ("q", "quit", "exit"):
            return "quit"
        print(f"{m}  Press r, m, or q.")


def _main_menu() -> str:
    _, m, rule = _layout()
    print()
    print(m + rule)
    print(f"{m}  {_hotkey('1.', PASTEL_BLU)}  Read feed   {DIM}For You + Following{RESET}")
    print(f"{m}  {_hotkey('2.', PASTEL_GRN)}  Renew auth    {DIM}refresh X session + query ids{RESET}")
    print(f"{m}  {_hotkey('q.', PASTEL_RED)}  Quit")
    print()
    while True:
        try:
            choice = input(
                f"{m}  [{_hotkey('1', PASTEL_BLU)}/{_hotkey('2', PASTEL_GRN)}/{_hotkey('q', PASTEL_RED)}] "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return "quit"
        if choice == "1":
            return "feed"
        if choice == "2":
            return "reload"
        if choice in ("q", "quit", "exit"):
            return "quit"
        print(f"{m}  Enter 1, 2, or q.")


def _fetch_feeds(count: int) -> tuple[list[TimelineTextPost], list[TimelineTextPost]]:
    with requests.Session() as session:
        with _Spinner(f"For You  ({count} posts)"):
            for_you = fetch_for_you_posts(count=count, session=session)
        _status(f"  For You  - {len(for_you)} posts")

        with _Spinner(f"Following  ({count} posts)"):
            following = fetch_following_posts(count=count, session=session)
        _status(f"  Following - {len(following)} posts")

    return for_you, following


def _reload_auth() -> None:
    with _Spinner("Reloading local X session"):
        config = refresh_runtime_config()
    if config.is_ready:
        _status("Session config loaded.")
    else:
        _status("Session config is incomplete. Choose 2 to renew auth before reading the feed.")


def _renew_auth() -> None:
    _status("Renewing the local X session. A browser window will open only if login is needed.")
    renew_runtime_config(interactive=True)
    _status("Session renewed.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fetchxh",
        description="Fetch raw text from X For You + Following timelines using pure HTTP requests.",
    )
    parser.add_argument("--count", type=int, default=54, help="Text posts per tab (default: 54).")
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_stdout()
    args = build_parser().parse_args(argv)

    _, m, rule = _layout()
    print(m + rule)
    print(f"{m}{BOLD}fetchxh{RESET}  {DIM}X raw text reader over HTTP{RESET}")
    print(m + rule)

    try:
        _reload_auth()
        _status("Ready.")

        while True:
            action = _main_menu()

            if action == "quit":
                break

            if action == "reload":
                try:
                    _renew_auth()
                except Exception as exc:
                    _status(f"Renew failed: {exc}")
                continue

            if action == "feed":
                while True:
                    try:
                        for_you, following = _fetch_feeds(args.count)
                    except PermissionError as exc:
                        _status(f"Authentication failed: {exc}")
                        _status("Choose 2 from the menu to renew the local X session, then retry.")
                        break
                    except Exception as exc:
                        _status(f"Request failed: {exc}")
                        _status("Choose 2 from the menu to renew the local X session, then retry.")
                        break

                    print_feeds(for_you, following)

                    sub = _prompt_after_feed()
                    if sub == "refresh":
                        continue
                    if sub == "menu":
                        break
                    return 0

    except KeyboardInterrupt:
        print()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0
