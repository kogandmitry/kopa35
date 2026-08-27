"""Хранение и получение OAuth-токена Яндекс.Диска."""

from __future__ import annotations

import os
import stat
import urllib.parse
from pathlib import Path

TOKEN_ENV = "YANDEX_DISK_TOKEN"
TOKEN_FILE_ENV = "YANDEX_DISK_TOKEN_FILE"
CLIENT_ID_ENV = "YANDEX_DISK_CLIENT_ID"
DEFAULT_TOKEN_FILE = Path.home() / ".config" / "yadisk" / "token"

HOWTO = f"""Токен Яндекс.Диска не найден.

Как получить (один раз, ~3 минуты):
  1. Откройте https://oauth.yandex.ru/client/new/ и создайте приложение.
     Платформа — «Веб-сервисы», Redirect URI — https://oauth.yandex.ru/verification_codes
     Права (scope): cloud_api:disk.info, cloud_api:disk.read, cloud_api:disk.write
  2. Скопируйте ClientID приложения.
  3. Выполните: python3 -m tools.yadisk.cli auth --client-id <ClientID>
     и вставьте токен из адресной строки после подтверждения.

Либо задайте токен напрямую:
  export {TOKEN_ENV}=<токен>
Либо положите его в файл {DEFAULT_TOKEN_FILE} (или укажите путь в {TOKEN_FILE_ENV})."""


class TokenNotFound(RuntimeError):
    def __init__(self) -> None:
        super().__init__(HOWTO)


def token_file() -> Path:
    override = os.environ.get(TOKEN_FILE_ENV)
    return Path(override).expanduser() if override else DEFAULT_TOKEN_FILE


def resolve_token(explicit: str | None = None) -> str:
    """Ищет токен: аргумент → переменная окружения → файл."""
    if explicit:
        return explicit.strip()
    from_env = os.environ.get(TOKEN_ENV)
    if from_env and from_env.strip():
        return from_env.strip()
    path = token_file()
    if path.is_file():
        stored = path.read_text(encoding="utf-8").strip()
        if stored:
            return stored
    raise TokenNotFound()


def save_token(token: str, path: Path | None = None) -> Path:
    """Сохраняет токен в файл с правами 600."""
    target = path or token_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(token.strip() + "\n", encoding="utf-8")
    target.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return target


def authorize_url(client_id: str) -> str:
    """Ссылка для implicit-grant: токен вернётся во фрагменте адреса."""
    query = urllib.parse.urlencode({"response_type": "token", "client_id": client_id})
    return f"https://oauth.yandex.ru/authorize?{query}"
