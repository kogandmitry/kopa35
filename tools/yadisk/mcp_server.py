"""MCP-сервер Яндекс.Диска для Claude Code (stdio, без внешних зависимостей).

Подключение:
    claude mcp add yadisk --env YANDEX_DISK_TOKEN=<токен> \
        -- python3 /путь/к/kopa35/tools/yadisk/mcp_server.py

Протокол: JSON-RPC 2.0, по одному сообщению в строке на stdin/stdout.
Диагностика пишется в stderr, чтобы не мешать протоколу.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from typing import Any, Callable

if __package__ in (None, ""):  # запуск файлом, а не модулем
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from tools.yadisk.api import MEDIA_TYPES, YaDiskError, YandexDisk, human_size
    from tools.yadisk.auth import TokenNotFound, resolve_token
else:
    from .api import MEDIA_TYPES, YaDiskError, YandexDisk, human_size
    from .auth import TokenNotFound, resolve_token

SERVER_NAME = "yadisk"
SERVER_VERSION = "1.0.0"
DEFAULT_PROTOCOL = "2025-06-18"
SUPPORTED_PROTOCOLS = {"2025-06-18", "2025-03-26", "2024-11-05"}
DOWNLOAD_DIR_ENV = "YANDEX_DISK_DOWNLOAD_DIR"

_STRING = {"type": "string"}
_INT = {"type": "integer"}
_BOOL = {"type": "boolean"}
_PATH = {"type": "string", "description": "Путь на Диске, например /Фотоархив/2024"}
_MEDIA = {"type": "string", "enum": list(MEDIA_TYPES), "description": "Фильтр по типу файлов"}


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


class Session:
    """Ленивое подключение: без токена сервер всё равно стартует и объясняет, чего не хватает."""

    def __init__(self, factory: Callable[[], YandexDisk] | None = None) -> None:
        self._factory = factory or (lambda: YandexDisk(resolve_token()))
        self._client: YandexDisk | None = None

    @property
    def client(self) -> YandexDisk:
        if self._client is None:
            self._client = self._factory()
        return self._client


def _brief(item: dict[str, Any]) -> dict[str, Any]:
    """Оставляет только поля, полезные модели, — ответы API слишком многословны."""
    kept = {
        key: item[key]
        for key in ("name", "path", "type", "size", "modified", "media_type", "mime_type", "public_url")
        if key in item
    }
    if isinstance(kept.get("size"), int):
        kept["size_human"] = human_size(kept["size"])
    return kept


def _download_dir() -> str:
    return os.environ.get(DOWNLOAD_DIR_ENV) or os.path.join(os.getcwd(), "yadisk-downloads")


# ----------------------------------------------------------------------
# реализация инструментов
# ----------------------------------------------------------------------
def _t_disk_info(disk: YandexDisk, _: dict[str, Any]) -> Any:
    info = disk.disk_info()
    return {
        "total": human_size(info.get("total_space")),
        "used": human_size(info.get("used_space")),
        "trash": human_size(info.get("trash_size")),
        "total_bytes": info.get("total_space"),
        "used_bytes": info.get("used_space"),
    }


def _t_list(disk: YandexDisk, args: dict[str, Any]) -> Any:
    path = args.get("path", "/")
    limit = int(args.get("limit", 100))
    if args.get("recursive"):
        items = disk.walk(path, max_depth=args.get("max_depth"))
    else:
        items = disk.listdir(path, sort=args.get("sort", "name"))
    collected = []
    for item in items:
        collected.append(_brief(item))
        if len(collected) >= limit:
            break
    return {"path": path, "count": len(collected), "items": collected}


def _t_stat(disk: YandexDisk, args: dict[str, Any]) -> Any:
    meta = disk.meta(args["path"], limit=1)
    brief = _brief(meta)
    for key in ("md5", "sha256", "created", "resource_id"):
        if key in meta:
            brief[key] = meta[key]
    return brief


def _t_find(disk: YandexDisk, args: dict[str, Any]) -> Any:
    items = disk.find(
        args.get("query", ""),
        media_type=args.get("media_type"),
        limit=int(args.get("limit", 50)),
        scan_limit=int(args.get("scan_limit", 5000)),
    )
    return {"count": len(items), "items": [_brief(item) for item in items]}


def _t_recent(disk: YandexDisk, args: dict[str, Any]) -> Any:
    items = disk.last_uploaded(limit=int(args.get("limit", 20)), media_type=args.get("media_type"))
    return {"count": len(items), "items": [_brief(item) for item in items]}


def _t_download(disk: YandexDisk, args: dict[str, Any]) -> Any:
    remote = args["path"]
    destination = args.get("dest") or os.path.join(_download_dir(), os.path.basename(remote.rstrip("/")))
    if args.get("recursive"):
        saved = disk.download_tree(remote, destination)
        return {"downloaded": len(saved), "dir": destination, "files": saved[:50]}
    local = disk.download(remote, destination)
    return {"file": local, "size": human_size(os.path.getsize(local))}


def _t_read_text(disk: YandexDisk, args: dict[str, Any]) -> Any:
    max_bytes = int(args.get("max_bytes", 200_000))
    with tempfile.TemporaryDirectory() as tmp:
        local = disk.download(args["path"], os.path.join(tmp, "content"))
        with open(local, "rb") as handle:
            raw = handle.read(max_bytes + 1)
    truncated = len(raw) > max_bytes
    return {
        "path": args["path"],
        "truncated": truncated,
        "text": raw[:max_bytes].decode("utf-8", "replace"),
    }


def _t_upload(disk: YandexDisk, args: dict[str, Any]) -> Any:
    result = disk.upload(args["local_path"], args["path"], overwrite=bool(args.get("overwrite")))
    return {"path": result["path"], "size": human_size(result["size"])}


def _t_mkdir(disk: YandexDisk, args: dict[str, Any]) -> Any:
    disk.mkdir(args["path"], parents=bool(args.get("parents")))
    return {"created": args["path"]}


def _t_move(disk: YandexDisk, args: dict[str, Any]) -> Any:
    disk.move(args["src"], args["dst"], overwrite=bool(args.get("overwrite")))
    return {"moved": args["src"], "to": args["dst"]}


def _t_copy(disk: YandexDisk, args: dict[str, Any]) -> Any:
    disk.copy(args["src"], args["dst"], overwrite=bool(args.get("overwrite")))
    return {"copied": args["src"], "to": args["dst"]}


def _t_delete(disk: YandexDisk, args: dict[str, Any]) -> Any:
    permanently = bool(args.get("permanently"))
    disk.remove(args["path"], permanently=permanently)
    return {"deleted": args["path"], "permanently": permanently}


def _t_publish(disk: YandexDisk, args: dict[str, Any]) -> Any:
    return {"path": args["path"], "public_url": disk.publish(args["path"])}


def _t_unpublish(disk: YandexDisk, args: dict[str, Any]) -> Any:
    disk.unpublish(args["path"])
    return {"unpublished": args["path"]}


TOOLS: list[dict[str, Any]] = [
    {
        "name": "yadisk_disk_info",
        "description": "Сколько места занято на Яндекс.Диске и сколько свободно.",
        "inputSchema": _schema({}),
        "handler": _t_disk_info,
    },
    {
        "name": "yadisk_list",
        "description": "Содержимое папки на Яндекс.Диске. С recursive=true обходит вложенные папки.",
        "inputSchema": _schema({
            "path": _PATH,
            "recursive": _BOOL,
            "max_depth": {**_INT, "description": "Глубина обхода при recursive"},
            "limit": {**_INT, "description": "Максимум записей в ответе (по умолчанию 100)"},
            "sort": {**_STRING, "description": "name, size, modified, created; минус — по убыванию"},
        }),
        "handler": _t_list,
    },
    {
        "name": "yadisk_stat",
        "description": "Сведения о файле или папке: размер, дата, md5, публичная ссылка.",
        "inputSchema": _schema({"path": _PATH}, ["path"]),
        "handler": _t_stat,
    },
    {
        "name": "yadisk_find",
        "description": "Поиск файлов по подстроке в имени или пути, с фильтром по типу (image, video, document…).",
        "inputSchema": _schema({
            "query": {**_STRING, "description": "Подстрока имени; пустая — вернуть всё подряд"},
            "media_type": _MEDIA,
            "limit": _INT,
            "scan_limit": {**_INT, "description": "Сколько файлов просмотреть (по умолчанию 5000)"},
        }),
        "handler": _t_find,
    },
    {
        "name": "yadisk_recent",
        "description": "Последние загруженные на Диск файлы.",
        "inputSchema": _schema({"limit": _INT, "media_type": _MEDIA}),
        "handler": _t_recent,
    },
    {
        "name": "yadisk_download",
        "description": "Скачать файл или папку с Диска на локальный компьютер для дальнейшей работы.",
        "inputSchema": _schema({
            "path": _PATH,
            "dest": {**_STRING, "description": "Локальный путь; по умолчанию ./yadisk-downloads/"},
            "recursive": {**_BOOL, "description": "Скачать папку целиком"},
        }, ["path"]),
        "handler": _t_download,
    },
    {
        "name": "yadisk_read_text",
        "description": "Прочитать текстовый файл с Диска прямо в ответ, ничего не сохраняя.",
        "inputSchema": _schema({
            "path": _PATH,
            "max_bytes": {**_INT, "description": "Ограничение объёма (по умолчанию 200000)"},
        }, ["path"]),
        "handler": _t_read_text,
    },
    {
        "name": "yadisk_upload",
        "description": "Загрузить локальный файл на Яндекс.Диск.",
        "inputSchema": _schema({
            "local_path": {**_STRING, "description": "Путь к файлу на компьютере"},
            "path": _PATH,
            "overwrite": _BOOL,
        }, ["local_path", "path"]),
        "handler": _t_upload,
    },
    {
        "name": "yadisk_mkdir",
        "description": "Создать папку на Диске (parents=true — вместе с промежуточными).",
        "inputSchema": _schema({"path": _PATH, "parents": _BOOL}, ["path"]),
        "handler": _t_mkdir,
    },
    {
        "name": "yadisk_move",
        "description": "Переместить или переименовать файл либо папку.",
        "inputSchema": _schema({"src": _PATH, "dst": _PATH, "overwrite": _BOOL}, ["src", "dst"]),
        "handler": _t_move,
    },
    {
        "name": "yadisk_copy",
        "description": "Скопировать файл или папку внутри Диска.",
        "inputSchema": _schema({"src": _PATH, "dst": _PATH, "overwrite": _BOOL}, ["src", "dst"]),
        "handler": _t_copy,
    },
    {
        "name": "yadisk_delete",
        "description": "Удалить файл или папку. По умолчанию — в корзину, permanently=true удаляет насовсем.",
        "inputSchema": _schema({"path": _PATH, "permanently": _BOOL}, ["path"]),
        "handler": _t_delete,
    },
    {
        "name": "yadisk_publish",
        "description": "Опубликовать файл или папку и получить публичную ссылку.",
        "inputSchema": _schema({"path": _PATH}, ["path"]),
        "handler": _t_publish,
    },
    {
        "name": "yadisk_unpublish",
        "description": "Снять публичную ссылку с файла или папки.",
        "inputSchema": _schema({"path": _PATH}, ["path"]),
        "handler": _t_unpublish,
    },
]

TOOL_INDEX = {tool["name"]: tool for tool in TOOLS}
PUBLIC_TOOLS = [{k: v for k, v in tool.items() if k != "handler"} for tool in TOOLS]


# ----------------------------------------------------------------------
# JSON-RPC
# ----------------------------------------------------------------------
def _result(request_id: Any, payload: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _text_result(request_id: Any, payload: Any, is_error: bool = False) -> dict[str, Any]:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=2)
    return _result(request_id, {"content": [{"type": "text", "text": text}], "isError": is_error})


def call_tool(session: Session, name: str, arguments: dict[str, Any]) -> tuple[Any, bool]:
    """Возвращает (результат, признак ошибки). Ошибки инструмента — не ошибки протокола."""
    tool = TOOL_INDEX.get(name)
    if tool is None:
        return f"Неизвестный инструмент: {name}", True
    try:
        return tool["handler"](session.client, arguments or {}), False
    except TokenNotFound as exc:
        return str(exc), True
    except YaDiskError as exc:
        return f"Яндекс.Диск ответил ошибкой: {exc}", True
    except KeyError as exc:
        return f"Не хватает обязательного параметра: {exc}", True
    except (OSError, ValueError) as exc:
        return f"{type(exc).__name__}: {exc}", True


def handle(message: dict[str, Any], session: Session) -> dict[str, Any] | None:
    """Обрабатывает одно сообщение JSON-RPC. ``None`` — отвечать не нужно."""
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if request_id is None:  # уведомление
        return None

    if method == "initialize":
        requested = params.get("protocolVersion")
        return _result(request_id, {
            "protocolVersion": requested if requested in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, {"tools": PUBLIC_TOOLS})
    if method == "tools/call":
        payload, failed = call_tool(session, params.get("name", ""), params.get("arguments") or {})
        return _text_result(request_id, payload, failed)
    return _error(request_id, -32601, f"Метод не поддерживается: {method}")


def serve(stdin: Any = None, stdout: Any = None, session: Session | None = None) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    session = session or Session()
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            json.dump(_error(None, -32700, "Не разобран JSON"), stdout)
            stdout.write("\n")
            stdout.flush()
            continue
        response = handle(message, session)
        if response is not None:
            json.dump(response, stdout, ensure_ascii=False)
            stdout.write("\n")
            stdout.flush()


if __name__ == "__main__":
    try:
        serve()
    except KeyboardInterrupt:
        pass
