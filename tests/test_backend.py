"""Настройки, ключи, модель Hermes, свой сервер, навык, автозапуск."""
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


from jarvis import backend, paths


def hermes_dir() -> Path:
    return Path(os.environ["HERMES_HOME"])


class FakeOpenAI:
    """Мини-сервер, похожий на OpenAI-совместимый: проверяет ключ и модель."""

    def __init__(self, key="sk-good", models=("gpt-x", "claude-y"), balance=True):
        outer = self
        self.key, self.models, self.balance = key, list(models), balance
        self.requests = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _auth(self):
                if self.headers.get("Authorization") != f"Bearer {outer.key}":
                    self._send(401, {"error": {"message": "Invalid API key"}})
                    return False
                return True

            def do_GET(self):
                outer.requests.append(("GET", self.path, None))
                if not self._auth():
                    return
                if self.path == "/v1/models":
                    self._send(200, {"data": [{"id": m} for m in outer.models]})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(("POST", self.path, body))
                if not self._auth():
                    return
                if self.path != "/v1/chat/completions":
                    return self._send(404, {"error": {"message": "no route"}})
                if not outer.balance:
                    return self._send(402, {"error": {"message": "Insufficient balance"}})
                if body.get("model") not in outer.models:
                    return self._send(400, {"error": {"message": f"model {body.get('model')} does not exist"}})
                self._send(200, {"choices": [{"message": {"content": "Готов."}}]})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class EnvFileTests(unittest.TestCase):
    def setUp(self):
        self.path = hermes_dir() / ".env"

    def test_roundtrip_and_masking(self):
        backend.write_env_value(self.path, "DEEPSEEK_API_KEY", "sk-abcdef123456")
        self.assertEqual(backend.read_env_file(self.path)["DEEPSEEK_API_KEY"], "sk-abcdef123456")
        m = backend.mask("sk-abcdef123456")
        self.assertEqual(m, {"set": True, "tail": "3456"})
        self.assertEqual(backend.mask(""), {"set": False, "tail": ""})
        self.assertEqual(backend.mask("short"), {"set": True, "tail": ""})   # короткий ключ не раскрываем

    def test_keeps_other_lines_and_crlf(self):
        self.path.write_bytes(b"# comment\r\nOTHER=1\r\nDEEPSEEK_API_KEY=old\r\n")
        backend.write_env_value(self.path, "DEEPSEEK_API_KEY", "new")
        raw = self.path.read_bytes()
        self.assertIn(b"\r\n", raw)
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
        env = backend.read_env_file(self.path)
        self.assertEqual(env, {"OTHER": "1", "DEEPSEEK_API_KEY": "new"})
        self.assertIn(b"# comment", raw)

    def test_backup_made_once(self):
        self.path.write_text("A=1\n", encoding="utf-8")
        backend.write_env_value(self.path, "OPENAI_API_KEY", "x1")
        bak = self.path.with_name(".env.jarvis.bak")
        self.assertEqual(bak.read_text(encoding="utf-8"), "A=1\n")
        backend.write_env_value(self.path, "OPENAI_API_KEY", "x2")
        self.assertEqual(bak.read_text(encoding="utf-8"), "A=1\n")

    def test_delete_and_export_prefix(self):
        self.path.write_text("export KEY_ONE=a\nKEY_TWO=b\n", encoding="utf-8")
        backend.write_env_value(self.path, "KEY_ONE", None)
        self.assertEqual(backend.read_env_file(self.path), {"KEY_TWO": "b"})

    def test_quotes_stripped_on_read(self):
        self.path.write_text('A="hello"\nB=\'x y\'\n', encoding="utf-8")
        self.assertEqual(backend.read_env_file(self.path), {"A": "hello", "B": "x y"})

    def test_rejects_bad_names_and_newlines(self):
        with self.assertRaises(ValueError):
            backend.write_env_value(self.path, "bad name", "x")
        with self.assertRaises(ValueError):
            backend.write_env_value(self.path, "GOOD_NAME", "a\nINJECT=1")

    def test_bom_and_missing_file(self):
        self.assertEqual(backend.read_env_file(self.path), {})
        self.path.write_bytes(b"\xef\xbb\xbfA=1\n")
        self.assertEqual(backend.read_env_file(self.path), {"A": "1"})


