"""Компиляция LaTeX в PDF: Tectonic (авто-скачивание) / xelatex / pdflatex / lualatex.

Tectonic — один бинарь с GitHub, не требует установки TeX Live.
Если движок не найден, пробуем автоматически скачать Tectonic под вашу ОС.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import LatexConfig, get_config
from .utils import LatexCompileError, get_logger, log_event

ProgressFn = Callable[[str], None]


@dataclass
class CompileResult:
    """Результат компиляции."""

    success: bool
    pdf_path: Path | None
    log: str
    engine: str = ""


def find_engine(preference: tuple[str, ...] = ("tectonic", "xelatex", "pdflatex", "lualatex")) -> str | None:
    """Найти доступный LaTeX-движок.

    Args:
        preference: Порядок предпочтения.

    Returns:
        Имя движка или None.
    """
    for name in preference:
        if shutil.which(name):
            return name
    return None


def _tectonic_asset() -> tuple[str, str] | None:
    """Определить asset Tectonic для текущей платформы (имя файла, URL)."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    # Маппинг: (os, arch) -> (asset_substring, ext)
    # Релизы: https://github.com/tectonic-typesetting/tectonic/releases
    # Имена вида: tectonic-0.15.0-x86_64-unknown-linux-gnu.tar.gz
    base = get_config().latex.tectonic_release_base
    # Получаем latest через GitHub API
    api = "https://api.github.com/repos/tectonic-typesetting/tectonic/releases/latest"
    try:
        req = urllib.request.Request(api, headers={"User-Agent": "Universum/1.0", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            import json

            data = json.loads(resp.read().decode("utf-8"))
        assets = data.get("assets", [])
        if system == "linux":
            want = "x86_64-unknown-linux-gnu" if "x86_64" in machine or "amd64" in machine else "aarch64-unknown-linux-gnu"
        elif system == "darwin":
            want = "x86_64-apple-darwin" if "x86_64" in machine else "aarch64-apple-darwin"
        elif system == "windows":
            want = "x86_64-pc-windows-msvc"
        else:
            return None
        for a in assets:
            name = a.get("name", "")
            if want in name and name.endswith((".tar.gz", ".zip")):
                return name, a.get("browser_download_url", "")
    except Exception as e:
        get_logger().warning("Tectonic release lookup failed: %s", e)
    return None


def ensure_tectonic(progress: ProgressFn | None = None) -> Path | None:
    """Скачать Tectonic в temp-папку, если движка нет. Вернуть путь к бинарю или None."""
    logger = get_logger()
    found = _tectonic_asset()
    if not found:
        logger.warning("Could not determine Tectonic asset for this platform")
        return None
    name, url = found
    if progress:
        progress("Скачивание LaTeX-движка Tectonic (первый запуск, ~30 МБ)…")
    dest_dir = get_config().temp_dir / "tectonic"
    dest_dir.mkdir(parents=True, exist_ok=True)
    archive = dest_dir / name
    binary = dest_dir / ("tectonic.exe" if platform.system().lower() == "windows" else "tectonic")
    if binary.exists():
        return binary
    try:
        log_event(f"Downloading tectonic {name}")
        req = urllib.request.Request(url, headers={"User-Agent": "Universum/1.0"})
        with urllib.request.urlopen(req, timeout=120) as resp, open(archive, "wb") as f:
            shutil.copyfileobj(resp, f)
        if name.endswith(".zip"):
            with zipfile.ZipFile(archive) as z:
                z.extractall(dest_dir)
        else:
            import tarfile

            with tarfile.open(archive, "r:gz") as tar:
                tar.extractall(dest_dir)
        # Найти бинарь внутри (может быть во вложенной папке)
        candidates = list(dest_dir.rglob("tectonic*"))
        for c in candidates:
            if c.is_file() and c.name in ("tectonic", "tectonic.exe"):
                if platform.system().lower() != "windows":
                    c.chmod(0o755)
                return c
        return None
    except Exception as e:
        logger.warning("Tectonic download failed: %s", e)
        return None


def compile_latex(
    latex_code: str,
    *,
    config: LatexConfig | None = None,
    progress: ProgressFn | None = None,
    cancel_event=None,
) -> CompileResult:
    """Скомпилировать LaTeX-код в PDF.

    Args:
        latex_code: Полный LaTeX-документ.
        config: Конфигурация компиляции.
        progress: Колбэк прогресса.
        cancel_event: Событие отмены.

    Returns:
        CompileResult (success=False + log при ошибке).
    """
    cfg = config or get_config().latex
    logger = get_logger()
    workdir = Path(tempfile.mkdtemp(prefix="universum_latex_"))
    tex_file = workdir / "document.tex"
    tex_file.write_text(latex_code, encoding="utf-8")
    log_event(f"LaTeX compile workdir={workdir} chars={len(latex_code)}")

    engine = find_engine(cfg.engine_preference)
    engine_cmd: list[str] | None = None
    if engine == "tectonic":
        engine_cmd = ["tectonic", "-X", "compile", "--keep-logs", "document.tex"]
    elif engine == "xelatex":
        engine_cmd = ["xelatex", "-interaction=nonstopmode", "-halt-on-error", "document.tex"]
    elif engine == "pdflatex":
        engine_cmd = ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "document.tex"]
    elif engine == "lualatex":
        engine_cmd = ["lualatex", "-interaction=nonstopmode", "-halt-on-error", "document.tex"]
    else:
        # Попытка авто-скачивания Tectonic
        tectonic_path = ensure_tectonic(progress)
        if tectonic_path is not None:
            engine = "tectonic"
            engine_cmd = [str(tectonic_path), "-X", "compile", "--keep-logs", "document.tex"]
        else:
            log = "LaTeX-движок не найден (tectonic/xelatex/pdflatex/lualatex). Установите TeX Live или MiKTeX, либо проверьте интернет для авто-скачивания Tectonic."
            return CompileResult(success=False, pdf_path=None, log=log, engine="")

    if progress:
        progress(f"Компиляция LaTeX ({engine})…")
    # Для xelatex/pdflatex/lualatex может понадобиться 2 прохода (оглавление/ссылки)
    passes = 2 if engine in ("xelatex", "pdflatex", "lualatex") else 1
    full_log = ""
    try:
        for i in range(passes):
            if cancel_event is not None and cancel_event.is_set():
                return CompileResult(success=False, pdf_path=None, log="Компиляция отменена пользователем.", engine=engine)
            proc = subprocess.run(
                engine_cmd,
                cwd=str(workdir),
                capture_output=True,
                text=True,
                timeout=cfg.compile_timeout_sec,
            )
            full_log += proc.stdout + "\n" + proc.stderr + "\n"
            if proc.returncode != 0:
                break
    except subprocess.TimeoutExpired:
        return CompileResult(success=False, pdf_path=None, log=full_log + "\nТаймаут компиляции.", engine=engine)
    except FileNotFoundError:
        return CompileResult(success=False, pdf_path=None, log=f"Движок '{engine}' не найден.", engine=engine)
    except Exception as e:
        return CompileResult(success=False, pdf_path=None, log=full_log + f"\nОшибка запуска: {e}", engine=engine)

    pdf_path = workdir / "document.pdf"
    if pdf_path.exists() and pdf_path.stat().st_size > 0:
        log_event(f"LaTeX compile success engine={engine} pdf_bytes={pdf_path.stat().st_size}")
        return CompileResult(success=True, pdf_path=pdf_path, log=full_log, engine=engine)
    # Иногда tectonic кладёт PDF с другим именем — ищем любой .pdf
    pdfs = list(workdir.glob("*.pdf"))
    if pdfs:
        return CompileResult(success=True, pdf_path=pdfs[0], log=full_log, engine=engine)
    return CompileResult(success=False, pdf_path=None, log=full_log or "PDF не создан.", engine=engine)


def compile_with_repair(
    latex_code: str,
    llm_generate,  # Callable[[str, str], str] — (system, user) -> text
    *,
    config: LatexConfig | None = None,
    progress: ProgressFn | None = None,
    cancel_event=None,
) -> CompileResult:
    """Компиляция с авто-ремонтом через LLM (до N попыток).

    Args:
        latex_code: Исходный LaTeX.
        llm_generate: Функция вызова LLM (system, user) -> ответ.
        config: Конфигурация.
        progress: Колбэк прогресса.
        cancel_event: Событие отмены.

    Returns:
        Успешный CompileResult или последний неудачный (с логом).
    """
    from .prompts import SYSTEM_PROMPT, build_repair_prompt
    from .utils import extract_latex, validate_latex

    cfg = config or get_config().latex
    current = latex_code
    last: CompileResult | None = None
    for attempt in range(cfg.max_repair_attempts + 1):
        if cancel_event is not None and cancel_event.is_set():
            raise LatexCompileError("Компиляция отменена пользователем.", log="")
        if progress:
            progress(f"Компиляция LaTeX (попытка {attempt + 1})…")
        result = compile_latex(current, config=cfg, progress=None, cancel_event=cancel_event)
        last = result
        if result.success:
            return result
        if attempt >= cfg.max_repair_attempts:
            break
        # Авто-ремонт
        if progress:
            progress(f"Исправление ошибок LaTeX (попытка {attempt + 1}/{cfg.max_repair_attempts})…")
        log_event(f"LaTeX repair attempt={attempt + 1}")
        try:
            repair_prompt = build_repair_prompt(current, result.log)
            fixed_raw = llm_generate(SYSTEM_PROMPT, repair_prompt)
            fixed = extract_latex(fixed_raw)
            problems = validate_latex(fixed)
            if problems:
                get_logger().warning("Repair produced invalid LaTeX: %s", problems)
            if not fixed:
                continue
            current = fixed
        except Exception as e:
            get_logger().warning("Repair LLM call failed: %s", e)
            continue
    assert last is not None
    raise LatexCompileError(
        "Не удалось скомпилировать PDF после авто-исправлений. Лог ошибки — ниже.",
        log=last.log,
    )
