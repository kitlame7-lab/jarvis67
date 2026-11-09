"""Свой Telegram-бот Джарвиса: задания с телефона выполняются на этом компьютере.

Раньше бот работал через шлюз Hermes, который запускался отдельной программой и часто не работал. Теперь бот —
часть Джарвиса: опрос Telegram (long polling, только стандартная библиотека), белый список владельцев, привязка
по коду из окна, текст и голосовые, файлы в обе стороны, ход выполнения в одном обновляемом сообщении.

Telegram разрешает только одного «слушателя» на токен: если тем же ботом пользуется шлюз Hermes, Telegram отвечает
409 Conflict. Тогда бот пишет понятную причину и ждёт, пока шлюз остановят."""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import random
import re
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import paths

log = logging.getLogger("jarvis")

API = "https://api.telegram.org"
TOKEN_RE = re.compile(r"^\d{5,12}:[A-Za-z0-9_-]{30,}$")
MAX_TEXT = 4000
STALE_S = 600                 # сообщения старше 10 минут (компьютер был выключен) не выполняем
HELP = (
    "Я Джарвис на вашем компьютере. Пишите или говорите голосовым, что сделать: «открой загрузки», "
    "«найди в интернете курс биткоина», «пришли файл отчёт.docx с рабочего стола», «запиши заметку …».\n\n"
    "/screen — снимок экрана\n/status — состояние компьютера\n/stop — прервать текущее\n"
    "/tasks — записанные задачи и шаблоны\n/run <шаблон> — выполнить шаблон\n/voice on|off — отвечать ещё и голосом\n"
    "/new — начать разговор заново"
)


# --------------------------------------------------------------------------
# Настройки бота: отдельный файл, токен не попадает в settings.json и отчёты
# --------------------------------------------------------------------------

def config_path() -> Path:
    return paths.DATA_DIR / "telegram.json"


def load_config() -> Dict[str, Any]:
    cfg: Dict[str, Any] = {"token": "", "allowed": [], "enabled": True, "voice_reply": False}
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
        if isinstance(data, dict):
            cfg.update({k: data[k] for k in cfg if k in data})
    except (OSError, ValueError):
        pass
    cfg["allowed"] = [int(x) for x in cfg.get("allowed") or [] if str(x).lstrip("-").isdigit()]
    return cfg


def save_config(**changes: Any) -> Dict[str, Any]:
    cfg = load_config()
    cfg.update(changes)
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return cfg


def import_from_hermes() -> bool:
    """Если бот раньше настраивался для шлюза Hermes, берём его токен и список, чтобы не вводить заново."""
    if config_path().exists():
        return False
    try:
        from . import backend
        env = backend.read_env_file(backend.hermes_home() / ".env")
    except Exception:  # noqa: BLE001
        return False
    token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return False
    users = [int(x) for x in re.split(r"[,\s]+", env.get("TELEGRAM_ALLOWED_USERS", "")) if x.isdigit()]
    save_config(token=token, allowed=users)
    log.info("Telegram: настройки перенесены из Hermes")
    return True


def mask_token(token: str) -> str:
    return f"…{token[-4:]}" if token else ""


# --------------------------------------------------------------------------
# Bot API
# --------------------------------------------------------------------------

class TgError(Exception):
    def __init__(self, code: int, description: str):
        super().__init__(f"{code}: {description}")
        self.code, self.description = code, description