class HermesConfigTests(unittest.TestCase):
    def setUp(self):
        self.cfg = hermes_dir() / "config.yaml"

    def test_read_nothing(self):
        self.assertEqual(backend.read_hermes_model(hermes_dir())["model"], "")

    def test_read_block(self):
        self.cfg.write_text('model:\n  provider: "deepseek"\n  default: deepseek-chat\nother: 1\n', encoding="utf-8")
        got = backend.read_hermes_model(hermes_dir())
        self.assertEqual((got["provider"], got["model"]), ("deepseek", "deepseek-chat"))

    def test_read_scalar_form(self):
        self.cfg.write_text("model: some/model\nagent:\n  x: 1\n", encoding="utf-8")
        self.assertEqual(backend.read_hermes_model(hermes_dir())["model"], "some/model")

    def test_write_replaces_only_model_block(self):
        self.cfg.write_text("agent:\n  max_turns: 90\nmodel:\n  provider: old\n  default: old-m\n\nterminal:\n  backend: local\n",
                            encoding="utf-8")
        backend.write_hermes_model(hermes_dir(), "custom", "gpt-x", base_url="http://h/v1", key_env="K")
        text = self.cfg.read_text(encoding="utf-8")
        self.assertIn("max_turns: 90", text)
        self.assertIn("backend: local", text)
        self.assertNotIn("old-m", text)
        got = backend.read_hermes_model(hermes_dir())
        self.assertEqual(got, {"provider": "custom", "model": "gpt-x", "base_url": "http://h/v1", "key_env": "K"})
        self.assertEqual(text.count("model:"), 1)

    def test_write_appends_when_absent_and_keeps_crlf(self):
        self.cfg.write_bytes(b"agent:\r\n  x: 1\r\n")
        backend.write_hermes_model(hermes_dir(), "custom", "m", base_url="http://h/v1", key_env="K")
        raw = self.cfg.read_bytes()
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
        self.assertIn(b"x: 1", raw)
        self.assertEqual(backend.read_hermes_model(hermes_dir())["model"], "m")

    def test_write_creates_file_and_backup_once(self):
        backend.write_hermes_model(hermes_dir(), "custom", "m", base_url="http://h/v1", key_env="K")
        self.assertTrue(self.cfg.exists())
        self.cfg.write_text("agent: 1\n", encoding="utf-8")
        backend.write_hermes_model(hermes_dir(), "custom", "m2", base_url="http://h/v1", key_env="K")
        self.assertEqual(self.cfg.with_name("config.yaml.jarvis.bak").read_text(encoding="utf-8"), "agent: 1\n")

    def test_values_with_special_characters_are_quoted(self):
        backend.write_hermes_model(hermes_dir(), "custom", "model: with #hash", base_url="http://h/v1", key_env="K")
        text = self.cfg.read_text(encoding="utf-8")
        self.assertIn('default: "model: with #hash"', text)


