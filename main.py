import argparse
import sys
import json
import os
import re
import io
import tarfile
from urllib.parse import urlparse
from urllib.request import urlopen, Request
from typing import Dict, Any, List, Optional, Tuple

# Допустимые значения
REPO_MODE_CHOICES = ("clone", "local", "skip")
ASCII_MODE_CHOICES = ("off", "flat", "tree")

# проверка
PKG_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)

def is_valid_url(s: str) -> bool:
    try:
        p = urlparse(s)
        return p.scheme in {"http", "https", "ssh", "git", "git+ssh"} and bool(p.netloc)
    except Exception:
        return False

# ---------------------------
# ЭТАП 2: Работа с APKINDEX
# ---------------------------

def _download_apkindex_tar(repo_url: str) -> bytes:
    """
    Скачивает APKINDEX.tar.gz из корня репозитория Alpine.
    Примеры repo_url:
      - https://dl-cdn.alpinelinux.org/alpine/latest-stable/main/x86_64
      - https://dl-cdn.alpinelinux.org/alpine/v3.20/main/x86_64
    """
    if not repo_url.endswith("/"):
        repo_url += "/"
    index_url = repo_url + "APKINDEX.tar.gz"

    req = Request(index_url, headers={"User-Agent": "depcli/2"})
    with urlopen(req, timeout=30) as r:
        return r.read()

def _read_apkindex_from_tar_bytes(data: bytes) -> str:
    """
    Извлекает текстовый файл APKINDEX из tar.gz-байтов.
    """
    bio = io.BytesIO(data)
    with tarfile.open(mode="r:gz", fileobj=bio) as tf:
        member = tf.getmember("APKINDEX")
        f = tf.extractfile(member)
        if f is None:
            raise RuntimeError("В архиве отсутствует файл 'APKINDEX'.")
        content = f.read()
    # APKINDEX — это UTF-8 текст
    return content.decode("utf-8", errors="replace")

def _read_apkindex_from_local(path: str) -> str:
    """
    Поддержка локального пути:
      - path на APKINDEX.tar.gz
      - path на APKINDEX
      - path на директорию, внутри которой есть APKINDEX.tar.gz или APKINDEX
    """
    if os.path.isdir(path):
        tgz = os.path.join(path, "APKINDEX.tar.gz")
        plain = os.path.join(path, "APKINDEX")
        if os.path.exists(tgz):
            with open(tgz, "rb") as fh:
                return _read_apkindex_from_tar_bytes(fh.read())
        if os.path.exists(plain):
            with open(plain, "rb") as fh:
                return fh.read().decode("utf-8", errors="replace")
        raise FileNotFoundError("В каталоге не найден APKINDEX.tar.gz или APKINDEX.")
    else:
        # конкретный файл
        if path.endswith(".tar.gz"):
            with open(path, "rb") as fh:
                return _read_apkindex_from_tar_bytes(fh.read())
        else:
            with open(path, "rb") as fh:
                return fh.read().decode("utf-8", errors="replace")

def _parse_apkindex(text: str) -> Dict[str, Dict[str, Any]]:
    """
    Парсер простого формата APKINDEX.
    Записи разделены пустой строкой.
    В каждой записи ключи вида 'P:' (имя пакета), 'V:' (версия), 'D:' (зависимости) и т.д.
    Возвращает словарь: name -> {"version": str, "depends": [str, ...], ...}
    """
    packages: Dict[str, Dict[str, Any]] = {}
    entry: Dict[str, Any] = {}
    for line in text.splitlines():
        if not line.strip():
            # завершение записи
            if "P" in entry:
                name = entry.get("P", "")
                packages[name] = {
                    "version": entry.get("V", ""),
                    "depends": entry.get("D", []),
                }
            entry = {}
            continue

        if ":" not in line:
            # некорректная строка — пропустим
            continue

        key, val = line.split(":", 1)
        key = key.strip()
        val = val.strip()

        if key == "P":
            entry["P"] = val
        elif key == "V":
            entry["V"] = val
        elif key == "D":
            # Зависимости разделены пробелом.
            # Могут содержать версии (>, <, =, ~) и виртуальные именования (например, so:libcrypto3)
            deps = [t for t in val.split() if t]
            entry["D"] = deps
        else:
            # остальные поля нам не критичны на этом этапе
            pass

    # последний блок, если файл не заканчивается пустой строкой
    if entry.get("P"):
        name = entry.get("P", "")
        packages[name] = {
            "version": entry.get("V", ""),
            "depends": entry.get("D", []),
        }

    return packages

