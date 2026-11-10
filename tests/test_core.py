import unittest

from core import Settings, extract_links, list_telegram_ids, site_allowlist, valid_url


class CoreTests(unittest.TestCase):
    def test_telegram_ids_numbered_only_and_unique(self):
        ids = list_telegram_ids({"TELEGRAM_ID_1": " 123456789 ", "telegram_id_2": "456", "TELEGRAM_ID_3": "123456789", "TELEGRAM_ID_0": "17", "TELEGRAM_ID_A": "15"})
        self.assertEqual(ids, frozenset({123456789, 456}))

    def test_invalid_telegram_ids_are_rejected(self):
        for bad in ("", "  ", "0", "-1", "+12", "abc", "123.4", "１２３", str(2**63), "1" * 60):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                list_telegram_ids({"TELEGRAM_ID_1": bad})

    def test_hostname_spoofing_http_credentials_and_ip_blocked(self):
        domains = ("youtube.com", "instagram.com")
        bad = [
            "https://youtube.com.attacker.test/watch?v=1", "https://127.0.0.1/a",
            "https://user:pass@youtube.com/x", "https://youtube.com:444/x", "file:///etc/passwd",
            "https://youtube.com.evil.org", "https://localhost/a",
        ]
        for url in bad:
            with self.subTest(url=url):
                self.assertFalse(valid_url(url, domains))
        self.assertTrue(valid_url("https://m.youtube.com/watch?v=123", domains))
        self.assertTrue(valid_url("https://example.org/watch?v=123", ()))
        self.assertFalse(valid_url("http://example.org:8080/a", ()))
        self.assertFalse(valid_url("https://[::1]/a", ()))
        self.assertFalse(valid_url("https://2130706433/a", ()))
        self.assertFalse(valid_url("https://0x7f000001/a", ()))
        self.assertFalse(valid_url("https://foo.railway.internal/a", ()))

    def test_two_links_are_kept_in_message_order(self):
        first = "https://youtu.be/abc"
        second = "https://www.tiktok.com/@abc/video/123"
        links, rejected = extract_links(f"Видео: {first} {second}", site_allowlist({}))
        self.assertEqual(links, [first, second])
        self.assertEqual(rejected, 0)

    def test_reject_bad_links_and_limit_ten(self):
        links, rejected = extract_links(" ".join(["https://youtu.be/1"] * 12) + " http://youtube.com/x", site_allowlist({}))
        self.assertEqual(len(links), 2)
        self.assertEqual(rejected, 0)

    def test_allowed_site_customization(self):
        self.assertEqual(site_allowlist({"ALLOWED_DOMAINS": "vimeo.com, Example.org"}), ("vimeo.com", "example.org"))
        with self.assertRaises(ValueError):
            site_allowlist({"ALLOWED_DOMAINS": "*"})

    def test_configuration_modes_and_limit(self):
        env = {
            "BOT_TOKEN": "123456789:" + "x" * 35,
            "TELEGRAM_ID_1": "123456789",
            "WEBHOOK_SECRET": "x" * 32,
            "WEBHOOK_BASE_URL": "https://example.up.railway.app",
        }
        settings = Settings.from_env(env)
        self.assertEqual(settings.allowed_user_ids, frozenset({123456789}))
        self.assertEqual(settings.mode, "eco")
        self.assertEqual(settings.max_file_mb, 49)
        self.assertEqual(settings.webhook_url, "https://example.up.railway.app/telegram-webhook")
        with self.assertRaises(ValueError):
            Settings.from_env(env | {"MAX_FILE_MB": "50"})
        large = Settings.from_env(env | {"BOT_MODE": "large", "TELEGRAM_API_BASE": "http://telegram-api.railway.internal:8081"})
        self.assertEqual(large.max_file_mb, 1950)
        with self.assertRaises(ValueError):
            Settings.from_env(env | {"BOT_MODE": "large", "TELEGRAM_API_BASE": "https://api.telegram.org"})

    def test_missing_secret_and_telegram_id_fails_fast(self):
        env = {"BOT_TOKEN": "123456789:" + "x" * 35, "WEBHOOK_BASE_URL": "https://test.railway.app", "WEBHOOK_SECRET": "x" * 32}
        with self.assertRaises(ValueError):
            Settings.from_env(env)
        with self.assertRaises(ValueError):
            Settings.from_env(env | {"TELEGRAM_ID_1": "123456789", "WEBHOOK_SECRET": "abc"})

    def test_cookie_base64_and_resource_limits_are_validated(self):
        env = {
            "BOT_TOKEN": "123456789:" + "x" * 35,
            "TELEGRAM_ID_1": "123456789",
            "WEBHOOK_SECRET": "x" * 32,
            "WEBHOOK_BASE_URL": "https://example.up.railway.app",
        }
        settings = Settings.from_env(env | {"COOKIES_B64": "bmV2ZXItdHJ1c3QtY29va2llcw==", "MAX_ITEMS": "5", "RATE_LIMIT_PER_MIN": "8"})
        self.assertEqual(settings.max_items, 5)
        self.assertEqual(settings.rate_limit_per_min, 8)
        with self.assertRaisesRegex(ValueError, "COOKIES_B64"):
            Settings.from_env(env | {"COOKIES_B64": "not base64"})
        with self.assertRaisesRegex(ValueError, "MAX_ITEMS"):
            Settings.from_env(env | {"MAX_ITEMS": "11"})


if __name__ == "__main__":
    unittest.main()