class UrlTests(unittest.TestCase):
    def test_normalize(self):
        n = backend.normalize_base_url
        self.assertEqual(n("https://x.ru/v1/"), "https://x.ru/v1")
        self.assertEqual(n(" https://x.ru/v1/chat/completions "), "https://x.ru/v1")
        self.assertEqual(n("http://localhost:8080"), "http://localhost:8080")

    def test_rejects_garbage(self):
        for bad in ("", "x.ru/v1", "ftp://x", "https://", "просто текст"):
            with self.assertRaises(ValueError, msg=bad):
                backend.normalize_base_url(bad)


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.srv = FakeOpenAI()
        self.addCleanup(self.srv.close)

    def test_probe_ok(self):
        self.assertEqual(backend.probe_endpoint(self.srv.url, "sk-good", "gpt-x"), "Готов.")
        method, path, body = self.srv.requests[-1]
        self.assertEqual((method, path, body["model"]), ("POST", "/v1/chat/completions", "gpt-x"))

    def test_probe_accepts_pasted_full_chat_url(self):
        self.assertEqual(backend.probe_endpoint(self.srv.url + "/chat/completions", "sk-good", "gpt-x"), "Готов.")

    def test_probe_wrong_key(self):
        with self.assertRaises(RuntimeError) as cm:
            backend.probe_endpoint(self.srv.url, "sk-bad", "gpt-x")
        self.assertIn("не принял ключ", str(cm.exception))

    def test_probe_wrong_model(self):
        with self.assertRaises(RuntimeError) as cm:
            backend.probe_endpoint(self.srv.url, "sk-good", "nope")
        self.assertIn("названи", str(cm.exception))

    def test_probe_wrong_path_hints_v1(self):
        base = self.srv.url.rsplit("/v1", 1)[0]
        with self.assertRaises(RuntimeError) as cm:
            backend.probe_endpoint(base, "sk-good", "gpt-x")
        self.assertIn("/v1", str(cm.exception))

    def test_probe_no_balance(self):
        self.srv.balance = False
        with self.assertRaises(RuntimeError) as cm:
            backend.probe_endpoint(self.srv.url, "sk-good", "gpt-x")
        self.assertIn("средства", str(cm.exception))

    def test_probe_unreachable(self):
        with self.assertRaises(RuntimeError) as cm:
            backend.probe_endpoint("http://127.0.0.1:9/v1", "k", "m")
        self.assertIn("нет связи", str(cm.exception))

    def test_probe_requires_key_and_model(self):
        with self.assertRaises(RuntimeError):
            backend.probe_endpoint(self.srv.url, "", "m")
        with self.assertRaises(RuntimeError):
            backend.probe_endpoint(self.srv.url, "k", "")

    def test_list_models(self):
        self.assertEqual(backend.list_endpoint_models(self.srv.url, "sk-good"), ["claude-y", "gpt-x"])

    def test_list_models_bad_key(self):
        with self.assertRaises(RuntimeError):
            backend.list_endpoint_models(self.srv.url, "bad")

    def test_key_echoed_by_server_is_scrubbed(self):
        srv = self.srv

        orig = srv.server.RequestHandlerClass._send

        def echo(handler, code, body):
            if code == 401:
                body = {"error": {"message": "Incorrect API key provided: sk-ECHOED-key"}}
            orig(handler, code, body)
        srv.server.RequestHandlerClass._send = echo
        with self.assertRaises(RuntimeError) as cm:
            backend.probe_endpoint(srv.url, "sk-ECHOED-key", "gpt-x")
        self.assertNotIn("ECHOED", str(cm.exception))

    def test_key_never_in_error_text(self):
        with self.assertRaises(RuntimeError) as cm:
            backend.probe_endpoint(self.srv.url, "sk-SECRET-bad", "gpt-x")
        self.assertNotIn("SECRET", str(cm.exception))


