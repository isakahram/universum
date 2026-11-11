"""LLM-клиент: бесплатная нейросеть без ключей (Pollinations + Ollama fallback).

Реализован на стандартной библиотеке urllib (без внешних зависимостей),
с retry, таймаутами и цепочкой эндпоинтов. OpenAI-совместимый формат.

Основной эндпоинт: https://text.pollinations.ai/openai (бесплатно, без ключа).
Fallback: локальный Ollama (http://localhost:11434), если запущен.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Callable

from .config import LlmConfig, get_config
from .utils import LlmError, get_logger, log_event

ProgressFn = Callable[[str], None]


def _post_json(url: str, payload: dict, timeout: int) -> dict:
    """Выполнить POST с JSON и вернуть распарсенный ответ.

    Args:
        url: URL эндпоинта.
        payload: Тело запроса.
        timeout: Таймаут в секундах.

    Returns:
        Распарсенный JSON-ответ.

    Raises:
        LlmError: При сетевой/HTTP ошибке.
    """
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "Universum/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return json.loads(body)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        raise LlmError(f"HTTP {e.code} от LLM-сервиса: {detail}") from e
    except urllib.error.URLError as e:
        raise LlmError(f"Сеть недоступна: {e.reason}") from e
    except TimeoutError as e:
        raise LlmError("LLM-сервис не ответил вовремя (таймаут).") from e
    except json.JSONDecodeError as e:
        raise LlmError("LLM-сервис вернул не-JSON ответ.") from e


def _extract_content(response: dict) -> str:
    """Извлечь текст из OpenAI-совместимого ответа."""
    try:
        choices = response.get("choices") or []
        if not choices:
            raise LlmError("LLM вернул пустой ответ (нет choices).")
        msg = choices[0].get("message") or {}
        content = msg.get("content") or ""
        if isinstance(content, list):
            content = " ".join(
                part.get("text", "") if isinstance(part, dict) else str(part) for part in content
            )
        content = str(content).strip()
        if not content:
            raise LlmError("LLM вернул пустой текст.")
        return content
    except LlmError:
        raise
    except Exception as e:
        raise LlmError(f"Не удалось разобрать ответ LLM: {e}") from e


class LlmClient:
    """Клиент бесплатной нейросети с retry и fallback-цепочкой."""

    def __init__(self, config: LlmConfig | None = None) -> None:
        self.config = config or get_config().llm
        self.logger = get_logger()

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        progress: ProgressFn | None = None,
        cancel_event=None,  # threading.Event | None
    ) -> str:
        """Сгенерировать текст (LaTeX) по промтам.

        Args:
            system_prompt: Системный промт.
            user_prompt: Пользовательский промт.
            progress: Колбэк прогресса (сообщение).
            cancel_event: Событие отмены (если установлено — прервать).

        Returns:
            Текст ответа модели.

        Raises:
            LlmError: Если все эндпоинты недоступны.
        """
        last_error: Exception | None = None
        for url, model in self.config.endpoints:
            if cancel_event is not None and cancel_event.is_set():
                raise LlmError("Генерация отменена пользователем.")
            if progress:
                progress(f"Запрос к нейросети ({model})…")
            log_event(f"LLM request url={url} model={model} prompt_chars={len(user_prompt)}")
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": self.config.temperature,
                "max_tokens": self.config.max_tokens,
            }
            headers: dict[str, str] = {}
            if self.config.api_key:
                headers["Authorization"] = f"Bearer {self.config.api_key}"
            # Retry внутри одного эндпоинта
            for attempt in range(1, self.config.max_retries + 1):
                if cancel_event is not None and cancel_event.is_set():
                    raise LlmError("Генерация отменена пользователем.")
                try:
                    # urllib не поддерживает кастомные headers через _post_json — передаём через payload trick:
                    # временно добавляем ключ в payload не нужно; используем прямой вызов с headers
                    result = self._post_with_headers(url, payload, headers)
                    content = _extract_content(result)
                    log_event(f"LLM success url={url} chars={len(content)}")
                    return content
                except LlmError as e:
                    last_error = e
                    self.logger.warning("LLM attempt %d/%d failed url=%s: %s", attempt, self.config.max_retries, url, e)
                    if attempt < self.config.max_retries:
                        time.sleep(self.config.retry_backoff_sec * attempt)
            # Следующий эндпоинт в цепочке
            self.logger.warning("Endpoint %s exhausted, trying next", url)
        raise LlmError(
            f"Нейросеть недоступна. Последняя ошибка: {last_error}. "
            "Проверьте интернет или запустите локальный Ollama (ollama serve)."
        ) from last_error

    def _post_with_headers(self, url: str, payload: dict, headers: dict[str, str]) -> dict:
        """POST с дополнительными заголовками (для опционального API-ключа)."""
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req_headers = {"Content-Type": "application/json", "User-Agent": "Universum/1.0", **headers}
        req = urllib.request.Request(url, data=data, headers=req_headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout_sec) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                return json.loads(body)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            raise LlmError(f"HTTP {e.code} от LLM-сервиса: {detail}") from e
        except urllib.error.URLError as e:
            raise LlmError(f"Сеть недоступна: {e.reason}") from e
        except TimeoutError as e:
            raise LlmError("LLM-сервис не ответил вовремя (таймаут).") from e
        except json.JSONDecodeError as e:
            raise LlmError("LLM-сервис вернул не-JSON ответ.") from e
