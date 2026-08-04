#!/usr/bin/env python3
"""Observe Windows WeChat receipt windows and deliver signed SynthPay events."""

from __future__ import annotations

import base64
import configparser
import ctypes
import hashlib
import hmac
import json
import logging
import os
import re
import signal
import sqlite3
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


RECEIPT_PATTERNS = (
    {
        "channel": "1",
        "titles": {"微信支付", "微信收款助手", "微信商家助手"},
        "message": re.compile(r"(?:个人收款服务|收款到账通知)"),
        "money": re.compile(r"收款金额[：:]?[￥¥](\d+(?:\.\d{1,2})?)"),
        "time": re.compile(r"收款到账通知(\d{2}月\d{2}日\d{2}:\d{2})(?=收款金额)"),
    },
    {
        "channel": "2",
        "titles": {"微信支付"},
        "message": re.compile(r"赞赏到账通知"),
        "money": re.compile(r"收款金额[：:]?[￥¥](\d+(?:\.\d{1,2})?)"),
        "time": re.compile(r"到账时间(\d{4}-\d{2}-\d{2}\d{2}:\d{2}:\d{2})"),
    },
    {
        "channel": "3",
        "titles": {"微信收款助手", "微信商家助手"},
        "message": re.compile(r"经营收款"),
        "money": re.compile(r"收款金额[：:]?[￥¥](\d+(?:\.\d{1,2})?)"),
        "time": re.compile(r"经营码收款到账通知(\d{2}月\d{2}日\d{2}:\d{2})(?=收款金额)"),
    },
    {
        "channel": "4",
        "titles": {"微信收款商业版", "微信商家助手"},
        "message": re.compile(r"收款通知"),
        "money": re.compile(r"收款金额[：:]?[￥¥](\d+(?:\.\d{1,2})?)"),
        "time": re.compile(r"收款通知(\d{2}月\d{2}日\d{2}:\d{2}:\d{2})(?=收款金额)"),
    },
    {
        "channel": "5",
        "titles": {"微信收款助手", "微信商家助手"},
        "message": re.compile(r"收款到账通知.+已存入店长"),
        "money": re.compile(r"收款金额[：:]?[￥¥](\d+(?:\.\d{1,2})?)"),
        "time": re.compile(r"收款到账通知(\d{2}月\d{2}日\d{2}:\d{2})(?=收款金额)"),
    },
)

WECHAT_CHAT_WINDOW_CLASSES = ("ChatWnd", "mmui::ChatSingleWindow")
OCR_WECHAT_WINDOW_CLASS_PREFIX = "Qt"
OCR_WECHAT_PROCESS_NAME = "weixin.exe"


def config_bool(section: configparser.SectionProxy, key: str, default: bool) -> bool:
    if key not in section:
        return default
    return section.get(key, "").strip().lower() in {"1", "true", "yes", "on"}


def money_to_cents(value: str) -> int:
    if re.fullmatch(r"\d+(?:\.\d{1,2})?", value) is None:
        raise ValueError("invalid money")
    integer, _, fraction = value.partition(".")
    return int(integer) * 100 + int((fraction + "00")[:2])


def parse_observed_at(value: str, now: datetime | None = None) -> datetime:
    now = now or datetime.now()
    for pattern in ("%Y-%m-%d%H:%M:%S", "%m月%d日%H:%M:%S"):
        try:
            parsed = datetime.strptime(value, pattern)
            if "%Y" not in pattern:
                parsed = parsed.replace(year=now.year)
            return parsed
        except ValueError:
            continue

    # WeChat personal-receipt notifications only display a minute.  Treating
    # that minute as :00 can place the receipt before the payment order was
    # created, so use the actual capture time when seconds are unavailable.
    if re.fullmatch(r"\d{2}月\d{2}日\d{2}:\d{2}", value):
        return now
    return now


