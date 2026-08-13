#!/usr/bin/env python3

import base64
import hashlib
import hmac
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import synthpay_wechat_watcher as watcher  # noqa: E402

from synthpay_wechat_watcher import (  # noqa: E402
    make_event_id,
    money_to_cents,
    parse_receipt,
    Settings,
    sign_payload,
    WindowCandidate,
    select_capture_window,
    expand_windows_environment,
)


class WindowsWatcherUnitTest(unittest.TestCase):
    def test_personal_receipt(self) -> None:
        raw = "个人收款服务 收款到账通知08月01日 12:30收款金额￥0.01汇总今日第1笔收款备注"
        captured_at = datetime(2026, 8, 1, 12, 31, 45)
        receipt = parse_receipt("微信收款助手", raw, captured_at)
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["channel"], "1")
        self.assertEqual(receipt["amount_cents"], 1)
        self.assertEqual(receipt["observed_at"], int(captured_at.timestamp() * 1000))

    def test_merchant_receipt(self) -> None:
        raw = "收款通知08月01日 12:30:45收款金额：￥12.34订单金额￥12.34交易单号420001收款门店测试"
        receipt = parse_receipt("微信商家助手", raw, datetime(2026, 8, 1, 12, 31, 0))
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["channel"], "4")
        self.assertEqual(receipt["amount_cents"], 1234)
        self.assertEqual(receipt["observed_at"], int(datetime(2026, 8, 1, 12, 30, 45).timestamp() * 1000))

    def test_unrelated_message_is_ignored(self) -> None:
        self.assertIsNone(parse_receipt("微信收款助手", "欢迎使用微信收款助手"))

    def test_ocr_text_can_include_window_chrome(self) -> None:
        raw = "微信支付 聊天信息 个人收款服务 收款到账通知08月01日 12:30收款金额￥0.01"
        captured_at = datetime(2026, 8, 1, 12, 31, 45)
        receipt = parse_receipt("微信收款助手", raw, captured_at)
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["amount_cents"], 1)
        self.assertEqual(receipt["observed_at"], int(captured_at.timestamp() * 1000))

    def test_money_conversion(self) -> None:
        self.assertEqual(money_to_cents("10"), 1000)
        self.assertEqual(money_to_cents("10.1"), 1010)
        self.assertEqual(money_to_cents("10.01"), 1001)

    def test_window_selection_ignores_compact_chrome(self) -> None:
        candidates = (
            WindowCandidate(handle=1, width=160, height=28),
            WindowCandidate(handle=2, width=760, height=540),
            WindowCandidate(handle=3, width=640, height=480),
        )
        self.assertEqual(select_capture_window(candidates), 2)

    def test_window_selection_returns_none_without_capture_area(self) -> None:
        candidates = (WindowCandidate(handle=1, width=160, height=28),)
        self.assertIsNone(select_capture_window(candidates))

    def test_window_selection_uses_minimized_restore_size(self) -> None:
        candidates = (
            WindowCandidate(
                handle=1,
                width=160,
                height=28,
                minimized=True,
                restore_width=614,
                restore_height=648,
            ),
        )
        self.assertEqual(select_capture_window(candidates), 1)

    def test_event_id_is_stable(self) -> None:
        receipt = {
            "channel": "1",
            "amount_cents": 1,
            "observed_at": 1785558600000,
            "raw_digest": "a" * 64,
        }
        self.assertEqual(make_event_id("微信收款助手", receipt), make_event_id("微信收款助手", receipt))

    def test_minute_receipt_identity_survives_recapture(self) -> None:
        raw = "个人收款服务 收款到账通知08月04日16:10收款金额￥5.00"
        first = parse_receipt("微信收款助手", raw, datetime(2026, 8, 4, 16, 10, 18))
        second = parse_receipt("微信收款助手", raw, datetime(2026, 8, 4, 16, 11, 5))
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertNotEqual(first["observed_at"], second["observed_at"])
        self.assertEqual(first["receipt_identity"], second["receipt_identity"])
        self.assertEqual(make_event_id("微信收款助手", first), make_event_id("微信收款助手", second))

    def test_signature_matches_backend(self) -> None:
        timestamp = "1785558600123"
        observed_at = "1785558600000"
        content = '{"title":"微信收款助手","msg":"收款到账 0.01 元"}'
        event_id = "b" * 64
        secret = "test-secret"
        expected = base64.b64encode(
            hmac.new(
                secret.encode(),
                f"{timestamp}\n{content}\n{event_id}\n{observed_at}".encode(),
                hashlib.sha256,
            ).digest()
        ).decode()
        self.assertEqual(sign_payload(timestamp, content, event_id, observed_at, secret), expected)

    def test_config_expands_windows_environment_variable(self) -> None:
        previous = os.environ.get("LOCALAPPDATA")
        os.environ["LOCALAPPDATA"] = "C:\\Users\\test\\AppData\\Local"
        try:
            with tempfile.TemporaryDirectory() as directory:
                config_path = Path(directory) / "watcher.ini"
                config_path.write_text(
                    "[watcher]\n"
                    "dry_run = 1\n"
                    "state_dir = %LOCALAPPDATA%\\SynthPay\\wechat-watcher\\state\n",
                    encoding="utf-8",
                )
                settings = Settings.load(config_path)
        finally:
            if previous is None:
                del os.environ["LOCALAPPDATA"]
            else:
                os.environ["LOCALAPPDATA"] = previous

        self.assertEqual(str(settings.state_dir), "C:\\Users\\test\\AppData\\Local\\SynthPay\\wechat-watcher\\state")
        self.assertFalse(settings.use_system_proxy)
        self.assertEqual(settings.observer_mode, "auto")
        self.assertTrue(settings.background_window)

    def test_environment_expansion_preserves_unknown_percent_variable(self) -> None:
        self.assertEqual(expand_windows_environment("%MISSING_TEST_VALUE%\\state"), "%MISSING_TEST_VALUE%\\state")


class TesseractHealthTest(unittest.TestCase):
    def test_missing_model_is_reported_without_running_tesseract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            ready, reason = watcher.check_tesseract_languages(
                Path(temporary_directory) / "missing.exe",
                Path(temporary_directory),
            )
        self.assertFalse(ready)
        self.assertIn("executable not found", reason)

    def test_tesseract_fallback_runs_when_rapidocr_returns_no_text(self) -> None:
        observer = object.__new__(watcher.WeChatOcrObserver)
        observer.tesseract_available = True
        observer.read_rapid_text = lambda _image: ""
        observer.read_tesseract_text = lambda _image: "Tesseract fallback text"

        self.assertEqual(observer.read_text("微信收款助手", b"image"), "Tesseract fallback text")

    def test_rapidocr_text_is_retained_when_tesseract_is_unavailable(self) -> None:
        observer = object.__new__(watcher.WeChatOcrObserver)
        observer.tesseract_available = False
        observer.tesseract_status = "missing model files: chi_sim"
        observer.tesseract_fallback_logged = False
        observer.read_rapid_text = lambda _image: "no receipt yet"

        self.assertEqual(observer.read_text("微信收款助手", b"image"), "no receipt yet")
        self.assertTrue(observer.tesseract_fallback_logged)


@unittest.skipUnless(os.name == "nt", "Windows mutex test")
class InstanceLockTest(unittest.TestCase):
    def test_second_lock_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "watcher.ini"
            first = watcher.WatcherInstanceLock(config_path)
            second = watcher.WatcherInstanceLock(config_path)
            self.assertTrue(first.acquire())
            try:
                self.assertFalse(second.acquire())
            finally:
                second.close()
                first.close()


if __name__ == "__main__":
    unittest.main()