def _load_index(ns: argparse.Namespace) -> Dict[str, Dict[str, Any]]:
    """
    Загружает и парсит APKINDEX по URL или локальному пути.
    По ТЗ — используем URL, но локалка поддержана для удобства.
    """
    if ns.repo_url:
        data = _download_apkindex_tar(ns.repo_url)
        txt = _read_apkindex_from_tar_bytes(data)
        return _parse_apkindex(txt)
    if ns.repo_path:
        txt = _read_apkindex_from_local(ns.repo_path)
        return _parse_apkindex(txt)
    raise RuntimeError("Не указан источник APKINDEX (repo_url или repo_path).")

def get_direct_dependencies(ns: argparse.Namespace) -> Tuple[List[str], str]:
    """
    Возвращает (список_зависимостей, версия_пакета).
    """
    index = _load_index(ns)
    pkg = ns.package
    rec = index.get(pkg)
    if not rec:
        raise KeyError(f"Пакет '{pkg}' не найден в APKINDEX указанного репозитория.")
    deps = rec.get("depends", []) or []
    # фильтр при необходимости
    if ns.filter_substr:
        deps = [d for d in deps if ns.filter_substr in d]
    return deps, rec.get("version", "")

# ---------------------------
# Твой исходник (Этап 1) + правки вызова
# ---------------------------

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

    # 5) Подстрока фильтра пакетов
    if ns.filter_substr is not None and len(ns.filter_substr) == 0:
        errors.append("Параметр --filter-substr не должен быть пустой строкой. Опустите его вовсе или задайте значение.")

    # 6) Для Этапа 2 по ТЗ нужен именно URL когда repo_mode != skip
    if ns.repo_mode != "skip" and not ns.repo_url:
        # не критично, но подсказка ТЗ
        pass

    return errors

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="depcli",
        description="CLI для конфигурации и получения зависимостей из Alpine APKINDEX (Этап 2).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # Имя пакета
    p.add_argument(
        "-p", "--package",
        help="Имя анализируемого пакета (например, busybox)",
        required=True,
    )

    # URL ИЛИ путь
    p.add_argument(
        "--repo-url",
        help="URL репозитория Alpine (например, https://dl-cdn.alpinelinux.org/alpine/v3.20/main/x86_64).",
        required=False,
    )
    p.add_argument(
        "--repo-path",
        help="Путь к APKINDEX или APKINDEX.tar.gz (опционально, для локальной отладки). Указывать либо это, либо --repo-url.",
        type=str,
        default='',
    )

    # Режим работы
    p.add_argument(
        "--repo-mode",
        choices=REPO_MODE_CHOICES,
        default="skip",
        help=(
            "Режим работы с тестовым репозиторием: "
            "'clone' — зарезервировано; "
            "'local' — использовать локальный путь; "
            "'skip' — пропустить (только вывод конфигурации)."
        ),
    )

    p.add_argument("--max_deep", type=int, default=3)

    # Режим вывода (для будущей визуализации; на этом этапе достаточно 'flat' или 'off')
    p.add_argument(
        "--ascii-mode",
        choices=ASCII_MODE_CHOICES,
        default="off",
        help="Режим вывода зависимостей: 'off' — только конфиг; 'flat' — список прямых зависимостей; 'tree' — зарезервировано."
    )

    # Подстрока фильтра
    p.add_argument(
        "--filter-substr",
        help="Подстрока для фильтрации зависимостей (например, 'musl' или 'so:')."
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

def print_direct_dependencies(ns: argparse.Namespace) -> int:
    """
    Выполняет требование Этапа 2: выводит на экран ВСЕ прямые зависимости заданного пакета.
    """
    try:
        deps, ver = get_direct_dependencies(ns)
    except Exception as ex:
        eprint(f"Ошибка получения зависимостей: {ex}")
        return 3

    print(f"# Пакет: {ns.package} (версия из индекса: {ver})")
    if not deps:
        print("# Прямые зависимости: отсутствуют")
        return 0

    print("# Прямые зависимости:")
    # По ТЗ — просто вывести, без менеджеров и сторонних либ
    for d in deps:
        print(d)
    return 0

def main(argv: Optional[List[str]] = None) -> int:
    ns = parse_args(argv)
    errors = validate_args(ns)
    if errors:
        eprint("Обнаружены ошибки параметров:")
        for msg in errors:
            eprint(" -", msg)
        return 2  # стандартный код выхода для ошибок CLI-аргументов

    # Всегда печатаем конфиг (Этап 1)
    print_config(ns)

    # Этап 2: если пользователь указал источник индекса (repo_mode != skip), выводим прямые зависимости
    if ns.repo_mode != "skip":
        # По ТЗ именно URL, но если указан локальный путь — тоже поддержим.
        return print_direct_dependencies(ns)

    # Если skip — завершаем на конфигурации
    return 0

if __name__ == "__main__":
    sys.exit(main())
