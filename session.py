from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from .paths import (
    active_profile_name_path,
    browser_profile_candidates,
    preferred_state_root,
    preferred_x_state_path,
)

X_BASE_URL = "https://x.com"
LOGIN_URL = f"{X_BASE_URL}/i/flow/login"
HOME_URL = f"{X_BASE_URL}/home"
_X_STATE_ORIGIN = "https://x.com"


class SessionRefreshUnavailable(RuntimeError):
    pass


class SessionRefreshError(RuntimeError):
    pass


def _chrome_candidates() -> list[Path]:
    if sys.platform == "win32":
        local_app_data = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
        program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        program_files_x86 = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        return [
            program_files / "Google" / "Chrome" / "Application" / "chrome.exe",
            program_files_x86 / "Google" / "Chrome" / "Application" / "chrome.exe",
            local_app_data / "Google" / "Chrome" / "Application" / "chrome.exe",
        ]
    if sys.platform == "darwin":
        return [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path.home() / "Applications" / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome",
        ]
    return [Path("/usr/bin/google-chrome"), Path("/usr/bin/chromium"), Path("/usr/bin/chromium-browser")]


def _profile_dir() -> Path:
    return preferred_state_root() / "browser_profile"


def _marker_path() -> Path:
    return preferred_state_root() / ".session_ok"


def _browser_pid_path() -> Path:
    return preferred_state_root() / ".chrome_pid"


def _is_x_cookie_domain(domain: str) -> bool:
    normalized = domain.lstrip(".").lower()
    return normalized == "x.com" or normalized.endswith(".x.com") or normalized == "twitter.com" or normalized.endswith(".twitter.com")


def _discover_chrome_binary() -> Path:
    override = os.environ.get("FETCHXH_CHROME_BIN")
    if override:
        return Path(override).expanduser()
    for candidate in _chrome_candidates():
        if candidate.exists():
            return candidate
    return _chrome_candidates()[0]


def _chrome_version_main(chrome_bin: Path) -> int | None:
    override = os.environ.get("FETCHXH_CHROME_VERSION")
    if override:
        try:
            return int(override)
        except ValueError as exc:
            raise SessionRefreshError("FETCHXH_CHROME_VERSION must be an integer.") from exc

    try:
        if sys.platform == "win32":
            cmd = f"(Get-Item '{chrome_bin}').VersionInfo.ProductVersion"
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", cmd],
                timeout=5,
                text=True,
            )
        else:
            out = subprocess.check_output([str(chrome_bin), "--version"], timeout=5, text=True)
    except Exception:
        return None

    major = out.strip().split(".")[0]
    digits = "".join(ch for ch in major if ch.isdigit())
    return int(digits) if digits else None


def _chrome_launch_args(chrome_bin: Path, profile_dir: Path, port: int, *, headless: bool) -> list[str]:
    args = [
        str(chrome_bin),
        "--remote-debugging-host=127.0.0.1",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-breakpad",
        "--disable-session-crashed-bubble",
        "--remote-allow-origins=*",
        "--window-size=1280,900",
    ]
    if headless:
        args.extend(["--headless=new", "--disable-gpu"])
    args.append("about:blank")
    return args


def _reserve_debug_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _spawn_debug_chrome(chrome_bin: Path, profile_dir: Path, port: int, *, headless: bool) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        _chrome_launch_args(chrome_bin, profile_dir, port, headless=headless),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _iter_nodriver_connections(browser: Any = None, tab: Any = None) -> list[Any]:
    seen: set[int] = set()
    connections: list[Any] = []
    for candidate in [tab, *(getattr(browser, "targets", []) or []), getattr(browser, "connection", None)]:
        if candidate is None:
            continue
        marker = id(candidate)
        if marker in seen:
            continue
        seen.add(marker)
        connections.append(candidate)
    return connections


async def _disconnect_nodriver_connections(browser: Any = None, tab: Any = None) -> None:
    for connection in _iter_nodriver_connections(browser, tab):
        disconnect = getattr(connection, "disconnect", None)
        if disconnect is None:
            continue
        try:
            await asyncio.wait_for(disconnect(), timeout=1.0)
        except Exception:
            pass
    await asyncio.sleep(0)


def _write_browser_pid(pid: int | None) -> None:
    if not pid or pid <= 0:
        return
    try:
        preferred_state_root().mkdir(parents=True, exist_ok=True)
        _browser_pid_path().write_text(str(pid), encoding="utf-8")
    except OSError:
        pass


