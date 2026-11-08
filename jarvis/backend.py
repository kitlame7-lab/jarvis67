"""Действия, которые нужны окну настроек: сохранение настроек, ключи и модель Hermes,
свой сервер модели, автозапуск, навык для Hermes. Без интерфейса, всё проверяется тестами."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import __version__, paths
from .paths import FROZEN, IS_WIN, RES_DIR

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
USER_AGENT = f"Jarvis-Assistant/{__version__}"
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
CUSTOM_KEY_ENV = "JARVIS_CUSTOM_API_KEY"

# --------------------------------------------------------------------------
# Провайдеры Hermes (идентификаторы и переменные ключей из документации Hermes)
# --------------------------------------------------------------------------

PROVIDERS: List[Dict[str, Any]] = [
    {"id": "", "label": "Как настроено в самом Hermes", "env": "", "models": [],
     "hint": "Джарвис не будет переопределять провайдера и модель."},
    {"id": "custom", "label": "Свой сервер (Base URL + ключ + модель)", "env": CUSTOM_KEY_ENV, "models": [],
     "hint": "Для ключей с сайтов-перепродавцов и любых OpenAI-совместимых серверов."},
    {"id": "deepseek", "label": "DeepSeek", "env": "DEEPSEEK_API_KEY",
     "models": ["deepseek-chat", "deepseek-reasoner"],
     "hint": "Ключ создаётся на platform.deepseek.com в разделе API keys. Нужен положительный баланс."},
    {"id": "openrouter", "label": "OpenRouter", "env": "OPENROUTER_API_KEY", "models": [],
     "hint": "Один ключ на много моделей. Название модели вида «провайдер/модель», смотрите на openrouter.ai."},
    {"id": "openai-api", "label": "OpenAI", "env": "OPENAI_API_KEY", "models": [],
     "hint": "Ключ на platform.openai.com."},
    {"id": "anthropic", "label": "Anthropic (Claude)", "env": "ANTHROPIC_API_KEY",
     "models": ["claude-sonnet-4-6"], "hint": "Ключ на console.anthropic.com."},
    {"id": "gemini", "label": "Google Gemini", "env": "GEMINI_API_KEY", "models": [],
     "hint": "Ключ в Google AI Studio."},
    {"id": "xai", "label": "xAI (Grok)", "env": "XAI_API_KEY", "models": ["grok-4-fast-reasoning"], "hint": ""},
    {"id": "zai", "label": "z.ai (GLM)", "env": "GLM_API_KEY", "models": ["glm-5"], "hint": ""},
    {"id": "kimi-coding", "label": "Kimi (Moonshot)", "env": "KIMI_API_KEY", "models": ["kimi-k2.5"], "hint": ""},
    {"id": "minimax", "label": "MiniMax", "env": "MINIMAX_API_KEY", "models": ["MiniMax-M2.7"], "hint": ""},
    {"id": "alibaba", "label": "Qwen (DashScope)", "env": "DASHSCOPE_API_KEY", "models": ["qwen3.5-plus"], "hint": ""},
]
KNOWN_IDS = {p["id"] for p in PROVIDERS}


def provider_by_id(pid: str) -> Optional[Dict[str, Any]]:
    return next((p for p in PROVIDERS if p["id"] == pid), None)


# --------------------------------------------------------------------------
# Hermes: каталог, .env, config.yaml
# --------------------------------------------------------------------------

def hermes_home() -> Path:
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    local = os.environ.get("LOCALAPPDATA")
    if local and (Path(local) / "hermes").exists():
        return Path(local) / "hermes"
    return Path.home() / ".hermes"


def read_env_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    try:
        text = path.read_bytes().decode("utf-8-sig", "replace")
    except OSError:
        return values
    for line in text.splitlines():
        m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
        if not m:
            continue
        val = m.group(2)
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        values[m.group(1)] = val
    return values


def write_env_value(path: Path, key: str, value: Optional[str]) -> None:
    """Задаёт (или удаляет при None) строку KEY=value, остальное не трогает."""
    if not ENV_NAME_RE.match(key):
        raise ValueError(f"недопустимое имя переменной: {key}")
    if value is not None and ("\n" in value or "\r" in value):
        raise ValueError("значение не должно содержать переводы строк")
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = ""
    if path.exists():
        raw = path.read_bytes().decode("utf-8-sig", "replace")  # байтами, чтобы сохранить \r\n
        backup = path.with_name(path.name + ".jarvis.bak")
        if not backup.exists():
            shutil.copyfile(path, backup)
    nl = "\r\n" if "\r\n" in raw else "\n"
    pat = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=")
    out: List[str] = []
    done = False
    for line in raw.splitlines():
        if pat.match(line):
            if value is not None and not done:
                out.append(f"{key}={value}")
            done = True
        else:
            out.append(line)
    if value is not None and not done:
        out.append(f"{key}={value}")
    text = nl.join(out) + (nl if out else "")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(text.encode("utf-8"))
    os.replace(tmp, path)


def mask(value: str) -> Dict[str, Any]:
    return {"set": bool(value), "tail": value[-4:] if len(value) >= 8 else ""}


def _model_block_span(lines: List[str]) -> Optional[Tuple[int, int]]:
    for i, line in enumerate(lines):
        if re.match(r"^model:(\s|$)", line):
            j = i + 1
            while j < len(lines) and (lines[j].strip() == "" or lines[j][:1] in (" ", "\t")):
                j += 1
            while j > i + 1 and lines[j - 1].strip() == "":
                j -= 1
            return i, j
    return None


def read_hermes_model(home: Path) -> Dict[str, str]:
    """Что записано в config.yaml Hermes: провайдер, модель, адрес (простой разбор без YAML-библиотеки)."""
    result = {"provider": "", "model": "", "base_url": "", "key_env": ""}
    try:
        text = (home / "config.yaml").read_bytes().decode("utf-8-sig", "replace")
    except OSError:
        return result
    lines = text.splitlines()
    span = _model_block_span(lines)
    if not span:
        return result
    first = lines[span[0]][len("model:"):].split("#")[0].strip().strip("\"'")
    if first:
        result["model"] = first
    body = "\n".join(lines[span[0] + 1:span[1]])
    for key, field in (("provider", "provider"), ("default", "model"), ("model", "model"),
                       ("base_url", "base_url"), ("key_env", "key_env")):
        m = re.search(rf"^[ \t]+{key}:[ \t]*[\"']?([^\"'\n#]+)", body, re.M)
        if m and not result[field]:
            result[field] = m.group(1).strip()
    return result


def write_hermes_model(home: Path, provider: str, model: str, base_url: str = "", key_env: str = "") -> None:
    """Заменяет блок `model:` в config.yaml Hermes, остальное не трогает. Делает копию."""
    path = home / "config.yaml"
    raw = ""
    if path.exists():
        raw = path.read_bytes().decode("utf-8-sig", "replace")
        backup = path.with_name(path.name + ".jarvis.bak")
        if not backup.exists():
            shutil.copyfile(path, backup)
    nl = "\r\n" if "\r\n" in raw else "\n"
    block = ["model:"]
    if provider:
        block.append(f"  provider: {json.dumps(provider)}")
    if model:
        block.append(f"  default: {json.dumps(model, ensure_ascii=False)}")
    if base_url:
        block.append(f"  base_url: {json.dumps(base_url)}")
    if key_env:
        block.append(f"  key_env: {key_env}")
    lines = raw.splitlines()
    span = _model_block_span(lines)
    if span:
        lines[span[0]:span[1]] = block
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(block)
    home.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes((nl.join(lines) + nl).encode("utf-8"))
    os.replace(tmp, path)


def normalize_base_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    for tail in ("/chat/completions", "/completions"):
        if url.lower().endswith(tail):
            url = url[: -len(tail)]
    if not re.match(r"^https?://[^\s/]+", url):
        raise ValueError("Base URL должен начинаться с http:// или https://")
    return url


def save_model_choice(provider: str, model: str, *, api_key: Optional[str] = None, clear_key: bool = False,
                      base_url: str = "", custom_provider: str = "", custom_env: str = "") -> None:
    """Сохраняет выбор модели. Ключи пишутся в .env Hermes, адрес и модель своего сервера — в config.yaml."""
    home = hermes_home()
    envp = home / ".env"
    model = (model or "").strip()
    env_name = ""
    if provider == "__other__":
        provider, env_name = custom_provider.strip(), custom_env.strip()
        if provider and not re.fullmatch(r"[A-Za-z0-9._\-]{1,60}", provider):
            raise ValueError("идентификатор провайдера: латиница, цифры, точка, дефис")
        if env_name and not ENV_NAME_RE.match(env_name):
            raise ValueError("имя переменной ключа: заглавные латинские буквы, цифры и _")
    else:
        info = provider_by_id(provider)
        if info is None:
            raise ValueError(f"неизвестный провайдер: {provider}")
        env_name = info["env"]

    if provider == "custom":
        url = normalize_base_url(base_url)
        if not model:
            raise ValueError("укажите название модели")
        if api_key:
            write_env_value(envp, CUSTOM_KEY_ENV, api_key.strip())
        elif not read_env_file(envp).get(CUSTOM_KEY_ENV):
            raise ValueError("вставьте API-ключ")
        write_hermes_model(home, "custom", model, base_url=url, key_env=CUSTOM_KEY_ENV)
        # Hermes теперь целиком знает адрес, модель и ключ: Джарвис ничего не переопределяет
        _save_hermes_overrides("", "")
        return

    if api_key and env_name:
        write_env_value(envp, env_name, api_key.strip())
    elif api_key and not env_name:
        raise ValueError("для этого варианта не задана переменная ключа")
    if clear_key and env_name:
        write_env_value(envp, env_name, None)
    _save_hermes_overrides(provider, model)


def _save_hermes_overrides(provider: str, model: str) -> None:
    save_settings({"hermes": {"provider": provider, "model": model}})


# --------------------------------------------------------------------------
# Свой сервер: прямая проверка адреса, ключа и модели
# --------------------------------------------------------------------------

def _http_json(url: str, key: str, body: Optional[dict] = None, timeout: float = 30) -> Tuple[int, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        "Accept": "application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            code = resp.status
    except urllib.error.HTTPError as exc:
        code = exc.code
        try:
            raw = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            raw = ""
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(f"нет связи с сервером: {getattr(exc, 'reason', exc)}") from None
    try:
        return code, json.loads(raw)
    except ValueError:
        return code, raw


def _api_message(payload: Any) -> str:
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err)[:240]
        if err:
            return str(err)[:240]
        if payload.get("message"):
            return str(payload["message"])[:240]
    return str(payload)[:240]


def explain_endpoint_error(code: int, payload: Any) -> str:
    text = _api_message(payload)
    if code in (401, 403):
        return f"Сервер не принял ключ (HTTP {code}). Проверьте ключ целиком, без пробелов. {text}".strip()
    if code == 404:
        return ("Сервер ответил «не найдено» (HTTP 404). Проверьте Base URL: обычно он заканчивается на /v1. "
                + text).strip()
    if code == 402:
        return f"На счёте закончились средства (HTTP 402). {text}".strip()
    if code == 429:
        return f"Слишком много запросов или лимит ключа исчерпан (HTTP 429). {text}".strip()
    if code in (400, 422):
        return f"Сервер не принял запрос (HTTP {code}). Часто это неверное название модели. {text}".strip()
    return f"Сервер ответил HTTP {code}. {text}".strip()


def probe_endpoint(base_url: str, api_key: str, model: str) -> str:
    """Один короткий запрос к серверу. Возвращает ответ модели или бросает RuntimeError с причиной."""
    base = normalize_base_url(base_url)
    if not api_key.strip():
        raise RuntimeError("вставьте API-ключ")
    if not model.strip():
        raise RuntimeError("укажите название модели")
    code, payload = _http_json(base + "/chat/completions", api_key.strip(), {
        "model": model.strip(), "max_tokens": 24,
        "messages": [{"role": "user", "content": "Ответь одним словом: готов."}]})
    if code != 200:
        # некоторые серверы повторяют ключ в тексте ошибки: в окно он попадать не должен
        raise RuntimeError(explain_endpoint_error(code, payload).replace(api_key.strip(), "***"))
    try:
        return str(payload["choices"][0]["message"]["content"]).strip()[:200] or "(пустой ответ)"
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"Сервер ответил, но не в формате OpenAI: {str(payload)[:200]}") from None


def list_endpoint_models(base_url: str, api_key: str) -> List[str]:
    base = normalize_base_url(base_url)
    if not api_key.strip():
        raise RuntimeError("вставьте API-ключ")
    code, payload = _http_json(base + "/models", api_key.strip())
    if code != 200:
        raise RuntimeError(explain_endpoint_error(code, payload).replace(api_key.strip(), "***"))
    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise RuntimeError("Сервер не отдал список моделей. Введите название вручную.")
    return sorted({str(i.get("id")) for i in items if isinstance(i, dict) and i.get("id")})


# --------------------------------------------------------------------------
# Настройки Джарвиса (settings.json)
# --------------------------------------------------------------------------

REPLACE_PATHS = {("apps",), ("game_mode", "kill"), ("wake", "words")}
VOICE_RE = re.compile(r"^[a-z]{2,3}-[A-Z]{2}-[A-Za-z0-9]+Neural$")


def read_user_settings() -> Dict[str, Any]:
    path = paths.SETTINGS_PATH
    try:
        return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    except (OSError, ValueError):
        return {}


def merge_into(dst: Dict[str, Any], src: Dict[str, Any], path: Tuple[str, ...] = ()) -> None:
    for key, val in src.items():
        here = path + (key,)
        if here in REPLACE_PATHS or not isinstance(val, dict):
            dst[key] = val
        else:
            sub = dst.get(key)
            if not isinstance(sub, dict):
                sub = {}
                dst[key] = sub
            merge_into(sub, val, here)


def clamp(value: Any, lo: float, hi: float, default: float) -> float:
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return default


def sanitize_settings(incoming: Dict[str, Any]) -> Dict[str, Any]:
    """Оставляет только известные поля и приводит значения к нужным типам."""
    out: Dict[str, Any] = {}

    def text(d: Dict[str, Any], key: str, limit: int = 200) -> Optional[str]:
        v = d.get(key)
        return str(v).strip()[:limit] if isinstance(v, (str, int, float)) else None

    for key in ("name", "address", "city"):
        v = text(incoming, key, 60)
        if v:
            out[key] = v
    if isinstance(incoming.get("greeting"), bool):
        out["greeting"] = incoming["greeting"]

    w = incoming.get("wake")
    if isinstance(w, dict):
        wo: Dict[str, Any] = {}
        if isinstance(w.get("words"), list):
            words = [str(x).strip().lower() for x in w["words"] if str(x).strip()]
            if words:
                wo["words"] = words[:40]
        if "followup_seconds" in w:
            wo["followup_seconds"] = clamp(w["followup_seconds"], 0, 60, 12)
        if wo:
            out["wake"] = wo

    a = incoming.get("audio")
    if isinstance(a, dict):
        ao: Dict[str, Any] = {}
        if "device" in a:
            ao["device"] = str(a["device"])[:200]
        if isinstance(a.get("auto_threshold"), bool):
            ao["auto_threshold"] = a["auto_threshold"]
        if "silence_threshold" in a:
            ao["silence_threshold"] = clamp(a["silence_threshold"], 0.002, 0.3, 0.012)
        out["audio"] = ao

    st = incoming.get("stt")
    if isinstance(st, dict) and st.get("model") in ("tiny", "base", "small", "medium", "large-v3"):
        out["stt"] = {"model": st["model"]}

    t = incoming.get("tts")
    if isinstance(t, dict):
        to: Dict[str, Any] = {}
        if isinstance(t.get("enabled"), bool):
            to["enabled"] = t["enabled"]
        if t.get("backend") in ("auto", "edge", "sapi"):
            to["backend"] = t["backend"]
        if isinstance(t.get("voice"), str) and VOICE_RE.match(t["voice"]):
            to["voice"] = t["voice"]
        for k in ("rate", "pitch"):
            v = text(t, k, 12)
            if v and re.fullmatch(r"[+-]\d{1,3}(%|Hz)", v):
                to[k] = v
        if "max_chars" in t:
            to["max_chars"] = int(clamp(t["max_chars"], 100, 2000, 600))
        out["tts"] = to

    h = incoming.get("hermes")
    if isinstance(h, dict):
        ho: Dict[str, Any] = {}
        for k in ("provider", "model"):
            if k in h:
                v = str(h[k]).strip()[:120]
                if re.fullmatch(r"[A-Za-z0-9._:/@+\- ]*", v):
                    ho[k] = v
        if isinstance(h.get("yolo"), bool):
            ho["yolo"] = h["yolo"]
        out["hermes"] = ho

    u = incoming.get("updates")
    if isinstance(u, dict) and isinstance(u.get("auto_check"), bool):
        out["updates"] = {"auto_check": u["auto_check"]}

    g = incoming.get("game_mode")
    if isinstance(g, dict) and isinstance(g.get("kill"), list):
        out["game_mode"] = {"kill": [str(x).strip()[:80] for x in g["kill"] if str(x).strip()][:60]}

    apps = incoming.get("apps")
    if isinstance(apps, dict):
        out["apps"] = {str(k).strip().lower()[:60]: str(v).strip()[:400]
                       for k, v in apps.items() if str(k).strip() and str(v).strip()}
    return out


def save_settings(incoming: Dict[str, Any]) -> Dict[str, Any]:
    clean = sanitize_settings(incoming)
    user = read_user_settings()
    merge_into(user, clean)
    path = paths.SETTINGS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(user, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return clean


def public_settings() -> Dict[str, Any]:
    from .core import load_settings
    cfg = load_settings()
    user = read_user_settings()
    return {
        "name": cfg["name"], "address": cfg["address"], "city": cfg["city"], "greeting": cfg["greeting"],
        "wake": {"words": list(cfg["wake"]["words"]), "followup_seconds": cfg["wake"]["followup_seconds"]},
        "audio": {k: cfg["audio"].get(k) for k in ("device", "auto_threshold", "silence_threshold")},
        "stt": {"model": cfg["stt"]["model"]},
        "tts": {k: cfg["tts"].get(k) for k in ("enabled", "backend", "voice", "rate", "pitch", "max_chars")},
        "hermes": {k: cfg["hermes"].get(k) for k in ("provider", "model", "yolo")},
        "game_mode": {"kill": list((user.get("game_mode") or {}).get("kill", []))},
        "apps": dict(user.get("apps", {})),
        "updates": {"auto_check": bool(cfg["updates"]["auto_check"])},
    }


def hermes_snapshot() -> Dict[str, Any]:
    """Всё, что окно показывает про Hermes: найден ли, что настроено, какие ключи заданы."""
    from .core import find_hermes, load_settings
    home = hermes_home()
    env = read_env_file(home / ".env")
    cfg = load_settings()
    now = read_hermes_model(home)
    return {
        "exe": find_hermes(cfg["hermes"].get("exe", "")) or "", "home": str(home), "now": now,
        "keys": {p["env"]: mask(env.get(p["env"], "")) for p in PROVIDERS if p["env"]},
        "custom_key": mask(env.get(CUSTOM_KEY_ENV, "")),
        "telegram": {"token": mask(env.get("TELEGRAM_BOT_TOKEN", "")), "users": env.get("TELEGRAM_ALLOWED_USERS", "")},
    }


def save_telegram(token: Optional[str], users: str, clear_token: bool = False) -> None:
    envp = hermes_home() / ".env"
    if not re.fullmatch(r"[0-9,\s]*", users or ""):
        raise ValueError("ID пользователей: только цифры через запятую")
    if token:
        write_env_value(envp, "TELEGRAM_BOT_TOKEN", token.strip())
    if clear_token:
        write_env_value(envp, "TELEGRAM_BOT_TOKEN", None)
    write_env_value(envp, "TELEGRAM_ALLOWED_USERS", (users or "").replace(" ", "") or None)


def stored_key(env_name: str) -> str:
    return read_env_file(hermes_home() / ".env").get(env_name, "") if env_name else ""


def run_hermes(args: List[str], emit: Callable[[str], None], timeout: float = 90) -> int:
    from .core import ANSI_RE, find_hermes, load_settings
    exe = find_hermes(load_settings()["hermes"].get("exe", ""))
    if not exe:
        raise RuntimeError("Hermes не найден")
    proc = subprocess.Popen([exe] + args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW,
                            env=dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8"))
    import threading
    timer = threading.Timer(timeout, proc.kill)
    timer.start()
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            emit(ANSI_RE.sub("", line.rstrip()))
        return proc.wait()
    finally:
        timer.cancel()


def restart_gateway(emit: Callable[[str], None]) -> str:
    emit("Перезапускаю шлюз Hermes…")
    code = run_hermes(["gateway", "restart", "--yes"], emit)
    if code != 0:
        emit("Перезапуск не удался, пробую запуск…")
        code = run_hermes(["gateway", "start"], emit)
    return "Шлюз Telegram перезапущен." if code == 0 else \
        "Ключи сохранены, но шлюз не перезапустился. Смотрите вывод выше."


# --------------------------------------------------------------------------
# Автозапуск (реестр, без ярлыков и PowerShell)
# --------------------------------------------------------------------------

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "Jarvis"


def autostart_command() -> str:
    if FROZEN:
        return f'"{paths.exe_path()}" --minimized'
    exe = paths.exe_path()
    if IS_WIN and exe.name.lower() == "python.exe" and exe.with_name("pythonw.exe").exists():
        exe = exe.with_name("pythonw.exe")
    return f'"{exe}" -m jarvis --minimized'


def autostart_enabled() -> bool:
    if not IS_WIN:
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, RUN_NAME)
        return bool(value)
    except OSError:
        return False


def set_autostart(enabled: bool) -> None:
    if not IS_WIN:
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_NAME)
            except OSError:
                pass


def remove_legacy_autostart() -> None:
    """Старая версия запускалась ярлыком Jarvis.lnk из папки «Автозагрузка»."""
    if not IS_WIN:
        return
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return
    lnk = Path(appdata) / "Microsoft/Windows/Start Menu/Programs/Startup/Jarvis.lnk"
    try:
        lnk.unlink()
    except OSError:
        pass


# --------------------------------------------------------------------------
# Навык jarvis-pc для Hermes
# --------------------------------------------------------------------------

def skill_target() -> Path:
    return hermes_home() / "skills" / "system" / "jarvis-pc"


def pc_command() -> str:
    """Строка запуска инструментов ПК, которую Hermes подставит в терминал."""
    if FROZEN:
        return f'"{paths.exe_path().parent / "pc" / "jarvis-pc.exe"}"'.replace("\\", "/")
    exe = paths.exe_path()
    if IS_WIN and exe.name.lower() == "pythonw.exe":
        exe = exe.with_name("python.exe")
    pc = skill_target() / "scripts" / "pc.py"
    return f'"{exe}" "{pc}"'.replace("\\", "/")


def _skill_hash() -> str:
    import hashlib
    h = hashlib.sha1()
    base = RES_DIR / "jarvis-pc"
    for rel in ("SKILL.md", "scripts/pc.py"):
        try:
            h.update((base / rel).read_bytes())
        except OSError:
            pass
    h.update(pc_command().encode("utf-8"))
    return h.hexdigest()


def install_skill() -> str:
    src = RES_DIR / "jarvis-pc"
    if not src.exists():
        raise RuntimeError("в приложении нет папки навыка jarvis-pc")
    dst = skill_target()
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, dirs_exist_ok=True)
    md = dst / "SKILL.md"
    text = md.read_text(encoding="utf-8").replace("{{PC_CMD}}", pc_command())
    md.write_text(text, encoding="utf-8", newline="\n")
    (dst / "scripts" / "settings.path").write_text(str(paths.SETTINGS_PATH), encoding="utf-8")
    (dst / ".jarvis-hash").write_text(_skill_hash(), encoding="utf-8")
    return str(dst)


def skill_state() -> Dict[str, bool]:
    dst = skill_target()
    try:
        current = (dst / ".jarvis-hash").read_text(encoding="utf-8").strip() == _skill_hash()
    except OSError:
        current = False
    return {"installed": dst.exists(), "current": current}


def ensure_skill() -> None:
    if hermes_home().exists() and not skill_state()["current"]:
        install_skill()


def disable_hermes_wake_word() -> None:
    """Встроенное слово-будильник Hermes слушает тот же микрофон, что и Джарвис. Выключаем один раз."""
    from .core import find_hermes, load_settings
    marker = paths.DATA_DIR / ".hermes-wake-off"
    if marker.exists():
        return
    exe = find_hermes(load_settings()["hermes"].get("exe", ""))
    if not exe:
        return
    done = subprocess.run([exe, "config", "set", "wake_word.enabled", "false"], capture_output=True,
                          stdin=subprocess.DEVNULL, creationflags=NO_WINDOW, timeout=30)
    if done.returncode == 0:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("1", encoding="utf-8")


def import_legacy_settings() -> Optional[Path]:
    """Первый запуск: подхватить settings.json от версии, жившей в папке на рабочем столе."""
    if paths.SETTINGS_PATH.exists():
        return None
    home = Path.home()
    for cand in (home / "Desktop/Hermes agent/Jarvis/settings.json", home / "Desktop/Hermes agent/jarvis/settings.json"):
        if cand.exists():
            try:
                data = json.loads(cand.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            data.get("tts", {}).pop("elevenlabs", None) if isinstance(data.get("tts"), dict) else None
            paths.SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            paths.SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            return cand
    return None


def first_run_setup() -> None:
    """Вызывается при каждом старте приложения: приводит окружение в порядок. Ошибки не мешают запуску."""
    from .core import log
    for step in (import_legacy_settings, remove_legacy_autostart, ensure_skill, disable_hermes_wake_word):
        try:
            step()
        except Exception:  # noqa: BLE001
            log.exception("Шаг подготовки %s не выполнен", step.__name__)


# --------------------------------------------------------------------------
# Звук: список микрофонов, проверка уровня, прослушивание голоса
# --------------------------------------------------------------------------

def list_input_devices() -> List[Dict[str, str]]:
    """Микрофоны для выпадающего списка. Пусто, если звуковая библиотека недоступна."""
    try:
        import sounddevice as sd
        apis = sd.query_hostapis()
        default_in = sd.default.device[0] if sd.default.device else -1
        out = []
        for idx, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] <= 0:
                continue
            api = apis[d["hostapi"]]["name"] if d.get("hostapi") is not None and d["hostapi"] < len(apis) else ""
            mark = " (по умолчанию)" if idx == default_in else ""
            out.append({"id": str(idx), "label": f"{d['name']}{mark} · {api}".strip(" ·")})
        return out
    except Exception:  # noqa: BLE001 — нет PortAudio или нет устройств
        return []


def mic_level(device: str = "", seconds: float = 2.5) -> Dict[str, float]:
    """Короткая запись: средний и пиковый уровень, чтобы человек видел, что микрофон жив."""
    import numpy as np
    import sounddevice as sd
    dev = int(device) if str(device).isdigit() else (device or None)
    frames = sd.rec(int(seconds * 16000), samplerate=16000, channels=1, dtype="float32", device=dev)
    sd.wait()
    data = frames[:, 0]
    return {"rms": float(np.sqrt(np.mean(np.square(data)))), "peak": float(np.max(np.abs(data)))}


def preview_voice(tts_cfg: Dict[str, Any], text: str) -> str:
    """Синтез и воспроизведение пробной фразы. Бросает RuntimeError с причиной, если голос не работает."""
    import tempfile
    from .core import IS_WIN, Speaker, play_mp3
    sp = Speaker(dict(tts_cfg, enabled=True))
    fd, path = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)
    try:
        try:
            sp.synth(text, path)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"голос Microsoft не отвечает: {exc}") from None
        if not IS_WIN:
            return "Голос синтезирован (воспроизведение работает только в Windows)."
        play_mp3(path)
        return "Готово."
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