class BotApi:
    def __init__(self, token: str, base: Optional[str] = None):
        self.token, self.base = token, (base or API).rstrip("/")

    def __repr__(self) -> str:
        return f"BotApi({self.base}, token={mask_token(self.token)})"

    def _open(self, req: urllib.request.Request, timeout: float) -> Any:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            try:
                data = json.loads(exc.read().decode("utf-8", "replace"))
                raise TgError(int(data.get("error_code", exc.code)), str(data.get("description", ""))) from None
            except ValueError:
                raise TgError(exc.code, "ошибка Telegram") from None
        data = json.loads(raw.decode("utf-8", "replace"))
        if not data.get("ok"):
            raise TgError(int(data.get("error_code", 0)), str(data.get("description", "")))
        return data.get("result")

    def call(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 20.0) -> Any:
        body = json.dumps(params or {}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(f"{self.base}/bot{self.token}/{method}", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        return self._open(req, timeout)

    def upload(self, method: str, field_name: str, filename: str, data: bytes,
               params: Optional[Dict[str, Any]] = None, timeout: float = 120.0) -> Any:
        boundary = uuid.uuid4().hex
        parts: List[bytes] = []
        for k, v in (params or {}).items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode("utf-8"))
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        safe = filename.replace('"', "'")
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{field_name}"; filename="{safe}"\r\n'
                     f"Content-Type: {ctype}\r\n\r\n".encode("utf-8") + data + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        req = urllib.request.Request(f"{self.base}/bot{self.token}/{method}", data=b"".join(parts), method="POST",
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        return self._open(req, timeout)

    def download(self, file_id: str, limit: int = 20 * 1024 * 1024) -> bytes:
        info = self.call("getFile", {"file_id": file_id})
        path = info.get("file_path") if isinstance(info, dict) else None
        if not path:
            raise TgError(404, "файл недоступен")
        req = urllib.request.Request(f"{self.base}/file/bot{self.token}/{path}")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read(limit)
        except urllib.error.HTTPError as exc:
            raise TgError(exc.code, "не удалось скачать файл") from None


# --------------------------------------------------------------------------
# Задание из Telegram
# --------------------------------------------------------------------------

@dataclass
class Job:
    text: str
    chat_id: int
    user_id: int
    cancel: threading.Event = field(default_factory=threading.Event)
    progress: Callable[[str], None] = lambda line: None
    files: List[str] = field(default_factory=list)          # что прислать в ответ (пути)
    voice: bool = False                                      # просьба пришла голосовым


class Handler:
    """Что бот делает с заданиями. Служба Джарвиса подставляет настоящие действия, тесты — свои."""

    def ask(self, job: Job) -> str:
        return "Готово."

    def screenshot(self) -> Optional[str]:
        return None

    def status(self) -> str:
        return "Работаю."

    def catalog(self) -> str:
        return "Задач и шаблонов нет."

    def run_routine(self, job: Job, name: str) -> str:
        return "Шаблонов нет."

    def transcribe(self, data: bytes) -> str:
        raise RuntimeError("распознавание недоступно")

    def voice(self, text: str) -> Optional[bytes]:
        return None

    def reset(self, chat_id: int) -> None:
        pass


# --------------------------------------------------------------------------
# Бот
# --------------------------------------------------------------------------

class TelegramBot:
    def __init__(self, handler: Handler, api: Optional[BotApi] = None, base: Optional[str] = None,
                 downloads: Optional[Path] = None):
        self.handler = handler
        self.cfg = load_config()
        self.api = api or BotApi(self.cfg["token"], base)
        self.downloads = downloads or (Path.home() / "Downloads" / "Джарвис из Telegram")
        self.state = "off"            # off | starting | ok | conflict | bad_token | offline | no_token
        self.status = "Бот выключен."
        self.username = ""
        self.pair_code = f"{random.SystemRandom().randrange(10 ** 5, 10 ** 6)}"
        self.on_status: Callable[[], None] = lambda: None
        self.offset = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._jobs: Dict[int, List[Job]] = {}
        self._lock = threading.Lock()
        self._tries: Dict[int, int] = {}
        self.poll_timeout = 25

    # --- состояние ------------------------------------------------------
    def _set(self, state: str, status: str) -> None:
        if (state, status) != (self.state, self.status):
            log.info("Telegram: %s", status)
        self.state, self.status = state, status
        try:
            self.on_status()
        except Exception:  # noqa: BLE001
            log.exception("Сбой обработчика статуса Telegram")

    def new_code(self) -> str:
        self.pair_code = f"{random.SystemRandom().randrange(10 ** 5, 10 ** 6)}"
        self._tries.clear()
        return self.pair_code

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        if not self.cfg.get("token"):
            self._set("no_token", "Бот не настроен: вставьте токен от @BotFather.")
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="jarvis-telegram", daemon=True)
        self._thread.start()

    def stop(self, wait: float = 3.0) -> None:
        self._stop.set()
        with self._lock:
            for jobs in self._jobs.values():
                for j in jobs:
                    j.cancel.set()
        th = self._thread
        if th is not None and th is not threading.current_thread():
            th.join(wait)
        self._set("off", "Бот выключен.")

    # --- опрос ----------------------------------------------------------
    def _loop(self) -> None:
        self._set("starting", "Подключаюсь к Telegram…")
        backoff = 2.0
        while not self._stop.is_set():
            try:
                if not self.username:
                    me = self.api.call("getMe", timeout=15)
                    self.username = str(me.get("username", ""))
                    if self.offset == 0:
                        self._skip_backlog()
                self._set("ok", f"Бот @{self.username} работает." + (
                    "" if self.cfg["allowed"] else f" Отправьте ему код {self.pair_code}, чтобы привязать свой Telegram."))
                updates = self.api.call("getUpdates", {"offset": self.offset, "timeout": self.poll_timeout,
                                                       "allowed_updates": ["message"]},
                                        timeout=self.poll_timeout + 10)
                backoff = 2.0
                for upd in updates or []:
                    self.offset = max(self.offset, int(upd.get("update_id", 0)) + 1)
                    try:
                        self.dispatch(upd)
                    except Exception:  # noqa: BLE001 — одно странное сообщение не должно останавливать бота
                        log.exception("Telegram: сбой обработки сообщения")
            except TgError as exc:
                if exc.code == 401 or exc.code == 404:
                    self._set("bad_token", "Telegram не принял токен. Проверьте его у @BotFather и вставьте заново.")
                    return
                if exc.code == 409:
                    self._set("conflict", "Этим ботом уже пользуется другая программа (обычно шлюз Hermes). "
                                          "Остановите шлюз кнопкой ниже — бот Джарвиса подключится сам.")
                    self._stop.wait(15)
                    continue
                self._set("offline", f"Telegram ответил ошибкой {exc.code}. Повторю через {int(backoff)} с.")
                self._stop.wait(backoff)
                backoff = min(60.0, backoff * 2)
            except (urllib.error.URLError, socket.timeout, OSError, ValueError) as exc:
                self._set("offline", f"Нет связи с Telegram ({type(exc).__name__}). Повторю через {int(backoff)} с.")
                self._stop.wait(backoff)
                backoff = min(60.0, backoff * 2)

    def _skip_backlog(self) -> None:
        """Старые сообщения (пока компьютер был выключен) не выполняем: «выключи компьютер» вчерашнее — не сегодня."""
        try:
            pending = self.api.call("getUpdates", {"offset": -1, "timeout": 0}, timeout=15) or []
            if pending:
                last = pending[-1]
                self.offset = int(last.get("update_id", 0))
                msg = last.get("message") or {}
                if time.time() - float(msg.get("date", 0)) > STALE_S:
                    self.offset += 1
        except TgError:
            pass

    # --- сообщения ----------------------------------------------------------
    def send(self, chat_id: int, text: str, **extra: Any) -> Optional[int]:
        text = text.strip() or "…"
        msg_id = None
        for i in range(0, len(text), MAX_TEXT):
            res = self.api.call("sendMessage", dict({"chat_id": chat_id, "text": text[i:i + MAX_TEXT],
                                                     "disable_web_page_preview": True}, **extra))
            msg_id = (res or {}).get("message_id", msg_id)
        return msg_id

    def send_file(self, chat_id: int, path: str, caption: str = "") -> None:
        p = Path(path)
        data = p.read_bytes()
        if len(data) > 49 * 1024 * 1024:
            self.send(chat_id, f"Файл {p.name} больше 50 МБ: Telegram такой не примет.")
            return
        photo = p.suffix.lower() in (".png", ".jpg", ".jpeg") and len(data) < 10 * 1024 * 1024
        self.api.upload("sendPhoto" if photo else "sendDocument", "photo" if photo else "document", p.name, data,
                        {"chat_id": chat_id, "caption": caption[:1000]} if caption else {"chat_id": chat_id})

    def dispatch(self, upd: Dict[str, Any]) -> None:
        msg = upd.get("message") or {}
        if not msg:
            return
        user = int((msg.get("from") or {}).get("id", 0))
        chat = int((msg.get("chat") or {}).get("id", 0))
        if time.time() - float(msg.get("date", time.time())) > STALE_S:
            log.info("Telegram: пропускаю старое сообщение")
            return
        text = str(msg.get("text") or msg.get("caption") or "").strip()
        if user not in self.cfg["allowed"]:
            self._stranger(user, chat, text)
            return
        if text.startswith("/"):
            if self._command(chat, user, text):
                return
        voice = msg.get("voice") or msg.get("audio") or msg.get("video_note")
        doc = msg.get("document") or (msg.get("photo") or [None])[-1]
        self._spawn(self._work, chat, user, text, voice, doc, msg.get("document"))

    def _stranger(self, user: int, chat: int, text: str) -> None:
        code = re.sub(r"\D", "", text)
        if code and code == self.pair_code:
            allowed = sorted(set(self.cfg["allowed"]) | {user})
            self.cfg = save_config(allowed=allowed)
            self.new_code()
            log.info("Telegram: привязан пользователь %s", user)
            self.send(chat, "Готово, вы привязаны. Теперь я выполняю ваши задания на компьютере.\n\n" + HELP)
            self._set("ok", f"Бот @{self.username} работает. Привязано: {len(allowed)}.")
            return
        tries = self._tries.get(user, 0) + 1
        self._tries[user] = tries
        if tries <= 3:
            self.send(chat, "Это личный бот Джарвиса. Чтобы пользоваться им, отправьте код из окна Джарвиса "
                            "(раздел «Telegram»).")

    def _command(self, chat: int, user: int, text: str) -> bool:
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        arg = arg.strip()
        if cmd in ("/start", "/help"):
            self.send(chat, HELP)
        elif cmd == "/stop":
            n = self.cancel(chat)
            self.send(chat, "Остановил." if n else "Сейчас ничего не выполняется.")
        elif cmd == "/screen":
            self._spawn(self._screen, chat)
        elif cmd == "/status":
            self._spawn(lambda: self.send(chat, self.handler.status()))
        elif cmd == "/tasks":
            self.send(chat, self.handler.catalog())
        elif cmd == "/voice":
            on = arg.lower() in ("on", "да", "вкл", "1")
            self.cfg = save_config(voice_reply=on)
            self.send(chat, "Буду отвечать ещё и голосом." if on else "Отвечаю только текстом.")
        elif cmd == "/new":
            self.handler.reset(chat)
            self.send(chat, "Начинаем разговор заново.")
        elif cmd == "/run":
            if not arg:
                self.send(chat, "Напишите название шаблона: /run утро")
            else:
                self._spawn(self._work, chat, user, "", None, None, None, arg)
        else:
            return False
        return True

    def cancel(self, chat: int) -> int:
        with self._lock:
            jobs = list(self._jobs.get(chat, []))
        for j in jobs:
            j.cancel.set()
        return len(jobs)

    def _spawn(self, fn: Callable[..., Any], *args: Any) -> None:
        def work() -> None:
            try:
                fn(*args)
            except TgError as exc:
                log.warning("Telegram: %s", exc)
            except Exception:  # noqa: BLE001
                log.exception("Telegram: сбой задания")
        threading.Thread(target=work, name="jarvis-tg-job", daemon=True).start()

    def _screen(self, chat: int) -> None:
        path = self.handler.screenshot()
        if path and Path(path).exists():
            self.send_file(chat, path)
        else:
            self.send(chat, "Не удалось сделать снимок экрана.")

    def _typing(self, chat: int, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.api.call("sendChatAction", {"chat_id": chat, "action": "typing"}, timeout=10)
            except (TgError, OSError, ValueError):
                pass
            stop.wait(4.5)

    def _work(self, chat: int, user: int, text: str, voice: Optional[Dict[str, Any]], doc: Optional[Dict[str, Any]],
              document: Optional[Dict[str, Any]], routine: Optional[str] = None) -> None:
        job = Job(text, chat, user)
        with self._lock:
            self._jobs.setdefault(chat, []).append(job)
        done = threading.Event()
        threading.Thread(target=self._typing, args=(chat, done), daemon=True).start()
        steps: List[str] = []
        status_msg: List[Optional[int]] = [None]
        last_edit = [0.0]

        def progress(line: str) -> None:
            steps.append(line)
            body = "⏳ Выполняю:\n" + "\n".join("• " + s[:80] for s in steps[-8:])
            try:
                if status_msg[0] is None:
                    status_msg[0] = self.send(chat, body)
                elif time.monotonic() - last_edit[0] > 1.0:
                    self.api.call("editMessageText", {"chat_id": chat, "message_id": status_msg[0], "text": body})
                    last_edit[0] = time.monotonic()
            except (TgError, OSError, ValueError):
                pass
        job.progress = progress
        try:
            if voice:
                job.voice = True
                heard = self.handler.transcribe(self.api.download(voice["file_id"]))
                if not heard.strip():
                    self.send(chat, "Не разобрал голосовое. Повторите или напишите текстом.")
                    return
                self.send(chat, f"🎙 {heard}")
                job.text = (text + "\n" + heard).strip()
            if doc and not voice:
                saved = self._save_incoming(doc, document)
                job.text = (job.text + f"\n[Владелец прислал файл, он сохранён: {saved}]").strip()
            if routine is not None:
                reply = self.handler.run_routine(job, routine)
            elif not job.text:
                return
            else:
                reply = self.handler.ask(job)
            if job.cancel.is_set():
                return
            if status_msg[0] is not None:
                try:
                    self.api.call("editMessageText", {"chat_id": chat, "message_id": status_msg[0],
                                                      "text": "✅ Выполнено:\n" + "\n".join("• " + s[:80] for s in steps[-8:])})
                except (TgError, OSError, ValueError):
                    pass
            self.send(chat, reply)
            for path in dict.fromkeys(job.files):
                try:
                    self.send_file(chat, path)
                except (OSError, TgError) as exc:
                    self.send(chat, f"Не смог отправить {Path(path).name}: {exc}")
            if self.cfg.get("voice_reply"):
                audio = self.handler.voice(reply)
                if audio:
                    self.api.upload("sendAudio", "audio", "Джарвис.mp3", audio, {"chat_id": chat})
        except TgError as exc:
            log.warning("Telegram: %s", exc)
            try:
                self.send(chat, f"Ошибка Telegram: {exc.description}")
            except (TgError, OSError):
                pass
        except Exception as exc:  # noqa: BLE001 — человек должен узнать, что задание не выполнено
            log.exception("Telegram: сбой задания")
            try:
                self.send(chat, f"Не получилось: {exc}")
            except (TgError, OSError):
                pass
        finally:
            done.set()
            with self._lock:
                jobs = self._jobs.get(chat, [])
                if job in jobs:
                    jobs.remove(job)

    def _save_incoming(self, doc: Dict[str, Any], document: Optional[Dict[str, Any]]) -> str:
        data = self.api.download(doc["file_id"])
        name = (document or {}).get("file_name") or f"фото_{time.strftime('%Y%m%d_%H%M%S')}.jpg"
        name = re.sub(r'[\\/:*?"<>|]+', "_", name)
        self.downloads.mkdir(parents=True, exist_ok=True)
        path = self.downloads / name
        n = 1
        while path.exists():
            path = self.downloads / f"{Path(name).stem} ({n}){Path(name).suffix}"
            n += 1
        path.write_bytes(data)
        return str(path)


def screenshot_paths(results: List[Tuple[str, bool, str]]) -> List[str]:
    """Файлы, которые стоит прислать: снимки экрана из выполненных команд."""
    out = []
    for line, ok, res in results:
        if ok and line.split()[:1] == ["screenshot"]:
            m = re.search(r"(?:сохранён|→)\s*:?\s*(.+\.png)", res)
            if m:
                out.append(m.group(1).strip())
    return out
