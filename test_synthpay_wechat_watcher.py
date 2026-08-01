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

from synthpay_wechat_watcher import (  # noqa: E402
    make_event_id,
    money_to_cents,
    parse_receipt,
    Settings,
    sign_payload,
)


class WindowsWatcherUnitTest(unittest.TestCase):
    def test_personal_receipt(self) -> None:
        raw = "个人收款服务 收款到账通知08月01日 12:30收款金额￥0.01汇总今日第1笔收款备注"
        receipt = parse_receipt("微信收款助手", raw, datetime(2026, 8, 1, 12, 31, 0))
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["channel"], "1")
        self.assertEqual(receipt["amount_cents"], 1)
        self.assertEqual(receipt["observed_at"], 1785558600000)

    def test_merchant_receipt(self) -> None:
        raw = "收款通知08月01日 12:30:45收款金额：￥12.34订单金额￥12.34交易单号420001收款门店测试"
        receipt = parse_receipt("微信商家助手", raw, datetime(2026, 8, 1, 12, 31, 0))
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["channel"], "4")
        self.assertEqual(receipt["amount_cents"], 1234)

    def test_unrelated_message_is_ignored(self) -> None:
        self.assertIsNone(parse_receipt("微信收款助手", "欢迎使用微信收款助手"))

    def test_ocr_text_can_include_window_chrome(self) -> None:
        raw = "微信支付 聊天信息 个人收款服务 收款到账通知08月01日 12:30收款金额￥0.01"
        receipt = parse_receipt("微信收款助手", raw, datetime(2026, 8, 1, 12, 31, 0))
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["amount_cents"], 1)
        self.assertEqual(receipt["observed_at"], 1785558600000)

    def test_money_conversion(self) -> None:
        self.assertEqual(money_to_cents("10"), 1000)
        self.assertEqual(money_to_cents("10.1"), 1010)
        self.assertEqual(money_to_cents("10.01"), 1001)

    def test_event_id_is_stable(self) -> None:
        receipt = {
            "channel": "1",
            "amount_cents": 1,
            "observed_at": 1785558600000,
            "raw_digest": "a" * 64,
        }
        self.assertEqual(make_event_id("微信收款助手", receipt), make_event_id("微信收款助手", receipt))

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
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "watcher.ini"
            config_path.write_text(
                "[watcher]\n"
                "dry_run = 1\n"
                "state_dir = %LOCALAPPDATA%\\SynthPay\\wechat-watcher\\state\n",
                encoding="utf-8",
            )
            settings = Settings.load(config_path)

        self.assertNotIn("%LOCALAPPDATA%", str(settings.state_dir))
        self.assertTrue(str(settings.state_dir).endswith(os.path.join("SynthPay", "wechat-watcher", "state")))
        self.assertFalse(settings.use_system_proxy)
        self.assertEqual(settings.observer_mode, "auto")
        self.assertTrue(settings.background_window)


if __name__ == "__main__":
    unittest.main()
