"""Общая подготовка тестов: данные и каталог Hermes во временной папке, Qt без экрана."""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_TMP = Path(tempfile.mkdtemp(prefix="jarvis-test-"))
os.environ["JARVIS_HOME"] = str(_TMP / "data")
os.environ["HERMES_HOME"] = str(_TMP / "hermes")
os.environ.pop("JARVIS_SETTINGS", None)
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["HF_HUB_DOWNLOAD_TIMEOUT"] = "3"      # тесты зависающего сервера не должны ждать по 20 секунд
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def clean_state():
    """Каждый тест начинает с пустых данных и пустого каталога Hermes."""
    import shutil
    for d in (_TMP / "data", _TMP / "hermes"):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
    yield
