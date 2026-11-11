"""Веб-поиск без ключей: DuckDuckGo (ddgs + fallback на stdlib-скрейпинг).

Используется только если пользователь включил чекбокс «искать актуальные данные».
Возвращает список (название, URL) для вставки в промт и в раздел «Источники» PDF.
"""

from __future__ import annotations

import html
import re
import time
import urllib.parse
import urllib.request
from typing import Callable

from .config import SearchConfig, get_config
from .utils import SearchError, get_logger, log_event

ProgressFn = Callable[[str], None]


def _search_with_ddgs(query: str, max_results: int, timeout: int) -> list[tuple[str, str]]:
    """Поиск через пакет ddgs (бывший duckduckgo-search)."""
    from ddgs import DDGS  # type: ignore

    results: list[tuple[str, str]] = []
    with DDGS(timeout=timeout) as ddgs:
        for r in ddgs.text(query, max_results=max_results):
            title = (r.get("title") or "").strip()
            href = (r.get("href") or r.get("link") or "").strip()
            if href and href.startswith("http"):
                results.append((title or href, href))
    return results


def _search_with_stdlib(query: str, max_results: int, timeout: int) -> list[tuple[str, str]]:
    """Fallback: HTML-скрейпинг html.duckduckgo.com (только stdlib)."""
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Universum)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    # Ищем <a class="result__a" href="...">title</a>
    pattern = re.compile(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        re.DOTALL | re.IGNORECASE,
    )
    results: list[tuple[str, str]] = []
    for href, title_html in pattern.findall(body):
        # DuckDuckGo оборачивает ссылки в /l/?uddg=...
        parsed = urllib.parse.urlparse(html.unescape(href))
        qs = urllib.parse.parse_qs(parsed.query)
        real = qs.get("uddg", [html.unescape(href)])[0]
        title = re.sub(r"<[^>]+>", "", title_html)
        title = html.unescape(title).strip()
        if real.startswith("http"):
            results.append((title or real, real))
        if len(results) >= max_results:
            break
    return results


def web_search(
    query: str,
    *,
    config: SearchConfig | None = None,
    progress: ProgressFn | None = None,
    cancel_event=None,
) -> list[tuple[str, str]]:
    """Найти актуальные данные по запросу.

    Args:
        query: Поисковый запрос (тема из запроса пользователя).
        config: Конфигурация поиска.
        progress: Колбэк прогресса.
        cancel_event: Событие отмены.

    Returns:
        Список (название, URL). Может быть пустым, если ничего не найдено.

    Raises:
        SearchError: При сетевой ошибке после всех попыток.
    """
    cfg = config or get_config().search
    logger = get_logger()
    query = query.strip()
    if not query:
        return []
    # Укоротить запрос для поиска (первые 200 символов, без лишних переносов)
    search_query = re.sub(r"\s+", " ", query)[:200]
    if progress:
        progress("Поиск актуальных данных в интернете…")
    log_event("Web search", preview=search_query)

    last_error: Exception | None = None
    for attempt in range(1, cfg.max_retries + 1):
        if cancel_event is not None and cancel_event.is_set():
            raise SearchError("Поиск отменён пользователем.")
        # Сначала пробуем ddgs, затем stdlib
        for backend in ("ddgs", "stdlib"):
            try:
                if backend == "ddgs":
                    results = _search_with_ddgs(search_query, cfg.max_results, cfg.timeout_sec)
                else:
                    results = _search_with_stdlib(search_query, cfg.max_results, cfg.timeout_sec)
                if results:
                    log_event(f"Web search success backend={backend} n={len(results)}")
                    return results
                logger.info("Web search backend=%s returned 0 results", backend)
            except ImportError:
                logger.info("ddgs not installed, using stdlib backend")
                continue
            except Exception as e:  # сеть, таймаут и т.п.
                last_error = e
                logger.warning("Web search backend=%s failed: %s", backend, e)
        if attempt < cfg.max_retries:
            time.sleep(1.5 * attempt)
    # Поиск не обязателен для успеха: возвращаем пусто, но логируем
    logger.warning("Web search failed after retries: %s", last_error)
    return []