def _clear_browser_pid() -> None:
    try:
        _browser_pid_path().unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _kill_tracked_browser() -> None:
    try:
        raw = _browser_pid_path().read_text(encoding="utf-8").strip()
    except OSError:
        return

    try:
        pid = int(raw)
    except ValueError:
        _clear_browser_pid()
        return

    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, timeout=5)
        else:
            os.kill(pid, 9)
    except Exception:
        pass
    finally:
        _clear_browser_pid()


def _clear_profile_artifacts(profile_dir: Path | None = None) -> None:
    profile_dir = profile_dir or _profile_dir()
    for name in (
        "lockfile",
        "SingletonLock",
        "SingletonSocket",
        "SingletonCookie",
        "DevToolsActivePort",
        "CrashpadMetrics-active.pma",
    ):
        path = profile_dir / name
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass

    for name in ("Crashpad", "BrowserMetrics", "DeferredBrowserMetrics"):
        path = profile_dir / name
        try:
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


def _kill_fetchxh_chrome() -> None:
    try:
        _kill_tracked_browser()
    except Exception:
        pass
    _kill_profile_chrome_processes()
    time.sleep(0.5)
    _clear_profile_artifacts()


def _kill_profile_chrome_processes() -> None:
    if sys.platform != "win32":
        return

    script = r"""
$patterns = @(
  '\fetchxh\browser_profile',
  '\fetchxh\browser_profile-',
  '\fetchx\uc_profile',
  '\fetchx\browser_profile'
)
Get-CimInstance Win32_Process -Filter "Name = 'chrome.exe'" -ErrorAction SilentlyContinue |
  Where-Object {
    $cmd = $_.CommandLine
    if (-not $cmd) { return $false }
    foreach ($pattern in $patterns) {
      if ($cmd -like ('*' + $pattern + '*')) { return $true }
    }
    return $false
  } |
  ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
  }
"""
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            timeout=8,
        )
    except Exception:
        pass


def _patch_nodriver_utf8_issue() -> None:
    spec = importlib.util.find_spec("nodriver")
    if spec is None or not spec.origin:
        return
    network_py = Path(spec.origin).resolve().parent / "cdp" / "network.py"
    try:
        data = network_py.read_bytes()
    except OSError:
        return
    if b"\xb1" not in data:
        return
    network_py.write_bytes(data.replace(b"\xb1", b"+/-"))


