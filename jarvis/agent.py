"""Встроенный «мозг» Джарвиса: короткий цикл «модель → команда ПК → результат → ответ» без Hermes.

Модель получает краткое описание команд и отвечает либо обычным текстом, либо строками `ДЕЙСТВИЕ: команда аргументы`.
Мы выполняем команды через jarvis-pc (отдельная программа: её сбой или зависание не ломает Джарвиса) и возвращаем
модели результат. Такой текстовый протокол работает с любой моделью, в том числе с сайтов-перепродавцов, у которых
вызов функций часто не поддерживается.

Простейшее (время, громкость, окна, скриншот) вообще не доходит до модели: `jarvis.quick`."""
from __future__ import annotations

import logging
import ntpath
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import backend, llm

log = logging.getLogger("jarvis")

MAX_STEPS = 5
MAX_ACTIONS_PER_STEP = 3
RESULT_LIMIT = 1500
TOOL_TIMEOUT_S = 40.0

# Что умеет jarvis-pc. Это единственный список, который видит модель; тест сверяет его с pc.py.
CATALOG: List[Tuple[str, str]] = [
    ("now", "текущее время и дата"),
    ("weather [город]", "погода"),
    ("rate [валюта]", "курс валюты (доллар, евро, юань…)"),
    ("open <программа или сайт>", "открыть программу, например блокнот, хром, ютуб"),
    ("volume up|down|mute|set N", "громкость"),
    ("minimize-all", "свернуть все окна"),
    ("restore-all", "вернуть окна"),
    ("maximize", "развернуть окно"),
    ("minimize", "свернуть окно"),
    ("lock", "заблокировать компьютер"),
    ("timer N", "выключить компьютер через N минут"),
    ("timer-cancel", "отменить выключение"),
    ("game-mode on|off", "игровой режим"),
    ("files list [путь]", "список файлов (без пути: рабочий стол)"),
    ("files search <имя> [--to папка]", "найти файл"),
    ("files size <путь>", "размер файла или папки"),
    ("files move <путь> --to <куда>", "переместить"),
    ("files copy <путь> --to <куда>", "скопировать"),
    ("files delete <путь>", "удалить в корзину"),
    ("files create-folder <путь>", "создать папку"),
    ("files read <путь>", "прочитать текстовый файл"),
    ("processes [имя]", "запущенные процессы"),
    ("process-kill <имя или PID>", "завершить процесс"),
    ("sysinfo", "процессор, память, диски, батарея"),
    ("network", "состояние сети"),
    ("clipboard get|set <текст>", "буфер обмена"),
    ("screenshot [путь]", "снимок экрана"),
    ("run <команда>", "выполнить команду Windows"),
    ("notify <текст>", "показать уведомление"),
    ("task list|run|show|delete <название>", "записанные пользователем задачи"),
]
COMMANDS = {line.split()[0] for line, _ in CATALOG}

SYSTEM = (
    "Ты — {name}, ИИ-ассистент на компьютере хозяина (Windows). Умный, находчивый, слегка ироничный, всегда "
    "уважительный. Отвечай по-русски, строго на «вы», обращайся «{address}». Ответ зачитывается вслух: одна-три "
    "короткие фразы, без разметки, списков, эмодзи и ссылок.\n"
    "Сейчас: {now}.\n\n"
    "Если для ответа нужно что-то сделать на компьютере или узнать с него, ответь ТОЛЬКО строками вида\n"
    "ДЕЙСТВИЕ: команда аргументы\n"
    "(до трёх строк, ничего больше). Ты получишь РЕЗУЛЬТАТ и тогда ответишь человеку словами. "
    "Если действие не нужно (вопрос, расчёт, разговор), просто ответь.\n\n"
    "Команды:\n{commands}\n\n"
    "Правила: не выдумывай результат, пересказывай то, что вернула команда; если команда не удалась, скажи прямо. "
    "Опасное (удаление, завершение процессов, выключение) делай, только когда об этом просили прямо. "
    "Пути с пробелами бери в кавычки."
)

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def system_prompt(name: str, address: str, now: Optional[time.struct_time] = None) -> str:
    t = now or time.localtime()
    stamp = f"{WEEKDAYS[t.tm_wday]}, {t.tm_mday} {MONTHS[t.tm_mon - 1]} {t.tm_year}, {t.tm_hour:02d}:{t.tm_min:02d}"
    cmds = "\n".join(f"- {line} — {what}" for line, what in CATALOG)
    return SYSTEM.format(name=name, address=address, now=stamp, commands=cmds)


# --------------------------------------------------------------------------
# Разбор ответа модели и безопасность команд
# --------------------------------------------------------------------------

ACTION_RE = re.compile(r"^\s*(?:ДЕЙСТВИЕ|ACTION)\s*[:：]\s*(.+?)\s*$", re.I | re.M)
TOKEN_RE = re.compile(r'"[^"]*"|\S+')


def parse_actions(reply: str) -> Tuple[List[str], str]:
    """Строки-команды из ответа и остаток текста."""
    found = [m.group(1).strip().strip("`") for m in ACTION_RE.finditer(reply)]
    rest = ACTION_RE.sub("", reply).strip()
    return found, rest


def split_args(line: str) -> List[str]:
    """Разбивает строку на аргументы; кавычки снимаются, обратные слэши Windows сохраняются."""
    return [t[1:-1] if len(t) >= 2 and t[0] == t[-1] == '"' else t for t in TOKEN_RE.findall(line)]


DANGEROUS_RUN = re.compile(
    r"\b(format|diskpart|bcdedit|cipher\s+/w|takeown|vssadmin|shutdown|net\s+user|reg\s+(delete|add)|"
    r"rd\s+/s|rmdir\s+/s|del\s+.*(/s|/q)|remove-item\s+.*-recurse)\b", re.I)
