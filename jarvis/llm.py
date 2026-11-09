"""Прямой запрос к языковой модели по протоколу OpenAI (chat/completions) без Hermes.

Hermes при каждом вопросе запускается как отдельная большая программа (загрузка навыков, инструментов, памяти и
длинного системного описания), поэтому отвечает долго и может зависнуть. Здесь один HTTP-запрос с жёстким временем
ожидания: типичный ответ приходит за пару секунд, а при сбое человек получает понятную причину, а не тишину.

Адрес, ключ и модель берутся оттуда же, где их настраивали в разделе «Подключения» (config.yaml и .env Hermes),
поэтому вводить их второй раз не нужно."""
from __future__ import annotations

import json
import logging
import os
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional

from . import backend

log = logging.getLogger("jarvis")

# Адреса OpenAI-совместимых API для встроенных провайдеров. У Anthropic своя схема запросов: он идёт через Hermes.
NATIVE_BASES: Dict[str, str] = {
    "deepseek": "https://api.deepseek.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "openai-api": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "xai": "https://api.x.ai/v1",
    "zai": "https://api.z.ai/api/paas/v4",
    "kimi-coding": "https://api.moonshot.ai/v1",
    "minimax": "https://api.minimax.io/v1",
    "alibaba": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
}
DEFAULT_MODELS = {"deepseek": "deepseek-chat"}


class LLMError(Exception):
    """Сбой запроса к модели; текст понятен человеку и подходит для озвучки."""


@dataclass
class Endpoint:
    base_url: str
    key: str
    model: str
    label: str = ""

    def __repr__(self) -> str:          # ключ не должен попадать в журналы
        return f"Endpoint({self.base_url}, model={self.model}, key=***)"


def resolve_endpoint(cfg: Dict[str, Any]) -> Optional[Endpoint]:
    """Находит, куда слать запросы. None, если настроено то, что встроенный движок не умеет (тогда работает Hermes)."""
    home = backend.hermes_home()
    now = backend.read_hermes_model(home)
    hermes_cfg = cfg.get("hermes", {})
    provider = (hermes_cfg.get("provider") or now.get("provider") or "").strip()
    model = (hermes_cfg.get("model") or now.get("model") or "").strip()

    if provider == "custom" or (not provider and now.get("base_url")):
        base = now.get("base_url", "")
        env = now.get("key_env") or backend.CUSTOM_KEY_ENV
        key = backend.stored_key(env) or os.environ.get(env, "")
        if base and key and model:
            try:
                return Endpoint(backend.normalize_base_url(base), key, model, "свой сервер")
            except ValueError:
                return None
        return None

    base = NATIVE_BASES.get(provider)
    if base:
        info = backend.provider_by_id(provider) or {}
        env = info.get("env", "")
        key = backend.stored_key(env) or os.environ.get(env, "")
        model = model or DEFAULT_MODELS.get(provider, "")
        if key and model:
            return Endpoint(base, key, model, info.get("label", provider))
    return None


THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)