def parse_receipt(window_title: str, raw_text: str, now: datetime | None = None) -> dict[str, Any] | None:
    captured_at = now or datetime.now()
    normalized = "".join(str(raw_text).split())
    raw_digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    for pattern in RECEIPT_PATTERNS:
        if window_title not in pattern["titles"] or pattern["message"].search(normalized) is None:
            continue
        money_match = pattern["money"].search(normalized)
        if money_match is None:
            continue
        time_match = pattern["time"].search(normalized)
        source_time = time_match.group(1) if time_match else ""
        observed = parse_observed_at(source_time, captured_at) if source_time else captured_at
        source_identity = source_time or raw_digest
        if source_time and not source_time.startswith(str(captured_at.year)):
            source_identity = f"{captured_at.year}:{source_time}"
        receipt_identity = hashlib.sha256(
            "\n".join((window_title, str(pattern["channel"]), money_match.group(1), source_identity)).encode("utf-8")
        ).hexdigest()
        return {
            "channel": str(pattern["channel"]),
            "money": money_match.group(1),
            "amount_cents": money_to_cents(money_match.group(1)),
            "observed_at": int(observed.timestamp() * 1000),
            "raw_digest": raw_digest,
            "receipt_identity": receipt_identity,
        }
    return None


def make_event_id(window_title: str, receipt: dict[str, Any]) -> str:
    receipt_identity = receipt.get("receipt_identity")
    if receipt_identity:
        source = "\n".join((window_title, str(receipt["channel"]), str(receipt_identity)))
        return hashlib.sha256(source.encode("utf-8")).hexdigest()
    source = "\n".join(
        (
            window_title,
            str(receipt["channel"]),
            str(receipt["amount_cents"]),
            str(receipt["observed_at"]),
            str(receipt["raw_digest"]),
        )
    )
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def sign_payload(timestamp: str, content: str, event_id: str, observed_at: str, secret: str) -> str:
    signing_text = f"{timestamp}\n{content}\n{event_id}\n{observed_at}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), signing_text, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


@dataclass(frozen=True)
class Settings:
    callback_url: str
    callback_secret: str
    state_dir: Path
    poll_interval: float
    http_timeout: float
    max_attempts: int
    dry_run: bool
    use_system_proxy: bool
    observer_mode: str
    tesseract_path: Path
    tessdata_dir: Path
    background_window: bool
    background_x: int
    background_y: int
    windows: tuple[str, ...]

    @classmethod
    def load(cls, path: Path) -> "Settings":
        parser = configparser.ConfigParser(interpolation=None)
        if not parser.read(path, encoding="utf-8"):
            raise ValueError(f"configuration file not found: {path}")
        section = parser["watcher"]
        state_value = os.path.expandvars(section.get("state_dir", r"%PROGRAMDATA%\SynthPay\wechat-watcher"))
        observer_mode = section.get("observer_mode", "auto").strip().lower()
        if observer_mode not in {"auto", "uia", "ocr"}:
            raise ValueError("observer_mode must be auto, uia, or ocr")
        settings = cls(
            callback_url=section.get("callback_url", "").strip(),
            callback_secret=section.get("callback_secret", ""),
            state_dir=Path(state_value),
            poll_interval=max(1.0, float(section.get("poll_interval_seconds", "2"))),
            http_timeout=max(1.0, float(section.get("http_timeout_seconds", "8"))),
            max_attempts=max(1, int(section.get("max_attempts", "30"))),
            dry_run=config_bool(section, "dry_run", True),
            use_system_proxy=config_bool(section, "use_system_proxy", False),
            observer_mode=observer_mode,
            tesseract_path=Path(
                os.path.expandvars(
                    section.get("tesseract_path", r"%ProgramFiles%\Tesseract-OCR\tesseract.exe")
                )
            ),
            tessdata_dir=Path(
                os.path.expandvars(section.get("tessdata_dir", r"%ProgramFiles%\Tesseract-OCR\tessdata"))
            ),
            background_window=config_bool(section, "background_window", True),
            background_x=int(section.get("background_x", "-10000")),
            background_y=int(section.get("background_y", "-10000")),
            windows=tuple(
                item.strip()
                for item in section.get(
                    "window_titles",
                    "微信支付,微信收款助手,微信收款商业版,微信商家助手",
                ).split(",")
                if item.strip()
            ),
        )
        if not settings.dry_run and (not settings.callback_url or not settings.callback_secret):
            raise ValueError("callback_url and callback_secret are required")
        if settings.callback_url and not settings.callback_url.lower().startswith("https://"):
            raise ValueError("callback_url must use HTTPS")
        return settings


class EventStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    delivered_at REAL,
                    last_error TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS events_due_idx ON events(state, next_attempt_at);
                """
            )

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def enqueue(self, event_id: str, payload: dict[str, str]) -> bool:
        now = time.time()
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO events(event_id,payload,next_attempt_at,created_at) VALUES(?,?,?,?)",
                (event_id, json.dumps(payload, ensure_ascii=False, separators=(",", ":")), now, now),
            )
            return cursor.rowcount == 1

    def next_pending(self) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT event_id,payload,attempts FROM events WHERE state='pending' AND next_attempt_at<=? ORDER BY created_at LIMIT 1",
                (time.time(),),
            ).fetchone()

    def delivered(self, event_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE events SET state='delivered',delivered_at=?,last_error='' WHERE event_id=?",
                (time.time(), event_id),
            )

    def failed(self, event_id: str, attempts: int, max_attempts: int, error: str) -> None:
        state = "dead" if attempts >= max_attempts else "pending"
        delay = min(300, 2 ** min(attempts, 8))
        with self.connect() as connection:
            connection.execute(
                "UPDATE events SET state=?,attempts=?,next_attempt_at=?,last_error=? WHERE event_id=?",
                (state, attempts, time.time() + delay, error[:500], event_id),
            )


class CallbackWorker(threading.Thread):
    def __init__(self, settings: Settings, store: EventStore, wake: threading.Event) -> None:
        super().__init__(name="callback-worker", daemon=True)
        self.settings = settings
        self.store = store
        self.wake = wake
        self.stopping = threading.Event()
        self.ssl_context = ssl.create_default_context()
        handlers: list[Any] = [urllib.request.HTTPSHandler(context=self.ssl_context)]
        if not settings.use_system_proxy:
            handlers.insert(0, urllib.request.ProxyHandler({}))
        self.opener = urllib.request.build_opener(*handlers)

    def stop(self) -> None:
        self.stopping.set()
        self.wake.set()

    def run(self) -> None:
        while not self.stopping.is_set():
            event = self.store.next_pending()
            if event is None:
                self.wake.wait(5)
                self.wake.clear()
                continue
            self.deliver(event)

    def deliver(self, event: sqlite3.Row) -> None:
        event_id = str(event["event_id"])
        if self.settings.dry_run:
            logging.info("dry-run receipt event accepted event_id=%s", event_id[:12])
            self.store.delivered(event_id)
            return
        payload = json.loads(str(event["payload"]))
        timestamp = str(time.time_ns() // 1_000_000)
        payload["timestamp"] = timestamp
        payload["sign"] = sign_payload(
            timestamp,
            str(payload["content"]),
            event_id,
            str(payload["observed_at"]),
            self.settings.callback_secret,
        )
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            self.settings.callback_url,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "SynthPay-Windows-WeChat-Watcher/1.0"},
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=self.settings.http_timeout) as response:
                result = response.read(128).decode("utf-8", errors="replace").strip()
                if response.status != 200 or result != "200":
                    raise RuntimeError(f"unexpected response status={response.status} body={result!r}")
            self.store.delivered(event_id)
            logging.info("receipt callback delivered event_id=%s", event_id[:12])
        except (urllib.error.URLError, TimeoutError, RuntimeError) as exc:
            attempts = int(event["attempts"]) + 1
            self.store.failed(event_id, attempts, self.settings.max_attempts, str(exc))
            logging.warning("receipt callback failed event_id=%s attempt=%d error=%s", event_id[:12], attempts, exc)


class WeChatUiObserver:
    def __init__(self, window_titles: tuple[str, ...]) -> None:
        import uiautomation as auto

        self.auto = auto
        self.window_titles = window_titles

    def latest_receipts(self) -> Iterable[tuple[str, str]]:
        for window_title in self.window_titles:
            raw = self.latest_receipt_text(window_title)
            if raw:
                yield window_title, raw

    def latest_receipt_text(self, window_title: str) -> str:
        window = None
        for class_name in WECHAT_CHAT_WINDOW_CLASSES:
            candidate = self.auto.WindowControl(searchDepth=1, ClassName=class_name, Name=window_title)
            if candidate.Exists(0.25):
                window = candidate
                break
        if window is None:
            return ""
        message_list = window.ListControl(searchDepth=9, Name="消息")
        if not message_list.Exists(0.5):
            return ""
        latest = message_list.GetLastChildControl()
        if latest is None:
            return ""
        candidates = latest.GetChildren()
        if candidates:
            candidates = candidates[0].GetChildren()
        if candidates:
            candidates = candidates[0].GetChildren()
        root = candidates[1] if len(candidates) > 1 else latest
        patterns = [pattern["message"] for pattern in RECEIPT_PATTERNS if window_title in pattern["titles"]]
        queue = [root]
        while queue:
            current = queue.pop(0)
            name = str(getattr(current, "Name", "") or "")
            if any(pattern.search("".join(name.split())) for pattern in patterns):
                return name
            queue.extend(current.GetChildren())
        return ""


class BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_uint32),
        ("width", ctypes.c_int32),
        ("height", ctypes.c_int32),
        ("planes", ctypes.c_uint16),
        ("bit_count", ctypes.c_uint16),
        ("compression", ctypes.c_uint32),
        ("size_image", ctypes.c_uint32),
        ("x_pixels_per_meter", ctypes.c_int32),
        ("y_pixels_per_meter", ctypes.c_int32),
        ("colors_used", ctypes.c_uint32),
        ("colors_important", ctypes.c_uint32),
    ]


class Rect(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_int32),
        ("top", ctypes.c_int32),
        ("right", ctypes.c_int32),
        ("bottom", ctypes.c_int32),
    ]


class Point(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_int32),
        ("y", ctypes.c_int32),
    ]


class WindowPlacement(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("show_cmd", ctypes.c_uint32),
        ("min_position", Point),
        ("max_position", Point),
        ("normal_position", Rect),
    ]


@dataclass(frozen=True)
class WindowCandidate:
    handle: int
    width: int
    height: int
    minimized: bool = False
    restore_width: int = 0
    restore_height: int = 0

    @property
    def area(self) -> int:
        width = self.restore_width if self.minimized else self.width
        height = self.restore_height if self.minimized else self.height
        return width * height

    @property
    def capture_size(self) -> tuple[int, int]:
        if self.minimized:
            return self.restore_width, self.restore_height
        return self.width, self.height


def is_capture_size_valid(width: int, height: int) -> bool:
    return 100 <= width <= 4096 and 100 <= height <= 4096


def select_capture_window(candidates: Iterable[WindowCandidate]) -> int | None:
    """Choose the largest usable WeChat conversation window.

    WeChat can expose a small Qt title-bar or notification window with the
    same title as a chat.  Those windows cannot contain receipt content and
    should not prevent the UI Automation observer from being used instead.
    """
    usable = [candidate for candidate in candidates if is_capture_size_valid(*candidate.capture_size)]
    return max(usable, key=lambda candidate: candidate.area).handle if usable else None


class WeChatOcrObserver:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.last_capture_digest: dict[str, str] = {}
        self.attached_handles: dict[str, int] = {}
        self.capture_warmup_until: dict[str, float] = {}
        self.capture_armed: set[str] = set()
        self.last_receipt_identity: dict[str, str] = {}
        self.restore_retry_at: dict[int, float] = {}
        self.restore_logged_handles: set[int] = set()
        self.rapid_ocr: Any | None = None
        self.rapid_ocr_failed = False
        self.available = os.name == "nt" and settings.tesseract_path.is_file() and settings.tessdata_dir.is_dir()
        if os.name != "nt":
            return
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def latest_receipts(self) -> Iterable[tuple[str, str]]:
        if not self.available:
            return
        for window_title in self.settings.windows:
            handle = self.find_window(window_title)
            if handle is None:
                self.attached_handles.pop(window_title, None)
                self.last_capture_digest.pop(window_title, None)
                self.capture_warmup_until.pop(window_title, None)
                self.capture_armed.discard(window_title)
                continue
            if not self.prepare_window_for_capture(handle):
                continue
            if self.attached_handles.get(window_title) != handle:
                if self.settings.background_window:
                    self.move_to_background(handle)
                logging.info("OCR observer attached window=%s handle=%s", window_title, handle)
                self.attached_handles[window_title] = handle
                self.last_capture_digest.pop(window_title, None)
                self.capture_armed.discard(window_title)
                self.capture_warmup_until[window_title] = time.monotonic() + max(4.0, self.settings.poll_interval * 2)
            image = self.capture_bmp(handle)
            digest = hashlib.sha256(image).hexdigest()
            previous_digest = self.last_capture_digest.get(window_title)
            self.last_capture_digest[window_title] = digest
            if window_title not in self.capture_armed:
                if time.monotonic() < self.capture_warmup_until.get(window_title, 0):
                    continue
                raw = self.read_text(window_title, image)
                receipt = parse_receipt(window_title, raw) if raw else None
                if receipt is not None:
                    self.last_receipt_identity[window_title] = str(receipt["receipt_identity"])
                self.capture_armed.add(window_title)
                logging.info("OCR observer armed window=%s", window_title)
                continue
            if previous_digest == digest:
                continue
            raw = self.read_text(window_title, image)
            if raw:
                receipt = parse_receipt(window_title, raw)
                if receipt is not None:
                    identity = str(receipt["receipt_identity"])
                    if self.last_receipt_identity.get(window_title) == identity:
                        continue
                    self.last_receipt_identity[window_title] = identity
                yield window_title, raw

    def has_candidate_window(self) -> bool:
        return self.available and any(self.find_window(title) is not None for title in self.settings.windows)

    def move_to_background(self, handle: int) -> None:
        hwnd_bottom = ctypes.c_void_p(1)
        swp_no_size = 0x0001
        swp_no_activate = 0x0010
        if not self.user32.SetWindowPos(
            handle,
            hwnd_bottom,
            self.settings.background_x,
            self.settings.background_y,
            0,
            0,
            swp_no_size | swp_no_activate,
        ):
            raise RuntimeError("SetWindowPos failed")

    def get_window_placement(self, handle: int) -> WindowPlacement | None:
        placement = WindowPlacement()
        placement.length = ctypes.sizeof(WindowPlacement)
        if not self.user32.GetWindowPlacement(handle, ctypes.byref(placement)):
            return None
        return placement

    def prepare_window_for_capture(self, handle: int) -> bool:
        if not self.user32.IsIconic(handle):
            self.restore_retry_at.pop(handle, None)
            return True
        now = time.monotonic()
        if now < self.restore_retry_at.get(handle, 0):
            return False
        self.restore_retry_at[handle] = now + max(5.0, self.settings.poll_interval * 2)
        placement = self.get_window_placement(handle)
        if placement is None:
            return False
        normal = placement.normal_position
        width = normal.right - normal.left
        height = normal.bottom - normal.top
        if not is_capture_size_valid(width, height):
            return False
        if self.settings.background_window:
            normal.left = self.settings.background_x
            normal.top = self.settings.background_y
            normal.right = normal.left + width
            normal.bottom = normal.top + height
        placement.flags = 0
        placement.show_cmd = 4  # SW_SHOWNOACTIVATE
        if not self.user32.SetWindowPlacement(handle, ctypes.byref(placement)):
            return False
        self.user32.ShowWindowAsync(handle, 4)
        if self.settings.background_window:
            self.move_to_background(handle)
        if handle not in self.restore_logged_handles:
            logging.info("restored minimized WeChat window handle=%s size=%dx%d", handle, width, height)
            self.restore_logged_handles.add(handle)
        return False

    def find_window(self, expected_title: str) -> int | None:
        matches: list[WindowCandidate] = []
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)

        def inspect(handle: int, _parameter: int) -> int:
            if not self.user32.IsWindowVisible(handle):
                return 1
            title_length = self.user32.GetWindowTextLengthW(handle)
            if title_length <= 0:
                return 1
            title = ctypes.create_unicode_buffer(title_length + 1)
            self.user32.GetWindowTextW(handle, title, title_length + 1)
            if title.value != expected_title:
                return 1
            class_name = ctypes.create_unicode_buffer(256)
            self.user32.GetClassNameW(handle, class_name, len(class_name))
            if not class_name.value.startswith(OCR_WECHAT_WINDOW_CLASS_PREFIX):
                return 1
            process_id = ctypes.c_uint32()
            self.user32.GetWindowThreadProcessId(handle, ctypes.byref(process_id))
            if self.process_name(process_id.value) == OCR_WECHAT_PROCESS_NAME:
                rect = Rect()
                if not self.user32.GetWindowRect(handle, ctypes.byref(rect)):
                    return 1
                minimized = bool(self.user32.IsIconic(handle))
                restore_width = 0
                restore_height = 0
                if minimized:
                    placement = self.get_window_placement(handle)
                    if placement is not None:
                        normal = placement.normal_position
                        restore_width = normal.right - normal.left
                        restore_height = normal.bottom - normal.top
                matches.append(
                    WindowCandidate(
                        handle=int(handle),
                        width=rect.right - rect.left,
                        height=rect.bottom - rect.top,
                        minimized=minimized,
                        restore_width=restore_width,
                        restore_height=restore_height,
                    )
                )
            return 1

        callback = callback_type(inspect)
        self.user32.EnumWindows(callback, 0)
        return select_capture_window(matches)

    def process_name(self, process_id: int) -> str:
        process_query_limited_information = 0x1000
        handle = self.kernel32.OpenProcess(process_query_limited_information, False, process_id)
        if not handle:
            return ""
        try:
            size = ctypes.c_uint32(1024)
            path = ctypes.create_unicode_buffer(size.value)
            if not self.kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
                return ""
            return Path(path.value).name.lower()
        finally:
            self.kernel32.CloseHandle(handle)

    def capture_bmp(self, handle: int) -> bytes:
        rect = Rect()
        if not self.user32.GetWindowRect(handle, ctypes.byref(rect)):
            raise RuntimeError("GetWindowRect failed")
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        if not is_capture_size_valid(width, height):
            raise RuntimeError(f"invalid WeChat window size {width}x{height}")
        window_dc = self.user32.GetWindowDC(handle)
        memory_dc = self.gdi32.CreateCompatibleDC(window_dc)
        bitmap = self.gdi32.CreateCompatibleBitmap(window_dc, width, height)
        previous = self.gdi32.SelectObject(memory_dc, bitmap)
        try:
            if not self.user32.PrintWindow(handle, memory_dc, 0x00000002):
                raise RuntimeError("PrintWindow failed")
            header = BitmapInfoHeader(
                ctypes.sizeof(BitmapInfoHeader),
                width,
                height,
                1,
                32,
                0,
                width * height * 4,
                0,
                0,
                0,
                0,
            )
            pixels = (ctypes.c_ubyte * header.size_image)()
            rows = self.gdi32.GetDIBits(
                memory_dc,
                bitmap,
                0,
                height,
                pixels,
                ctypes.byref(header),
                0,
            )
            if rows != height:
                raise RuntimeError("GetDIBits failed")
            file_size = 14 + ctypes.sizeof(header) + header.size_image
            file_header = struct.pack("<2sIHHI", b"BM", file_size, 0, 0, 14 + ctypes.sizeof(header))
            return file_header + bytes(header) + bytes(pixels)
        finally:
            self.gdi32.SelectObject(memory_dc, previous)
            self.gdi32.DeleteObject(bitmap)
            self.gdi32.DeleteDC(memory_dc)
            self.user32.ReleaseDC(handle, window_dc)

    def read_text(self, window_title: str, image: bytes) -> str:
        rapid_text = self.read_rapid_text(image)
        rapid_receipt = parse_receipt(window_title, rapid_text) if rapid_text else None
        if rapid_receipt is None and "收款" not in rapid_text and "金额" not in rapid_text:
            return rapid_text

        tesseract_text = self.read_tesseract_text(image)
        tesseract_receipt = parse_receipt(window_title, tesseract_text) if tesseract_text else None
        if rapid_receipt is not None and tesseract_receipt is not None:
            if rapid_receipt["amount_cents"] != tesseract_receipt["amount_cents"]:
                logging.warning("OCR engines disagreed on receipt amount window=%s", window_title)
                return ""
            return rapid_text
        if rapid_receipt is not None:
            tesseract_amounts = {
                money_to_cents(value)
                for value in re.findall(r"[￥¥]\s*(\d+(?:\.\d{1,2})?)", tesseract_text)
            }
            if tesseract_amounts and rapid_receipt["amount_cents"] not in tesseract_amounts:
                logging.warning("Tesseract did not confirm RapidOCR receipt amount window=%s", window_title)
                return ""
            return rapid_text
        return rapid_text if rapid_receipt is not None else tesseract_text

    def read_rapid_text(self, image: bytes) -> str:
        if self.rapid_ocr_failed:
            return ""
        try:
            if self.rapid_ocr is None:
                from rapidocr_onnxruntime import RapidOCR

                self.rapid_ocr = RapidOCR()
            result, _elapsed = self.rapid_ocr(image)
            return "\n".join(str(item[1]) for item in (result or [])).strip()
        except Exception as exc:
            self.rapid_ocr_failed = True
            logging.warning("RapidOCR unavailable, using Tesseract fallback error=%s", str(exc)[:300])
            return ""

    def read_tesseract_text(self, image: bytes) -> str:
        command = [
            str(self.settings.tesseract_path),
            "stdin",
            "stdout",
            "--tessdata-dir",
            str(self.settings.tessdata_dir),
            "-l",
            "chi_sim+eng",
            "--psm",
            "6",
        ]
        completed = subprocess.run(
            command,
            input=image,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
            creationflags=0x08000000,
        )
        if completed.returncode != 0:
            error = completed.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Tesseract failed: {error[:300]}")
        return completed.stdout.decode("utf-8", errors="replace").strip()


class WeChatObserver:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.uia = WeChatUiObserver(settings.windows)
        self.ocr = WeChatOcrObserver(settings)
        if settings.observer_mode == "ocr" and not self.ocr.available:
            raise ValueError("OCR observer requires Tesseract with chi_sim language data")

    def latest_receipts(self) -> Iterable[tuple[str, str]]:
        if self.settings.observer_mode == "uia":
            yield from self.uia.latest_receipts()
            return
        if self.settings.observer_mode == "ocr" or self.ocr.has_candidate_window():
            yield from self.ocr.latest_receipts()
            return
        yield from self.uia.latest_receipts()


def lower_process_priority() -> None:
    if os.name != "nt":
        return
    import ctypes

    below_normal_priority_class = 0x00004000
    handle = ctypes.windll.kernel32.GetCurrentProcess()
    ctypes.windll.kernel32.SetPriorityClass(handle, below_normal_priority_class)


def configure_logging(state_dir: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(state_dir / "watcher.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    default_config = Path(os.path.expandvars(r"%PROGRAMDATA%\SynthPay\wechat-watcher.ini"))
    config_path = Path(argv[0]) if argv else default_config
    settings = Settings.load(config_path)
    configure_logging(settings.state_dir)
    lower_process_priority()
    store = EventStore(settings.state_dir / "events.sqlite3")
    wake = threading.Event()
    worker = CallbackWorker(settings, store, wake)
    observer = WeChatObserver(settings)
    stopping = threading.Event()

    def stop(_signum: int, _frame: object) -> None:
        stopping.set()
        worker.stop()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    worker.start()
    logging.info("watcher started dry_run=%s windows=%s", settings.dry_run, ",".join(settings.windows))
    while not stopping.wait(settings.poll_interval):
        try:
            for window_title, raw_text in observer.latest_receipts():
                receipt = parse_receipt(window_title, raw_text)
                if receipt is None:
                    continue
                event_id = make_event_id(window_title, receipt)
                observed_at = str(receipt["observed_at"])
                content = json.dumps(
                    {
                        "title": window_title,
                        "msg": f"收款到账 {receipt['money']} 元",
                        "receipt_type": receipt["channel"],
                        "receipt_digest": receipt["raw_digest"],
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                payload = {
                    "timestamp": observed_at,
                    "observed_at": observed_at,
                    "content": content,
                    "from": "com.tencent.mm",
                    "source": "windows_wechat_ui",
                    "event_id": event_id,
                    "sign_version": "2",
                    "sign": sign_payload(
                        observed_at,
                        content,
                        event_id,
                        observed_at,
                        settings.callback_secret,
                    ),
                }
                if store.enqueue(event_id, payload):
                    logging.info("receipt queued event_id=%s amount_cents=%d", event_id[:12], receipt["amount_cents"])
                    wake.set()
        except Exception:
            logging.exception("WeChat UI observation failed")
    worker.join(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
