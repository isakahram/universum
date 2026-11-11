"""LLM-клиент: Pollinations (gen.pollinations.ai) + провайдеры с ключом + Ollama.

Реализован на стандартной библиотеке urllib (без внешних зависимостей),
с retry, таймаутами и цепочкой эндпоинтов. OpenAI-совместимый формат.

Pollinations: строго https://gen.pollinations.ai/v1/chat/completions,
модель openai, max_tokens НЕ отправляется (сервер сам решает лимит).
Старый text.pollinations.ai с max_tokens=8192 отвечал 400
output_token_limit_exceeded даже на 1 страницу — поэтому лимит убран.

Ключ отправляется только на удалённый эндпоинт (не на localhost) и никогда
не пишется в логи (только замаскированный вид).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Callable

from .config import LlmConfig, get_config, is_pollinations_url, mask_key
from .utils import LlmError, get_logger, log_event

ProgressFn = Callable[[str], None]


def effective_max_tokens(url: str, configured: int | None) -> int | None:
    """Определить max_tokens для запроса.

    Для Pollinations всегда None (не отправлять — сервер сам решает,
    иначе 400 output_token_limit_exceeded). Для остальных — как настроено
    (None = не отправлять).

    Args:
        url: URL эндпоинта.
        configured: Настроенное значение (None = не отправлять).

    Returns:
        Значение для payload или None (не включать в payload).
    """
    if is_pollinations_url(url):
        return None
    if configured is None or configured <= 0:
        return None
    return configured


def friendly_network_error(reason: object, url: str = "") -> str:
    """Перевести сетевую ошибку в понятное русское сообщение.

    Args:
        reason: Причина из URLError (может быть OSError с errno).
        url: URL эндпоинта (для подсказки про localhost).

    Returns:
        Дружелюбное сообщение на русском.
    """
    text = str(reason)
    is_local = "localhost" in (url or "") or "127.0.0.1" in (url or "")
    # WinError 10061 / Connection refused
    if "10061" in text or "Connection refused" in text or "Errno 111" in text:
        if is_local:
            return (
                "Локальный Ollama не запущен (подключение отклонено). "
                "Запустите Ollama (ollama serve) или выберите другой провайдер в «Настройках нейросети»."
            )
        return (
            "Сервис отклонил подключение (WinError 10061). Проверьте интернет, "
            "файрвол/антивирус и правильность URL в «Настройках нейросети»."
        )
    if "Name or service not known" in text or "getaddrinfo failed" in text or "NameResolutionError" in text:
        return "Не удалось разрешить адрес сервера (DNS). Проверьте интернет и URL."
    if "timed out" in text.lower() or "timeout" in text.lower():
        return "Сервер не ответил вовремя (таймаут). Попробуйте ещё раз через минуту."
    if "SSL" in text or "certificate" in text.lower():
        return f"Ошибка SSL/TLS при подключении: {text[:200]}. Проверьте дату/время системы и интернет."
    return f"Сеть недоступна: {text[:300]}"


def friendly_http_error(code: int, detail: str) -> str:
    """Перевести HTTP-ошибку провайдера в понятное сообщение."""
    d = (detail or "")[:300]
    low = (detail or "").lower()
    if "output_token_limit_exceeded" in low or "token_limit" in low:
        return (
            "Сервер ограничил объём ответа (output_token_limit_exceeded). "
            "Попробуйте уменьшить объём документа (меньше страниц/вопросов) "
            "или выбрать другой провайдер в «Настройках нейросети»."
        )
    if code in (401, 403):
        return (
            f"Провайдер отклонил ключ (HTTP {code}). Проверьте, что ключ верный и не отозван, "
            f"и что URL соответствует провайдеру. Ответ сервера: {d}"
        )
    if code == 404:
        return (
            f"Эндпоинт не найден (HTTP 404). Проверьте URL и модель в «Настройках нейросети». Ответ: {d}"
        )
    if code == 429:
        return f"Превышен лимит запросов (HTTP 429). Подождите минуту и попробуйте снова. Ответ: {d}"
    if code >= 500:
        return f"Сервер провайдера временно недоступен (HTTP {code}). Попробуйте через минуту. Ответ: {d}"
    return f"HTTP {code} от LLM-сервиса: {d}"


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


def build_payload(model: str, system_prompt: str, user_prompt: str, url: str, config: LlmConfig) -> dict:
    """Собрать payload для OpenAI-совместимого chat/completions.

    max_tokens включается только если effective_max_tokens() вернул значение
    (для Pollinations — никогда).
    """
    payload: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": config.temperature,
    }
    mt = effective_max_tokens(url, config.max_tokens)
    if mt is not None:
        payload["max_tokens"] = mt
    return payload


class LlmClient:
    """Клиент нейросети с retry и fallback-цепочкой. Поддерживает API-ключ."""

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
            LlmError: Если все эндпоинты недоступны (с объединёнными ошибками).
        """
        errors: list[str] = []
        for url, model, send_key in self.config.endpoints:
            if cancel_event is not None and cancel_event.is_set():
                raise LlmError("Генерация отменена пользователем.")
            if progress:
                progress(f"Запрос к нейросети ({model})…")
            # Ключ в лог не пишем — только замаскированный вид
            log_event(f"LLM request url={url} model={model} key={mask_key(self.config.api_key) if send_key else 'no-key'} prompt_chars={len(user_prompt)}")
            payload = build_payload(model, system_prompt, user_prompt, url, self.config)
            headers: dict[str, str] = {}
            if send_key and self.config.api_key.strip():
                headers["Authorization"] = f"Bearer {self.config.api_key.strip()}"
            endpoint_error = ""
            for attempt in range(1, self.config.max_retries + 1):
                if cancel_event is not None and cancel_event.is_set():
                    raise LlmError("Генерация отменена пользователем.")
                try:
                    result = self._post_with_headers(url, payload, headers)
                    content = _extract_content(result)
                    log_event(f"LLM success url={url} chars={len(content)}")
                    return content
                except LlmError as e:
                    endpoint_error = str(e)
                    self.logger.warning("LLM attempt %d/%d failed url=%s: %s", attempt, self.config.max_retries, url, e)
                    if attempt < self.config.max_retries:
                        time.sleep(self.config.retry_backoff_sec * attempt)
            if endpoint_error:
                errors.append(f"{url} ({model}): {endpoint_error}")
            self.logger.warning("Endpoint %s exhausted, trying next", url)
        details = "\n".join(errors) if errors else "нет деталей"
        raise LlmError(
            "Нейросеть недоступна. Проверьте интернет, URL/модель/ключ в «Настройках нейросети».\n"
            f"Ошибки по эндпоинтам:\n{details}"
        )

    def test_connection(self, *, timeout: int = 30) -> tuple[bool, str]:
        """Проверить соединение с первым эндпоинтом (лёгкий пробный запрос).

        Args:
            timeout: Таймаут в секундах.

        Returns:
            (ok, сообщение на русском).
        """
        endpoints = self.config.endpoints
        if not endpoints:
            return False, "Нет настроенных эндпоинтов."
        url, model, send_key = endpoints[0]
        payload: dict = {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "temperature": 0,
        }
        # Для Pollinations max_tokens не отправляем; для остальных — 1 (быстрый тест)
        if not is_pollinations_url(url):
            payload["max_tokens"] = 1
        headers: dict[str, str] = {}
        if send_key and self.config.api_key.strip():
            headers["Authorization"] = f"Bearer {self.config.api_key.strip()}"
        try:
            result = self._post_with_headers(url, payload, headers, timeout=timeout)
            _extract_content(result)
            return True, f"Соединение OK: {url} ({model}). Ключ: {mask_key(self.config.api_key) if send_key else 'не требуется'}."
        except LlmError as e:
            return False, str(e)

    def _post_with_headers(self, url: str, payload: dict, headers: dict[str, str], timeout: int | None = None) -> dict:
        """POST с дополнительными заголовками (Authorization — только если передан)."""
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req_headers = {"Content-Type": "application/json", "User-Agent": "Universum/1.0", **headers}
        req = urllib.request.Request(url, data=data, headers=req_headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.config.timeout_sec) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                return json.loads(body)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            raise LlmError(friendly_http_error(e.code, detail)) from e
        except urllib.error.URLError as e:
            raise LlmError(friendly_network_error(e.reason, url)) from e
        except TimeoutError as e:
            raise LlmError("LLM-сервис не ответил вовремя (таймаут).") from e
        except json.JSONDecodeError as e:
            raise LlmError("LLM-сервис вернул не-JSON ответ.") from e