class XSessionRefresher:
    def __init__(
        self,
        *,
        headless: bool = True,
        delay_ms: int = 1200,
        timeout_ms: int = 15000,
        prefer_fresh_profile: bool = False,
    ) -> None:
        self.headless = headless
        self.delay_ms = delay_ms
        self.timeout_ms = timeout_ms
        self.prefer_fresh_profile = prefer_fresh_profile
        self._browser = None
        self._tab = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._nd = None
        self._chrome_proc: subprocess.Popen[bytes] | None = None
        self._debug_port: int | None = None
        self._active_profile_dir: Path | None = None

    def __enter__(self) -> XSessionRefresher:
        _patch_nodriver_utf8_issue()
        _kill_fetchxh_chrome()
        try:
            self._nd = importlib.import_module("nodriver")
        except ImportError as exc:
            raise SessionRefreshUnavailable(
                "Automatic renewal requires the optional browser dependency. Install with: pip install 'fetchxh[renew]'"
            ) from exc

        if sys.platform == "win32":
            selector_policy = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
            if selector_policy is not None:
                asyncio.set_event_loop_policy(selector_policy())
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._run(self._async_start())
        return self

    def __exit__(self, *_: object) -> None:
        if self._loop is None:
            return
        try:
            self._run(self._async_stop())
        finally:
            self._loop.close()
            self._loop = None
            asyncio.set_event_loop(None)

    def _run(self, coro: Any) -> Any:
        if self._loop is None:
            raise RuntimeError("Browser loop is not initialized.")
        asyncio.set_event_loop(self._loop)
        return self._loop.run_until_complete(coro)

    async def _async_start(self) -> None:
        chrome_bin = _discover_chrome_binary()
        if not chrome_bin.exists():
            raise SessionRefreshError(
                "Google Chrome was not found. Install it or set FETCHXH_CHROME_BIN."
            )

        errors: list[str] = []
        for profile_dir in self._profile_candidates():
            try:
                profile_dir.mkdir(parents=True, exist_ok=True)
                self._remember_active_profile(profile_dir)
                self._debug_port = _reserve_debug_port()
                self._chrome_proc = _spawn_debug_chrome(
                    chrome_bin,
                    profile_dir,
                    self._debug_port,
                    headless=self.headless,
                )
                _write_browser_pid(self._chrome_proc.pid if self._chrome_proc is not None else None)
                await self._wait_for_debug_endpoint_async(profile_dir, self._debug_port, self._chrome_proc)
                self._browser = await asyncio.wait_for(
                    self._nd.start(host="127.0.0.1", port=self._debug_port),
                    timeout=8,
                )
                self._tab = await asyncio.wait_for(self._browser.get("about:blank"), timeout=10)
                self._active_profile_dir = profile_dir
                return
            except Exception as exc:
                errors.append(f"{profile_dir.name}: {exc}")
                await self._cleanup_failed_start_async(profile_dir)

        summary = "; ".join(errors) if errors else "no usable browser profile was available"
        raise SessionRefreshError(f"Could not connect to Chrome for session renewal. Tried: {summary}")

    async def _async_stop(self) -> None:
        try:
            if self._browser is not None:
                stop = getattr(self._browser, "stop", None)
                if stop is not None:
                    try:
                        stop()
                    except Exception:
                        pass
                try:
                    await asyncio.wait_for(
                        _disconnect_nodriver_connections(self._browser, self._tab),
                        timeout=2.0,
                    )
                except Exception:
                    pass
        finally:
            self._browser = None
            self._tab = None
            self._debug_port = None
            if self._chrome_proc is not None:
                try:
                    if self._chrome_proc.poll() is None:
                        self._chrome_proc.terminate()
                        self._chrome_proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try:
                        self._chrome_proc.kill()
                        self._chrome_proc.wait(timeout=2)
                    except Exception:
                        pass
                except Exception:
                    pass
            self._chrome_proc = None
            self._active_profile_dir = None
            _clear_browser_pid()
            _kill_fetchxh_chrome()

    async def _cleanup_failed_start_async(self, profile_dir: Path) -> None:
        try:
            if self._browser is not None:
                await _disconnect_nodriver_connections(self._browser, self._tab)
        finally:
            self._browser = None
            self._tab = None
            if self._chrome_proc is not None:
                try:
                    if self._chrome_proc.poll() is None:
                        self._chrome_proc.terminate()
                        self._chrome_proc.wait(timeout=2)
                except Exception:
                    try:
                        self._chrome_proc.kill()
                        self._chrome_proc.wait(timeout=2)
                    except Exception:
                        pass
            self._chrome_proc = None
            self._debug_port = None
            _clear_browser_pid()
            _clear_profile_artifacts(profile_dir)

    def _profile_candidates(self) -> list[Path]:
        candidates: list[Path] = []
        seen: set[Path] = set()

        if self.prefer_fresh_profile:
            fresh = preferred_state_root() / f"browser_profile-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            seen.add(fresh)
            candidates.append(fresh)

        for candidate in browser_profile_candidates(prefer_legacy=not self.prefer_fresh_profile):
            if candidate not in seen:
                seen.add(candidate)
                candidates.append(candidate)

        default_profile = _profile_dir()
        if default_profile not in seen:
            seen.add(default_profile)
            candidates.append(default_profile)

        return candidates

    def _remember_active_profile(self, profile_dir: Path) -> None:
        root = preferred_state_root()
        try:
            if profile_dir.parent == root:
                root.mkdir(parents=True, exist_ok=True)
                active_profile_name_path(root).write_text(profile_dir.name, encoding="utf-8")
        except OSError:
            pass

    async def _wait_for_debug_endpoint_async(
        self,
        profile_dir: Path,
        port: int,
        proc: subprocess.Popen[bytes],
    ) -> None:
        url = f"http://127.0.0.1:{port}/json/version"
        deadline = time.time() + 12
        last_error: Exception | None = None

        while time.time() < deadline:
            exit_code = proc.poll()
            if exit_code is not None:
                if exit_code == 21:
                    raise SessionRefreshError(
                        f"Chrome could not use profile '{profile_dir.name}' because it is locked or inaccessible."
                    )
                raise SessionRefreshError(
                    f"Chrome exited early while opening profile '{profile_dir.name}' (exit code {exit_code})."
                )
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: urllib.request.urlopen(url, timeout=2).read(),
                )
                return
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                await asyncio.sleep(0.5)

        raise SessionRefreshError(
            f"Chrome did not expose a debugging endpoint for profile '{profile_dir.name}': {last_error or 'timeout'}"
        )

    async def _goto(self, url: str) -> None:
        self._tab = await self._browser.get(url)
        await self._browser.wait(0.2)

    async def _sleep_async(self, factor: float = 1.0) -> None:
        await asyncio.sleep((self.delay_ms / 1000) * factor)

    async def _current_url(self) -> str:
        await self._browser.wait(0.05)
        return (getattr(self._tab, "url", None) or "").strip()

    async def _evaluate_json(self, expression: str) -> Any:
        payload = await self._tab.evaluate(f"JSON.stringify(({expression}))", return_by_value=True)
        if not payload:
            return None
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return None

    async def _select_one(self, selector: str, *, timeout: float = 0.8) -> Any:
        try:
            return await self._tab.select(selector, timeout=timeout)
        except Exception:
            return None

    async def _looks_authenticated_async(self) -> bool:
        url = (await self._current_url()).lower()
        if "/login" in url or "/i/flow/" in url:
            return False
        for path in ("/home", "/notifications", "/messages", "/explore"):
            if path in url:
                return True
        for selector in (
            "[data-testid='AppTabBar_Home_Link']",
            "[data-testid='SideNav_NewTweet_Button']",
            "[data-testid='primaryColumn']",
            "article[data-testid='tweet']",
        ):
            if await self._select_one(selector, timeout=0.2):
                return True
        return False

    async def _wait_for_login_async(self) -> None:
        print("Complete the X login in the browser window.", flush=True)
        deadline = time.time() + 300
        while time.time() < deadline:
            await asyncio.sleep(2)
            url = (await self._current_url()).lower()
            if "/i/flow/login" not in url and "/login" not in url:
                await self._sleep_async(1.5)
                if await self._looks_authenticated_async():
                    _marker_path().parent.mkdir(parents=True, exist_ok=True)
                    _marker_path().touch()
                    print("Login detected - session saved.", flush=True)
                    return
        raise SessionRefreshError("Login timed out after 5 minutes.")

    def ensure_authenticated(self, *, force_login: bool = False) -> None:
        self._run(self._ensure_authenticated_async(force_login=force_login))

    async def _ensure_authenticated_async(self, *, force_login: bool = False) -> None:
        await self._goto(HOME_URL)
        await self._sleep_async(2.5)
        if await self._looks_authenticated_async():
            _marker_path().parent.mkdir(parents=True, exist_ok=True)
            _marker_path().touch()
            return
        if not force_login:
            raise SessionRefreshError("The saved X browser session is not authenticated.")
        await self._goto(LOGIN_URL)
        await self._wait_for_login_async()

    def export_x_state(self) -> dict[str, Any]:
        try:
            return self._run(asyncio.wait_for(self._export_x_state_async(), timeout=20))
        except TimeoutError as exc:
            raise SessionRefreshError("Timed out while exporting the refreshed X session from Chrome.") from exc

    async def _export_x_state_async(self) -> dict[str, Any]:
        current_url = (await self._current_url()).lower()
        if "x.com" not in current_url:
            await self._goto(HOME_URL)
            await self._sleep_async(1.5)

        try:
            cookies = await asyncio.wait_for(
                self._tab.send(self._nd.cdp.storage.get_cookies()),
                timeout=8.0,
            )
        except TimeoutError as exc:
            raise SessionRefreshError("Timed out while reading Chrome cookies for the refreshed X session.") from exc
        except Exception as exc:
            raise SessionRefreshError("Could not read Chrome cookies for the refreshed X session.") from exc

        if not any(_is_x_cookie_domain(cookie.domain) and cookie.name in {"auth_token", "ct0"} for cookie in cookies):
            raise SessionRefreshError("Chrome did not expose the X auth cookies after login.")

        try:
            raw_local_storage = await asyncio.wait_for(
                self._evaluate_json(
                    "Object.entries(window.localStorage).map(([name, value]) => ({name, value}))"
                ),
                timeout=4.0,
            )
        except Exception:
            raw_local_storage = []

        local_storage: list[dict[str, str]] = []
        if isinstance(raw_local_storage, list):
            for item in raw_local_storage:
                if not isinstance(item, dict):
                    continue
                name = item.get("name")
                value = item.get("value")
                if isinstance(name, str) and isinstance(value, str):
                    local_storage.append({"name": name, "value": value})

        return {
            "cookies": [cookie.to_json() for cookie in cookies if _is_x_cookie_domain(cookie.domain)],
            "origins": [{"origin": _X_STATE_ORIGIN, "localStorage": local_storage}],
        }

    def save_x_state(self, path: Path | None = None) -> Path:
        target = path or preferred_x_state_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.export_x_state(), ensure_ascii=True, indent=2), encoding="utf-8")
        return target


def renew_x_session_state(
    *,
    headless: bool = True,
    force_login: bool = False,
    delay_ms: int = 1200,
    timeout_ms: int = 15000,
) -> Path:
    with XSessionRefresher(
        headless=headless,
        delay_ms=delay_ms,
        timeout_ms=timeout_ms,
        prefer_fresh_profile=force_login,
    ) as refresher:
        refresher.ensure_authenticated(force_login=force_login)
        return refresher.save_x_state()
