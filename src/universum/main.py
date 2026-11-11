"""Точка входа Universum: python -m universum.main или universum."""

from __future__ import annotations

import sys


def main() -> int:
    """Запустить приложение. Возвращает код выхода."""
    try:
        from .gui import run
    except ImportError as e:
        print(f"Ошибка запуска GUI: {e}", file=sys.stderr)
        print("Установите зависимости: pip install -r requirements.txt", file=sys.stderr)
        return 1
    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
