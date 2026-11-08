"""Обновления из GitHub Releases: проверка, скачивание установщика, запуск тихой установки."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Tuple

from . import REPO, __version__, paths

API = "https://api.github.com/repos/{repo}/releases/latest"
INSTALLER_RE = re.compile(r"^Jarvis-Setup.*\.exe$", re.I)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0)


@dataclass
class Release:
    version: str
    notes: str
    url: str          # страница релиза
    asset_url: str    # прямая ссылка на установщик ("" если не приложен)
    asset_name: str
    size: int
    sha256: str       # "" если контрольной суммы нет


class UpdateError(RuntimeError):
    pass


def parse_version(text: str) -> Tuple[int, ...]:
    """'v3.1.0' -> (3, 1, 0). Всё, что не число, считается нулём."""
    nums = re.findall(r"\d+", (text or "").split("-")[0].split("+")[0])
    return tuple(int(n) for n in nums[:4]) or (0,)


def is_newer(candidate: str, current: str = __version__) -> bool:
    a, b = parse_version(candidate), parse_version(current)
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def _get(url: str, timeout: float = 15, accept: str = "application/vnd.github+json"):
    req = urllib.request.Request(url, headers={
        "Accept": accept, "User-Agent": f"Jarvis-Assistant/{__version__}", "X-GitHub-Api-Version": "2022-11-28"})
    return urllib.request.urlopen(req, timeout=timeout)


def parse_release(data: dict) -> Release:
    tag = str(data.get("tag_name") or "")
    if not tag:
        raise UpdateError("в ответе GitHub нет номера версии")
    asset = next((a for a in data.get("assets", []) if INSTALLER_RE.match(str(a.get("name", "")))), None)
    sha = ""
    body = str(data.get("body") or "")
    m = re.search(r"sha256[:\s`*]+([0-9a-f]{64})", body, re.I)
    if m:
        sha = m.group(1).lower()
    if asset and isinstance(asset.get("digest"), str) and asset["digest"].startswith("sha256:"):
        sha = asset["digest"].split(":", 1)[1].lower()
    return Release(
        version=tag.lstrip("vV"), notes=body.strip(), url=str(data.get("html_url") or ""),
        asset_url=str(asset.get("browser_download_url")) if asset else "",
        asset_name=str(asset.get("name")) if asset else "", size=int(asset.get("size") or 0) if asset else 0,
        sha256=sha)


def check(repo: str = REPO, current: str = __version__) -> Optional[Release]:
    """Возвращает свежий релиз, если он новее установленной версии, иначе None."""
    try:
        with _get(API.format(repo=repo)) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None  # релизов ещё нет
        if exc.code in (403, 429):
            raise UpdateError("GitHub временно ограничил запросы, попробуйте позже") from None
        raise UpdateError(f"GitHub ответил HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise UpdateError(f"нет связи с GitHub: {getattr(exc, 'reason', exc)}") from None
    except ValueError:
        raise UpdateError("GitHub вернул непонятный ответ") from None
    if data.get("draft") or data.get("prerelease"):
        return None
    rel = parse_release(data)
    return rel if is_newer(rel.version, current) else None


def download(rel: Release, progress: Callable[[int, int], None] = lambda done, total: None,
             dest_dir: Optional[Path] = None) -> Path:
    if not rel.asset_url:
        raise UpdateError("к релизу не приложен установщик")
    dest_dir = dest_dir or paths.CACHE_DIR / "update"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / rel.asset_name
    part = dest.with_name(dest.name + ".part")
    h = hashlib.sha256()
    try:
        with _get(rel.asset_url, timeout=60, accept="application/octet-stream") as resp, open(part, "wb") as fh:
            total = int(resp.headers.get("Content-Length") or rel.size or 0)
            done = 0
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                h.update(chunk)
                done += len(chunk)
                progress(done, total)
    except (urllib.error.URLError, OSError) as exc:
        raise UpdateError(f"не удалось скачать: {getattr(exc, 'reason', exc)}") from None
    if rel.size and part.stat().st_size != rel.size:
        part.unlink(missing_ok=True)
        raise UpdateError("файл скачался не полностью, попробуйте ещё раз")
    if rel.sha256 and h.hexdigest() != rel.sha256:
        part.unlink(missing_ok=True)
        raise UpdateError("контрольная сумма не совпала: файл повреждён или подменён")
    os.replace(part, dest)
    return dest


def launch_installer(path: Path) -> None:
    """Тихая установка поверх. Установщик сам закроет Джарвиса и запустит снова."""
    if os.name != "nt":
        raise UpdateError("автоустановка работает только в Windows")
    subprocess.Popen([str(path), "/SILENT", "/SUPPRESSMSGBOXES", "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS", "/NORESTART"],
                     creationflags=DETACHED | NO_WINDOW, close_fds=True)


def cleanup_old(keep: Optional[Path] = None) -> None:
    folder = paths.CACHE_DIR / "update"
    if not folder.exists():
        return
    for f in folder.iterdir():
        if keep is not None and f == keep:
            continue
        try:
            f.unlink()
        except OSError:
            pass
