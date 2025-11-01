

import argparse
import sys
import json
import os
import re
from urllib.parse import urlparse
from typing import Dict, Any, List, Optional

# Допустимые значения
REPO_MODE_CHOICES = ("clone", "local", "skip")
ASCII_MODE_CHOICES = ("off", "flat", "tree")

#  проверка 
PKG_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)

def is_valid_url(s: str) -> bool:
    try:
        p = urlparse(s)
        return p.scheme in {"http", "https", "ssh", "git", "git+ssh"} and bool(p.netloc)
    except Exception:
        return False

def validate_args(ns: argparse.Namespace) -> List[str]:
    """
    Возвращает список сообщений об ошибках.
    """
    errors: List[str] = []

    # 1) Имя пакета
    if not ns.package:
        errors.append("Параметр --package обязателен и не может быть пустым.")
    elif not PKG_NAME_RE.match(ns.package):
        errors.append("Недопустимое имя пакета в --package: разрешены только A-Z, a-z, 0-9, '.', '_' и '-'.")

    # 2) URL или путь
    # Условие: при режиме repo_mode != 'skip' должен быть указан либо --repo-url, либо --repo-path (но не оба).
    if ns.repo_mode != "skip":
        if ns.repo_url and ns.repo_path:
            errors.append("Укажите что-то одно: либо --repo-url, либо --repo-path (сейчас заданы оба).")
        elif not ns.repo_url and not ns.repo_path:
            errors.append("Для --repo-mode != 'skip' требуется указать --repo-url или --repo-path.")
        elif ns.repo_url:
            if not is_valid_url(ns.repo_url):
                errors.append("Некорректный URL в --repo-url (ожидается http(s)/ssh/git/git+ssh).")
        elif ns.repo_path:
            if not os.path.exists(ns.repo_path):
                errors.append(f"Путь в --repo-path не найден: {ns.repo_path}")

    # 3) Режим работы с тестовым репозиторием
    if ns.repo_mode not in REPO_MODE_CHOICES:
        errors.append(f"Недопустимое значение --repo-mode: {ns.repo_mode!r}. Допустимо: {', '.join(REPO_MODE_CHOICES)}")

    # 4) Режим ASCII-дерева зависимостей
    if ns.ascii_mode not in ASCII_MODE_CHOICES:
        errors.append(f"Недопустимое значение --ascii-mode: {ns.ascii_mode!r}. Допустимо: {', '.join(ASCII_MODE_CHOICES)}")

    # 5) Подстрока фильтра пакетов (можно пустую не разрешать — но в ТЗ это «настраиваемый параметр»)
    # Здесь разрешим пустую, но если указана явно пустая строка "", предупредим.
    if ns.filter_substr is not None and len(ns.filter_substr) == 0:
        errors.append("Параметр --filter-substr не должен быть пустой строкой. Опустите его вовсе или задайте значение.")

    return errors

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="depcli",
        description="Минимальный CLI для конфигурации анализа зависимостей (Этап 1).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # Имя пакета 
    p.add_argument(
        "-p", "--package",
        help="Имя анализируемого пакета (например, requests)",
        required=True,
    )

    #  URL ИЛИ путь
    p.add_argument(
        "--repo-url",
        help="URL тестового репозитория (http(s)/ssh/git/git+ssh). Указывать либо это, либо --repo-path.",
        required=False,
    )
    p.add_argument(
        "--repo-path",
        help="Путь к файлу или директории тестового репозитория. Указывать либо это, либо --repo-url.",
        type = str,
        default = '',
    )

    # Режим работы 
    p.add_argument(
        "--repo-mode",
        choices=REPO_MODE_CHOICES,
        default="skip",
        help=(
            "Режим работы с тестовым репозиторием: "
            "'clone' — клонировать по URL; "
            "'local' — использовать локальный путь; "
            "'skip' — пропустить работу с тестовым репозиторием."
        ),
    )
    p.add_argument(
        "--max_deep",
        type = int, 
        default = 3,

    )

    # Режим вывода ASCII-дерева
    p.add_argument(
        "--ascii-mode",
        choices=ASCII_MODE_CHOICES,
        default="off",
        help="Режим вывода зависимостей: 'off' — не выводить дерево; 'flat' — плоский список; 'tree' — ASCII-дерево."
    )

    # Подстрока фильтра
    p.add_argument(
        "--filter-substr",
        help="Подстрока для фильтрации пакетов (например, 'pytest')."
    )

    # Флаг
    p.add_argument(
        "--print-json",
        action="store_true",
        help="Печатать параметры в JSON вместо key=value."
    )

    return p.parse_args(argv)

def print_config(ns: argparse.Namespace) -> None:
    """
    Печатает ВСЕ настраиваемые параметры в формате ключ=значение (или JSON).
    """
    cfg: Dict[str, Any] = {
        "package": ns.package,
        "repo_url": ns.repo_url or "",
        "repo_path": ns.repo_path or "",
        "repo_mode": ns.repo_mode,
        "ascii_mode": ns.ascii_mode,
        "filter_substr": ns.filter_substr if ns.filter_substr is not None else "",
    }

    if ns.print_json:
        print(json.dumps(cfg, ensure_ascii=False, indent=2))
    else:
        for k, v in cfg.items():
            print(f"{k}={v}")

def main(argv: Optional[List[str]] = None) -> int:
    ns = parse_args(argv)
    errors = validate_args(ns)
    if errors:
        eprint("Обнаружены ошибки параметров:")
        for msg in errors:
            eprint(" -", msg)
        return 2  # стандартный код выхода для ошибок CLI-аргументов
    # Этап 1: просто печатаем конфигурацию
    print_config(ns)
    return 0

if __name__ == "__main__":
    sys.exit(main())