def chat(ep: Endpoint, messages: List[Dict[str, str]], max_tokens: int = 400, temperature: float = 0.4,
         timeout: float = 40.0) -> str:
    """Один запрос. Возвращает текст ответа или бросает LLMError с причиной по-русски."""
    body = json.dumps({"model": ep.model, "messages": messages, "max_tokens": max_tokens,
                       "temperature": temperature}).encode("utf-8")
    req = urllib.request.Request(ep.base_url + "/chat/completions", data=body, method="POST", headers={
        "Authorization": f"Bearer {ep.key}", "Content-Type": "application/json", "Accept": "application/json",
        "User-Agent": backend.USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw, code = resp.read().decode("utf-8", "replace"), resp.status
    except urllib.error.HTTPError as exc:
        code = exc.code
        try:
            raw = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            raw = ""
    except socket.timeout:
        raise LLMError("Модель не ответила вовремя. Попробуйте ещё раз или выберите модель побыстрее.") from None
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, socket.timeout) or "timed out" in str(reason):
            raise LLMError("Модель не ответила вовремя. Попробуйте ещё раз или выберите модель побыстрее.") from None
        raise LLMError("Не удалось связаться с сервером модели. Проверьте интернет и Base URL в разделе «Подключения».") from None
    try:
        payload: Any = json.loads(raw)
    except ValueError:
        payload = raw
    if code != 200:
        raise LLMError(backend.explain_endpoint_error(code, payload).replace(ep.key, "***"))
    try:
        text = str(payload["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError):
        raise LLMError("Сервер ответил не в формате OpenAI. Проверьте Base URL в разделе «Подключения».") from None
    text = THINK_RE.sub("", text).strip()
    if not text:
        raise LLMError("Модель вернула пустой ответ.")
    return text


def _request(ep: Endpoint, messages: List[Dict[str, Any]], max_tokens: int, temperature: float, stream: bool):
    body: Dict[str, Any] = {"model": ep.model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
    if stream:
        body["stream"] = True
    return urllib.request.Request(ep.base_url + "/chat/completions", data=json.dumps(body).encode("utf-8"),
                                  method="POST", headers={
                                      "Authorization": f"Bearer {ep.key}", "Content-Type": "application/json",
                                      "Accept": "text/event-stream" if stream else "application/json",
                                      "User-Agent": backend.USER_AGENT})


class _ThinkFilter:
    """Вырезает <think>…</think> из потока, даже если тег разрезан между кусками."""

    def __init__(self) -> None:
        self.buf, self.inside = "", False

    def feed(self, text: str) -> str:
        self.buf += text
        out = []
        while self.buf:
            if self.inside:
                end = self.buf.lower().find("</think>")
                if end < 0:
                    self.buf = self.buf[-8:]
                    return "".join(out)
                self.buf, self.inside = self.buf[end + 8:], False
                continue
            start = self.buf.lower().find("<think>")
            if start >= 0:
                out.append(self.buf[:start])
                self.buf, self.inside = self.buf[start + 7:], True
                continue
            # хвост может оказаться началом тега: придерживаем его
            keep = next((k for k in range(min(6, len(self.buf)), 0, -1) if "<think>".startswith(self.buf[-k:].lower())), 0)
            out.append(self.buf[:len(self.buf) - keep])
            self.buf = self.buf[len(self.buf) - keep:]
            break
        return "".join(out)

    def flush(self) -> str:
        rest, self.buf = ("" if self.inside else self.buf), ""
        return rest


def stream_chat(ep: Endpoint, messages: List[Dict[str, Any]], max_tokens: int = 400, temperature: float = 0.4,
                timeout: float = 40.0, cancel: Optional[Any] = None) -> Iterator[str]:
    """Ответ модели по кусочкам (SSE), чтобы озвучка начиналась с первого предложения.

    `timeout` — сколько ждать подключения и каждого следующего куска. Если сервер не умеет поток и вернул обычный
    JSON, он отдаётся одним куском. Ошибки — LLMError с причиной по-русски."""
    req = _request(ep, messages, max_tokens, temperature, stream=True)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            raw = ""
        try:
            payload: Any = json.loads(raw)
        except ValueError:
            payload = raw
        raise LLMError(backend.explain_endpoint_error(exc.code, payload).replace(ep.key, "***")) from None
    except socket.timeout:
        raise LLMError("Модель не ответила вовремя. Попробуйте ещё раз или выберите модель побыстрее.") from None
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, socket.timeout) or "timed out" in str(reason):
            raise LLMError("Модель не ответила вовремя. Попробуйте ещё раз или выберите модель побыстрее.") from None
        raise LLMError("Не удалось связаться с сервером модели. Проверьте интернет и Base URL в разделе «Подключения».") from None

    think = _ThinkFilter()
    got = False
    with resp:
        ctype = resp.headers.get("Content-Type", "")
        try:
            if "event-stream" not in ctype:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
                text = THINK_RE.sub("", str(payload["choices"][0]["message"]["content"] or "")).strip()
                if not text:
                    raise LLMError("Модель вернула пустой ответ.")
                yield text
                return
            for raw_line in resp:
                if cancel is not None and cancel.is_set():
                    return
                line = raw_line.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                if isinstance(chunk, dict) and chunk.get("error"):
                    raise LLMError(backend.explain_endpoint_error(500, chunk).replace(ep.key, "***"))
                try:
                    delta = chunk["choices"][0].get("delta") or chunk["choices"][0].get("message") or {}
                except (KeyError, IndexError, TypeError, AttributeError):
                    continue
                piece = think.feed(str(delta.get("content") or ""))
                if piece:
                    got = True
                    yield piece
        except socket.timeout:
            raise LLMError("Модель замолчала посреди ответа. Попробуйте ещё раз.") from None
        except (KeyError, IndexError, TypeError, ValueError):
            raise LLMError("Сервер ответил не в формате OpenAI. Проверьте Base URL в разделе «Подключения».") from None
        except OSError:
            raise LLMError("Связь с сервером модели оборвалась. Попробуйте ещё раз.") from None
    rest = think.flush()
    if rest:
        got = True
        yield rest
    if not got:
        raise LLMError("Модель вернула пустой ответ.")