class SaveModelTests(unittest.TestCase):
    def setUp(self):
        self.env = hermes_dir() / ".env"

    def test_custom_server_writes_config_env_and_clears_overrides(self):
        backend.save_settings({"hermes": {"provider": "deepseek", "model": "deepseek-chat"}})
        backend.save_model_choice("custom", "gpt-x", api_key="sk-good", base_url="https://api.example.ru/v1/")
        now = backend.read_hermes_model(hermes_dir())
        self.assertEqual(now["provider"], "custom")
        self.assertEqual(now["model"], "gpt-x")
        self.assertEqual(now["base_url"], "https://api.example.ru/v1")
        self.assertEqual(now["key_env"], backend.CUSTOM_KEY_ENV)
        self.assertEqual(backend.read_env_file(self.env)[backend.CUSTOM_KEY_ENV], "sk-good")
        # ключ не лежит в config.yaml
        self.assertNotIn("sk-good", (hermes_dir() / "config.yaml").read_text(encoding="utf-8"))
        # Джарвис больше не переопределяет провайдера и модель
        s = backend.read_user_settings()["hermes"]
        self.assertEqual((s["provider"], s["model"]), ("", ""))

    def test_custom_keeps_old_key_when_not_retyped(self):
        backend.save_model_choice("custom", "gpt-x", api_key="sk-good", base_url="https://a.ru/v1")
        backend.save_model_choice("custom", "other-model", base_url="https://a.ru/v1")
        self.assertEqual(backend.read_env_file(self.env)[backend.CUSTOM_KEY_ENV], "sk-good")
        self.assertEqual(backend.read_hermes_model(hermes_dir())["model"], "other-model")

    def test_custom_requires_key_url_model(self):
        with self.assertRaises(ValueError):
            backend.save_model_choice("custom", "m", api_key="k", base_url="не адрес")
        with self.assertRaises(ValueError):
            backend.save_model_choice("custom", "", api_key="k", base_url="https://a.ru/v1")
        with self.assertRaises(ValueError):
            backend.save_model_choice("custom", "m", base_url="https://a.ru/v1")   # ключа ещё нет

    def test_failed_validation_changes_nothing(self):
        try:
            backend.save_model_choice("custom", "", api_key="k", base_url="https://a.ru/v1")
        except ValueError:
            pass
        self.assertFalse(self.env.exists())
        self.assertFalse((hermes_dir() / "config.yaml").exists())

    def test_builtin_provider_saves_key_and_override(self):
        backend.save_model_choice("deepseek", "deepseek-chat", api_key="sk-ds")
        self.assertEqual(backend.read_env_file(self.env)["DEEPSEEK_API_KEY"], "sk-ds")
        s = backend.read_user_settings()["hermes"]
        self.assertEqual((s["provider"], s["model"]), ("deepseek", "deepseek-chat"))

    def test_builtin_provider_clear_key(self):
        backend.save_model_choice("deepseek", "deepseek-chat", api_key="sk-ds")
        backend.save_model_choice("deepseek", "deepseek-chat", clear_key=True)
        self.assertNotIn("DEEPSEEK_API_KEY", backend.read_env_file(self.env))

    def test_unknown_provider_rejected(self):
        with self.assertRaises(ValueError):
            backend.save_model_choice("evil; rm -rf", "m")

    def test_other_provider(self):
        backend.save_model_choice("__other__", "m1", api_key="k1", custom_provider="my-prov", custom_env="MY_PROV_KEY")
        self.assertEqual(backend.read_env_file(self.env)["MY_PROV_KEY"], "k1")
        self.assertEqual(backend.read_user_settings()["hermes"]["provider"], "my-prov")
        with self.assertRaises(ValueError):
            backend.save_model_choice("__other__", "m", custom_provider="x y", custom_env="A_B")
        with self.assertRaises(ValueError):
            backend.save_model_choice("__other__", "m", custom_provider="x", custom_env="bad env")

    def test_empty_provider_means_follow_hermes(self):
        backend.save_model_choice("deepseek", "deepseek-chat")
        backend.save_model_choice("", "")
        s = backend.read_user_settings()["hermes"]
        self.assertEqual((s["provider"], s["model"]), ("", ""))

    def test_snapshot_never_returns_full_keys(self):
        backend.save_model_choice("custom", "gpt-x", api_key="sk-very-secret-1234", base_url="https://a.ru/v1")
        snap = backend.hermes_snapshot()
        self.assertEqual(snap["custom_key"], {"set": True, "tail": "1234"})
        self.assertNotIn("very-secret", json.dumps(snap))
        self.assertEqual(snap["now"]["provider"], "custom")


