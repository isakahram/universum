"""Диагностика подключения к нейросети: `python -m universum.diagnose`.

Показывает, какой эндпоинт/модель/ключ реально видит приложение (те же
настройки, что в GUI: env -> keyring -> ~/.universum/settings.json),
отправляется ли ключ в заголовке Authorization, и делает пробный запрос.

Полезно, когда `curl` с ключом работает, а приложение отвечает 401:
видно, совпадают ли ключ и эндпоинт у приложения и у вашей ручной проверки.

Ключ никогда не печатается целиком — только маска.
"""

from __future__ import annotations

import sys

from .config import is_localhost_url, load_llm_settings, mask_key, normalize_api_key
from .config import LlmConfig
from .llm_client import LlmClient, auth_headers


def main() -> int:
    """Запустить диагностику и вернуть код выхода (0 — успех)."""
    settings = load_llm_settings()
    cfg = LlmConfig.from_settings(settings)

    print("=== Universum: диагностика нейросети ===")
    print(f"Провайдер:  {settings.provider.label_ru}")
    print(f"URL:        {cfg.endpoints[0][0]}")
    print(f"Модель:     {cfg.endpoints[0][1]}")
    key = normalize_api_key(cfg.api_key)
    print(f"Ключ:       {mask_key(key)} (длина {len(key)})")
    if not key:
        print("  ! Ключ пустой. Вставьте его в «Настройки нейросети» и нажмите «Сохранить»,")
        print("    либо задайте переменную окружения UNIVERSUM_LLM_KEY.")
    else:
        headers = auth_headers(cfg.primary_url, key, cfg.endpoints[0][2])
        if headers:
            print("Заголовок:  Authorization будет отправлен (Bearer <ключ>)")
        else:
            why = "эндпоинт локальный (localhost)" if is_localhost_url(cfg.primary_url) else "ключ не разрешён"
            print(f"  ! Заголовок Authorization НЕ отправляется: {why}")

    print("\nПробный запрос…")
    client = LlmClient(cfg)
    ok, msg = client.test_connection(timeout=30)
    print(("OK   " if ok else "ОШИБКА ") + msg)
    if not ok and key and "401" in msg:
        print(
            "\nПодсказки при 401 (ключ есть, но сервер его не принял):\n"
            "  1) Ключ должен быть создан на https://enter.pollinations.ai (sk_… или pk_…).\n"
            "  2) Ключ из другого сервиса (OpenAI и т.п.) для Pollinations не подходит.\n"
            "  3) Вставляйте ключ без слова Bearer и без кавычек — лишнее очищается, но проверьте.\n"
            "  4) Сверьте URL: https://gen.pollinations.ai/v1/chat/completions"
        )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
