"""Проверка и скачивание обновлений без настоящего GitHub."""
import hashlib
import io
import json
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


from jarvis import __version__, paths, updater


def release(tag="v9.9.9", body="Что нового", assets=None, **extra):
    d = {"tag_name": tag, "body": body, "html_url": "https://github.com/x/y/releases/tag/" + tag,
         "assets": assets if assets is not None else [
             {"name": "Jarvis-Setup-9.9.9.exe", "browser_download_url": "https://dl/x.exe", "size": 5}]}
    d.update(extra)
    return d


class FakeResp(io.BytesIO):
    def __init__(self, data, headers=None):
        super().__init__(data)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class VersionTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(updater.parse_version("v3.1.0"), (3, 1, 0))
        self.assertEqual(updater.parse_version("3.0.0-beta.1"), (3, 0, 0))
        self.assertEqual(updater.parse_version("мусор"), (0,))

    def test_is_newer(self):
        self.assertTrue(updater.is_newer("3.0.1", "3.0.0"))
        self.assertTrue(updater.is_newer("v3.10.0", "3.9.9"))      # 10 > 9, а не строка
        self.assertTrue(updater.is_newer("4", "3.9.9"))
        self.assertFalse(updater.is_newer("3.0.0", "3.0.0"))
        self.assertFalse(updater.is_newer("3.0", "3.0.0"))
        self.assertFalse(updater.is_newer("2.9.9", "3.0.0"))


class ParseTests(unittest.TestCase):
    def test_picks_installer_asset(self):
        rel = updater.parse_release(release(assets=[
            {"name": "notes.txt", "browser_download_url": "u0", "size": 1},
            {"name": "Jarvis-Setup-9.9.9.exe", "browser_download_url": "u1", "size": 7}]))
        self.assertEqual((rel.version, rel.asset_url, rel.size), ("9.9.9", "u1", 7))

    def test_no_installer(self):
        rel = updater.parse_release(release(assets=[]))
        self.assertEqual(rel.asset_url, "")

    def test_sha_from_digest_or_body(self):
        h = "a" * 64
        rel = updater.parse_release(release(assets=[
            {"name": "Jarvis-Setup.exe", "browser_download_url": "u", "size": 1, "digest": "sha256:" + h}]))
        self.assertEqual(rel.sha256, h)
        rel = updater.parse_release(release(body=f"SHA256: {'B' * 64}"))
        self.assertEqual(rel.sha256, "b" * 64)

    def test_missing_tag(self):
        with self.assertRaises(updater.UpdateError):
            updater.parse_release({"assets": []})


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.orig = updater._get
        self.addCleanup(setattr, updater, "_get", self.orig)

    def serve(self, data):
        updater._get = lambda url, **k: FakeResp(json.dumps(data).encode())

    def fail(self, exc):
        def f(url, **k):
            raise exc
        updater._get = f

    def test_newer_found(self):
        self.serve(release())
        self.assertEqual(updater.check().version, "9.9.9")

    def test_same_or_older(self):
        self.serve(release(tag="v" + __version__))
        self.assertIsNone(updater.check())

    def test_draft_and_prerelease_ignored(self):
        self.serve(release(draft=True))
        self.assertIsNone(updater.check())
        self.serve(release(prerelease=True))
        self.assertIsNone(updater.check())

    def test_no_releases_yet(self):
        self.fail(urllib.error.HTTPError("u", 404, "nf", {}, io.BytesIO(b"")))
        self.assertIsNone(updater.check())

    def test_rate_limit_and_offline(self):
        self.fail(urllib.error.HTTPError("u", 403, "x", {}, io.BytesIO(b"")))
        with self.assertRaises(updater.UpdateError) as cm:
            updater.check()
        self.assertIn("ограничил", str(cm.exception))
        self.fail(urllib.error.URLError("нет сети"))
        with self.assertRaises(updater.UpdateError) as cm:
            updater.check()
        self.assertIn("нет связи", str(cm.exception))

    def test_garbage_answer(self):
        updater._get = lambda url, **k: FakeResp(b"<html>")
        with self.assertRaises(updater.UpdateError):
            updater.check()

    def test_url_uses_repo(self):
        seen = []
        updater._get = lambda url, **k: (seen.append(url), FakeResp(json.dumps(release(tag="v0.0.1")).encode()))[1]
        updater.check()
        self.assertIn("/repos/kitlame7-lab/jarvis67/releases/latest", seen[0])


class DownloadTests(unittest.TestCase):
    PAYLOAD = b"MZ" + b"\x00" * 5000

    def setUp(self):
        payload = self.PAYLOAD

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                cut = payload[: len(payload) // 2] if self.path == "/short" else payload
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                try:
                    self.wfile.write(cut)
                except OSError:
                    pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.base = f"http://127.0.0.1:{self.srv.server_port}"
        self.dir = Path(paths.CACHE_DIR) / "update-test"

    def rel(self, path="/ok", sha=None, size=None):
        return updater.Release("9.9.9", "", "", self.base + path, "Jarvis-Setup-9.9.9.exe",
                               len(self.PAYLOAD) if size is None else size,
                               hashlib.sha256(self.PAYLOAD).hexdigest() if sha is None else sha)

    def test_ok_with_progress(self):
        ticks = []
        out = updater.download(self.rel(), lambda d, t: ticks.append((d, t)), self.dir)
        self.assertEqual(out.read_bytes(), self.PAYLOAD)
        self.assertEqual(ticks[-1], (len(self.PAYLOAD), len(self.PAYLOAD)))
        self.assertFalse(list(self.dir.glob("*.part")))

    def test_checksum_mismatch_removes_file(self):
        with self.assertRaises(updater.UpdateError) as cm:
            updater.download(self.rel(sha="0" * 64), dest_dir=self.dir)
        self.assertIn("контрольная сумма", str(cm.exception))
        self.assertFalse(list(self.dir.iterdir()))

    def test_without_checksum_still_checks_size(self):
        with self.assertRaises(updater.UpdateError):
            updater.download(self.rel(sha="", size=1), dest_dir=self.dir)

    def test_truncated_download(self):
        with self.assertRaises(updater.UpdateError):
            updater.download(self.rel(path="/short"), dest_dir=self.dir)

    def test_no_asset(self):
        r = self.rel()
        r.asset_url = ""
        with self.assertRaises(updater.UpdateError):
            updater.download(r, dest_dir=self.dir)

    def test_cleanup_old(self):
        folder = paths.CACHE_DIR / "update"
        folder.mkdir(parents=True, exist_ok=True)
        keep, drop = folder / "keep.exe", folder / "drop.exe"
        keep.write_bytes(b"1")
        drop.write_bytes(b"1")
        updater.cleanup_old(keep)
        self.assertTrue(keep.exists())
        self.assertFalse(drop.exists())

    def test_installer_args_are_silent(self):
        import subprocess
        calls = []
        real_popen, real_name = subprocess.Popen, updater.os.name
        subprocess.Popen = lambda argv, **k: calls.append(argv)
        updater.os.name = "nt"
        try:
            updater.launch_installer(Path("C:/x/Jarvis-Setup.exe"))
        finally:
            subprocess.Popen = real_popen
            updater.os.name = real_name
        self.assertIn("/SILENT", calls[0])
        self.assertIn("/CLOSEAPPLICATIONS", calls[0])
        self.assertIn("/RESTARTAPPLICATIONS", calls[0])