class TelegramTests(unittest.TestCase):
    def test_save_and_validate(self):
        backend.save_telegram("123:ABC", "111, 222")
        env = backend.read_env_file(hermes_dir() / ".env")
        self.assertEqual(env["TELEGRAM_BOT_TOKEN"], "123:ABC")
        self.assertEqual(env["TELEGRAM_ALLOWED_USERS"], "111,222")
        with self.assertRaises(ValueError):
            backend.save_telegram(None, "abc")
        backend.save_telegram(None, "", clear_token=True)
        env = backend.read_env_file(hermes_dir() / ".env")
        self.assertNotIn("TELEGRAM_BOT_TOKEN", env)
        self.assertNotIn("TELEGRAM_ALLOWED_USERS", env)


class SettingsTests(unittest.TestCase):
    def test_sanitize_drops_unknown_and_clamps(self):
        out = backend.sanitize_settings({
            "city": "  Казань ", "evil": "x", "wake": {"followup_seconds": 999, "words": ["Джарвис", " ", "ЖАРВИС"]},
            "audio": {"silence_threshold": 5, "device": 3}, "stt": {"model": "huge"},
            "tts": {"voice": "ru-RU-DmitryNeural", "rate": "+10%", "pitch": "bad", "backend": "elevenlabs"},
        })
        self.assertEqual(out["city"], "Казань")
        self.assertNotIn("evil", out)
        self.assertEqual(out["wake"]["followup_seconds"], 60)
        self.assertEqual(out["wake"]["words"], ["джарвис", "жарвис"])
        self.assertEqual(out["audio"]["silence_threshold"], 0.3)
        self.assertEqual(out["audio"]["device"], "3")
        self.assertNotIn("stt", out)
        self.assertEqual(out["tts"], {"voice": "ru-RU-DmitryNeural", "rate": "+10%"})

    def test_sanitize_voice_must_look_like_neural_voice(self):
        self.assertNotIn("voice", backend.sanitize_settings({"tts": {"voice": "x; rm"}})["tts"])
        self.assertEqual(backend.sanitize_settings({"tts": {"voice": "ru-RU-SvetlanaNeural"}})["tts"]["voice"],
                         "ru-RU-SvetlanaNeural")

    def test_sanitize_hermes_fields(self):
        out = backend.sanitize_settings({"hermes": {"provider": "deep seek;", "model": "gpt-4o", "yolo": True}})
        self.assertNotIn("provider", out["hermes"])
        self.assertEqual(out["hermes"]["model"], "gpt-4o")
        self.assertTrue(out["hermes"]["yolo"])

    def test_save_merges_but_replaces_lists(self):
        backend.save_settings({"city": "Казань", "game_mode": {"kill": ["a.exe", "b.exe"]}, "apps": {"x": "y"}})
        backend.save_settings({"game_mode": {"kill": ["c.exe"]}, "apps": {"z": "w"}, "wake": {"words": ["джарвис"]}})
        s = backend.read_user_settings()
        self.assertEqual(s["city"], "Казань")
        self.assertEqual(s["game_mode"]["kill"], ["c.exe"])
        self.assertEqual(s["apps"], {"z": "w"})
        self.assertEqual(s["wake"]["words"], ["джарвис"])

    def test_save_survives_garbage_file(self):
        paths.SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        paths.SETTINGS_PATH.write_text("{сломано", encoding="utf-8")
        backend.save_settings({"city": "Омск"})
        self.assertEqual(backend.read_user_settings()["city"], "Омск")

    def test_public_settings_have_everything_the_window_needs(self):
        backend.save_settings({"game_mode": {"kill": ["a.exe"]}, "apps": {"блокнот": "notepad"}})
        pub = backend.public_settings()
        for key in ("name", "address", "city", "wake", "audio", "stt", "tts", "hermes", "game_mode", "apps"):
            self.assertIn(key, pub)
        self.assertEqual(pub["game_mode"]["kill"], ["a.exe"])
        self.assertNotIn("elevenlabs", pub["tts"])
        self.assertNotIn("api_key", json.dumps(pub))