PROTECTED_PROCESSES = {"system", "csrss", "wininit", "winlogon", "services", "lsass", "smss", "svchost", "dwm",
                       "registry", "memory compression"}
PROTECTED_PATHS = ("c:\\windows", "c:\\program files", "c:\\program files (x86)", "c:\\programdata")


def _protected_path(path: str) -> bool:
    """Корень диска, папка «Пользователи», профиль пользователя целиком и системные каталоги."""
    p = ntpath.normpath(path).lower().rstrip("\\")
    if len(p) <= 2 or p == "c:\\users" or ntpath.dirname(p) == "c:\\users":
        return True
    return any(p == base or p.startswith(base + "\\") for base in PROTECTED_PATHS)


def check_allowed(tokens: List[str]) -> Optional[str]:
    """Причина отказа или None. Это страховка от ошибки модели, а не замена здравому смыслу пользователя."""
    if not tokens:
        return "пустая команда"
    cmd = tokens[0]
    if cmd not in COMMANDS:
        return f"неизвестная команда «{cmd}»"
    rest = " ".join(tokens[1:])
    if cmd == "run" and DANGEROUS_RUN.search(rest):
        return "эта команда может навредить системе, я её не выполняю"
    if cmd == "process-kill" and rest.lower().removesuffix(".exe").strip() in PROTECTED_PROCESSES:
        return "это системный процесс, его нельзя завершать"
    if cmd == "files" and len(tokens) > 2 and tokens[1] in ("delete", "move"):
        if _protected_path(tokens[2]):
            return "это системная или корневая папка, её трогать нельзя"
    return None


def run_pc(tokens: List[str], timeout: float = TOOL_TIMEOUT_S) -> Tuple[bool, str]:
    """Запускает jarvis-pc отдельным процессом. Возвращает (успех, текст)."""
    try:
        proc = subprocess.run(backend.pc_argv() + tokens, capture_output=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return False, "команда выполнялась слишком долго и была остановлена"
    except OSError as exc:
        return False, f"не удалось запустить инструмент: {exc}"
    text = (proc.stdout or b"").decode("utf-8", "replace").strip()
    return proc.returncode == 0, text or ("Готово." if proc.returncode == 0 else "Не получилось.")


# --------------------------------------------------------------------------
# Цикл
# --------------------------------------------------------------------------

@dataclass
class Answer:
    text: str
    steps: int = 0
    llm_s: float = 0.0
    tools_s: float = 0.0
    actions: List[str] = field(default_factory=list)


class Agent:
    def __init__(self, cfg: Dict[str, Any], endpoint: llm.Endpoint,
                 runner: Callable[[List[str]], Tuple[bool, str]] = run_pc,
                 chat: Callable[..., str] = llm.chat):
        self.cfg, self.endpoint, self.runner, self.chat = cfg, endpoint, runner, chat
        eng = cfg.get("engine", {})
        self.timeout = float(eng.get("llm_timeout_s", 40))
        self.max_steps = int(eng.get("max_steps", MAX_STEPS))

    def ask(self, command: str, history: Optional[List[Tuple[str, str]]] = None,
            cancel: Optional[Any] = None) -> Answer:
        msgs: List[Dict[str, str]] = [{"role": "system", "content": system_prompt(self.cfg["name"], self.cfg["address"])}]
        for user, reply in (history or [])[-6:]:
            msgs += [{"role": "user", "content": user}, {"role": "assistant", "content": reply[:300]}]
        msgs.append({"role": "user", "content": command})
        out = Answer("")
        last_results = ""
        for step in range(1, self.max_steps + 1):
            if cancel is not None and cancel.is_set():
                break
            t0 = time.monotonic()
            reply = self.chat(self.endpoint, msgs, timeout=self.timeout)
            out.llm_s += time.monotonic() - t0
            out.steps = step
            actions, text = parse_actions(reply)
            if not actions:
                out.text = text or reply.strip()
                return out
            results = []
            for line in actions[:MAX_ACTIONS_PER_STEP]:
                tokens = split_args(line)
                refusal = check_allowed(tokens)
                if refusal:
                    ok, res = False, refusal
                else:
                    t1 = time.monotonic()
                    ok, res = self.runner(tokens)
                    out.tools_s += time.monotonic() - t1
                out.actions.append(line)
                results.append(f"{line} -> {'' if ok else 'ОШИБКА: '}{res[:RESULT_LIMIT]}")
                log.info("Команда: %s -> %s", line, ("ok" if ok else "ошибка") + ": " + res[:120].replace("\n", " "))
            last_results = "\n".join(results)
            msgs.append({"role": "assistant", "content": reply})
            msgs.append({"role": "user", "content": "РЕЗУЛЬТАТ:\n" + last_results +
                         "\nТеперь ответь человеку словами, без строк ДЕЙСТВИЕ, если больше ничего делать не нужно."})
        out.text = ("Сделал, но не смог подвести итог: " + last_results.split("->", 1)[-1].strip()[:200]) if last_results \
            else "Не успел обдумать ответ."
        return out


def make_agent(cfg: Dict[str, Any]) -> Optional[Agent]:
    """Встроенный движок, если он включён и настроен; иначе None и отвечает Hermes."""
    mode = str(cfg.get("engine", {}).get("mode", "auto"))
    if mode == "hermes":
        return None
    ep = llm.resolve_endpoint(cfg)
    if ep is None:
        if mode == "native":
            log.warning("Встроенный движок выбран, но адрес, ключ или модель не заданы: отвечает Hermes")
        return None
    return Agent(cfg, ep)
