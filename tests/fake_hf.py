"""Поддельный сервер Hugging Face для проверки загрузки модели без интернета.

Отдаёт крошечный «репозиторий» с теми же путями, что у настоящего: список файлов, HEAD и GET с поддержкой Range.
Режимы: нормальный, медленный (отдаёт по кусочку с паузой), зависающий (после части данных молчит), сломанный (500).
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional

SHA = "a" * 40


class FakeHub:
    def __init__(self, repo: str, files: Dict[str, bytes], mode: str = "ok", chunk: int = 4096, pause: float = 0.0):
        self.repo, self.files, self.mode, self.chunk, self.pause = repo, files, mode, chunk, pause
        self.requests: List[str] = []
        hub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):       # тишина
                pass

            def _etag(self, name: str) -> str:
                return '"' + hashlib.sha1(hub.files[name]).hexdigest() + '"'

            def _route(self):
                path = self.path.split("?")[0]
                hub.requests.append(f"{self.command} {path}")
                if hub.mode == "broken":
                    self.send_response(500)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return None
                if path.startswith(f"/api/models/{hub.repo}"):
                    if "/tree/" in path:      # huggingface_hub 1.x/2.x перечисляет файлы отдельным запросом
                        listing = [{"type": "file", "oid": hashlib.sha1(d).hexdigest(), "size": len(d), "path": n}
                                   for n, d in hub.files.items()]
                        body = json.dumps(listing).encode()
                    else:
                        body = json.dumps({"id": hub.repo, "sha": SHA,
                                           "siblings": [{"rfilename": n} for n in hub.files]}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    if self.command == "GET":
                        self.wfile.write(body)
                    return None
                prefix = f"/{hub.repo}/resolve/"
                if path.startswith(prefix):
                    name = path[len(prefix):].split("/", 1)[1]
                    if name in hub.files:
                        return name
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None

            def do_HEAD(self):
                name = self._route()
                if name is None:
                    return
                self.send_response(200)
                self.send_header("ETag", self._etag(name))
                self.send_header("X-Repo-Commit", SHA)
                self.send_header("Content-Length", str(len(hub.files[name])))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()

            def do_GET(self):
                name = self._route()
                if name is None or self.path.startswith("/api/"):
                    return
                data = hub.files[name]
                start = 0
                rng = self.headers.get("Range")
                if rng and rng.startswith("bytes="):
                    start = int(rng[6:].split("-")[0] or 0)
                part = data[start:]
                self.send_response(206 if start else 200)
                self.send_header("ETag", self._etag(name))
                self.send_header("X-Repo-Commit", SHA)
                self.send_header("Content-Length", str(len(part)))
                self.send_header("Accept-Ranges", "bytes")
                if start:
                    self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
                self.end_headers()
                sent = 0
                try:
                    while sent < len(part):
                        if hub.mode == "hang" and name == "model.bin" and sent >= len(part) // 4:
                            time.sleep(60)               # молчим: клиент должен сам понять, что данные не идут
                            return
                        piece = part[sent:sent + hub.chunk]
                        self.wfile.write(piece)
                        sent += len(piece)
                        if hub.pause:
                            time.sleep(hub.pause)
                except (BrokenPipeError, ConnectionResetError):
                    return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self) -> "FakeHub":
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()


def tiny_model(size: int = 400_000) -> Dict[str, bytes]:
    return {"config.json": b"{}", "preprocessor_config.json": b"{}", "tokenizer.json": b"{}",
            "vocabulary.json": b"[]", "model.bin": bytes(range(256)) * (size // 256)}