class SkillTests(unittest.TestCase):
    def test_install_fills_command_and_pointer(self):
        dst = Path(backend.install_skill())
        md = (dst / "SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn("{{", md)
        self.assertIn("pc.py", md)                 # в режиме разработки команда ведёт на скрипт
        self.assertEqual((dst / "scripts" / "settings.path").read_text(encoding="utf-8"), str(paths.SETTINGS_PATH))
        self.assertTrue(backend.skill_state()["current"])

    def test_frozen_command_points_to_jarvis_pc_exe(self):
        old_f, old_e = backend.FROZEN, paths.exe_path
        backend.FROZEN = True
        paths.exe_path = lambda: Path("C:/Users/u/AppData/Local/Programs/Jarvis/Jarvis.exe")
        try:
            cmd = backend.pc_command()
        finally:
            backend.FROZEN, paths.exe_path = old_f, old_e
        self.assertIn("jarvis-pc.exe", cmd)
        self.assertNotIn("\\", cmd)

    def test_ensure_updates_stale_skill(self):
        backend.install_skill()
        (backend.skill_target() / ".jarvis-hash").write_text("old", encoding="utf-8")
        self.assertFalse(backend.skill_state()["current"])
        backend.ensure_skill()
        self.assertTrue(backend.skill_state()["current"])

    def test_ensure_skips_when_hermes_missing(self):
        import shutil
        shutil.rmtree(hermes_dir())
        backend.ensure_skill()
        self.assertFalse(backend.skill_target().exists())


class AutostartTests(unittest.TestCase):
    def test_command_for_exe_and_dev(self):
        old_f, old_e = backend.FROZEN, paths.exe_path
        backend.FROZEN = True
        paths.exe_path = lambda: Path("C:/Jarvis/Jarvis.exe")
        try:
            self.assertTrue(backend.autostart_command().endswith("--minimized"))
            self.assertIn("Jarvis.exe", backend.autostart_command())
        finally:
            backend.FROZEN, paths.exe_path = old_f, old_e

    def test_noop_off_windows(self):
        if os.name != "nt":
            self.assertFalse(backend.autostart_enabled())
            backend.set_autostart(True)       # не падает
            backend.remove_legacy_autostart()


class LegacyImportTests(unittest.TestCase):
    def test_imports_old_settings_and_drops_elevenlabs(self):
        home = Path(os.environ["JARVIS_HOME"]).parent / "fakehome"
        old = home / "Desktop/Hermes agent/Jarvis"
        old.mkdir(parents=True)
        (old / "settings.json").write_text(json.dumps(
            {"city": "Пермь", "tts": {"voice": "ru-RU-SvetlanaNeural", "elevenlabs": {"api_key": "SECRET"}}}), encoding="utf-8")
        real_home = Path.home
        Path.home = staticmethod(lambda: home)
        try:
            src = backend.import_legacy_settings()
        finally:
            Path.home = real_home
        self.assertIsNotNone(src)
        data = backend.read_user_settings()
        self.assertEqual(data["city"], "Пермь")
        self.assertNotIn("elevenlabs", data["tts"])
        self.assertNotIn("SECRET", paths.SETTINGS_PATH.read_text(encoding="utf-8"))

    def test_does_not_overwrite_existing(self):
        backend.save_settings({"city": "Уфа"})
        self.assertIsNone(backend.import_legacy_settings())
        self.assertEqual(backend.read_user_settings()["city"], "Уфа")


class ProviderCatalogTests(unittest.TestCase):
    def test_catalog_is_consistent(self):
        ids = [p["id"] for p in backend.PROVIDERS]
        self.assertEqual(len(ids), len(set(ids)))
        for p in backend.PROVIDERS:
            if p["env"]:
                self.assertRegex(p["env"], backend.ENV_NAME_RE)
        self.assertIn("custom", ids)
        self.assertEqual(backend.provider_by_id("custom")["env"], backend.CUSTOM_KEY_ENV)
