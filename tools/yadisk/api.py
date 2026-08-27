"""Клиент Yandex Disk REST API на одной стандартной библиотеке Python.

Справочник API: https://yandex.ru/dev/disk/api/reference/
Авторизация — заголовок ``Authorization: OAuth <token>``.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator

API_ROOT = "https://cloud-api.yandex.net/v1/disk"
USER_AGENT = "kopa35-yadisk/1.0"
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_ATTEMPTS = 4

#: Значения параметра ``media_type`` в API.
MEDIA_TYPES = (
    "audio", "backup", "book", "compressed", "data", "development",
    "diskimage", "document", "encoded", "executable", "flash", "font",
    "image", "settings", "spreadsheet", "text", "unknown", "video", "web",
)


class YaDiskError(RuntimeError):
    """Ошибка, о которой сообщил API Диска."""

    def __init__(self, status: int, payload: dict[str, Any] | None, url: str) -> None:
        self.status = status
        self.payload = payload or {}
        self.url = url
        description = self.payload.get("description") or self.payload.get("message")
        super().__init__(
            "Yandex Disk API %s для %s%s"
            % (status, url, f": {description}" if description else "")
        )


def normalize_path(path: str | None) -> str:
    """Приводит путь к виду ``disk:/...``. Пустой путь — корень Диска.

    Схемы ``disk:``, ``app:``, ``trash:``, ``photounlim:`` остаются как есть.
    """
    if not path or path in {"/", ".", "disk:", "disk:/"}:
        return "disk:/"
    head = path.split("/", 1)[0]
    if head.endswith(":"):
        return path
    return "disk:/" + path.lstrip("/")


def human_size(size: int | None) -> str:
    if size is None:
        return "-"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}TB"


def _read_error_payload(exc: urllib.error.HTTPError) -> dict[str, Any] | None:
    try:
        raw = exc.read()
    except Exception:
        return None
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {"message": raw[:400].decode("utf-8", "replace")}


class YandexDisk:
    """Тонкая обёртка над REST API Яндекс.Диска."""

    def __init__(
        self,
        token: str,
        *,
        api_root: str = API_ROOT,
        timeout: float = 60.0,
        opener: Any = None,
        sleep: Any = time.sleep,
    ) -> None:
        if not token:
            raise ValueError("нужен OAuth-токен Яндекс.Диска")
        self.token = token
        self.api_root = api_root.rstrip("/")
        self.timeout = timeout
        self._opener = opener or urllib.request.build_opener()
        self._sleep = sleep

    # ------------------------------------------------------------------
    # транспорт
    # ------------------------------------------------------------------
    def open(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        data: Any = None,
        headers: dict[str, str] | None = None,
        auth: bool = True,
    ) -> Any:
        """Выполняет запрос и возвращает открытый HTTP-ответ."""
        if params:
            query = {k: v for k, v in params.items() if v is not None}
            if query:
                url += ("&" if "?" in url else "?") + urllib.parse.urlencode(query)

        hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if auth:
            hdrs["Authorization"] = f"OAuth {self.token}"
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        if headers:
            hdrs.update(headers)

        request = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        # Поток (файл на выгрузку) нельзя перечитать, поэтому такие запросы не повторяем.
        retriable = data is None or isinstance(data, (bytes, bytearray))

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return self._opener.open(request, timeout=self.timeout)
            except urllib.error.HTTPError as exc:
                payload = _read_error_payload(exc)
                if exc.code in RETRY_STATUSES and retriable and attempt < MAX_ATTEMPTS:
                    self._sleep(self._backoff(exc, attempt))
                    continue
                raise YaDiskError(exc.code, payload, url) from exc
            except urllib.error.URLError as exc:
                if retriable and attempt < MAX_ATTEMPTS:
                    self._sleep(2 ** (attempt - 1))
                    continue
                raise YaDiskError(0, {"message": str(exc.reason)}, url) from exc
        raise AssertionError("недостижимо")

    @staticmethod
    def _backoff(exc: urllib.error.HTTPError, attempt: int) -> float:
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
        if retry_after:
            try:
                return min(float(retry_after), 30.0)
            except ValueError:
                pass
        return float(2 ** (attempt - 1))

    def call(self, method: str, endpoint: str, **kwargs: Any) -> dict[str, Any]:
        """Запрос к API, возвращающий разобранный JSON (или ``{}``)."""
        with self.open(method, self.api_root + endpoint, **kwargs) as response:
            raw = response.read()
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    # ------------------------------------------------------------------
    # чтение
    # ------------------------------------------------------------------
    def disk_info(self) -> dict[str, Any]:
        return self.call("GET", "/")

    def meta(
        self,
        path: str,
        *,
        limit: int = 200,
        offset: int = 0,
        sort: str | None = None,
        fields: str | None = None,
    ) -> dict[str, Any]:
        return self.call(
            "GET",
            "/resources",
            params={
                "path": normalize_path(path),
                "limit": limit,
                "offset": offset,
                "sort": sort,
                "fields": fields,
            },
        )

    def listdir(self, path: str, *, sort: str = "name", page: int = 200) -> Iterator[dict[str, Any]]:
        """Постранично отдаёт содержимое папки."""
        offset = 0
        while True:
            embedded = self.meta(path, limit=page, offset=offset, sort=sort).get("_embedded")
            if embedded is None:
                raise YaDiskError(400, {"description": f"{path} — не папка"}, self.api_root)
            items = embedded.get("items", [])
            for item in items:
                yield item
            offset += len(items)
            total = embedded.get("total")
            if not items or (total is not None and offset >= total):
                return

    def files(
        self,
        *,
        media_type: str | None = None,
        sort: str | None = None,
        limit: int | None = None,
        page: int = 200,
    ) -> Iterator[dict[str, Any]]:
        """Плоский список всех файлов Диска (в API нет поиска по имени)."""
        offset = 0
        produced = 0
        while True:
            chunk = min(page, limit - produced) if limit is not None else page
            if chunk <= 0:
                return
            payload = self.call(
                "GET",
                "/resources/files",
                params={
                    "limit": chunk,
                    "offset": offset,
                    "media_type": media_type,
                    "sort": sort,
                },
            )
            items = payload.get("items", [])
            for item in items:
                yield item
                produced += 1
            offset += len(items)
            if len(items) < chunk:
                return

    def find(
        self,
        needle: str = "",
        *,
        media_type: str | None = None,
        limit: int = 50,
        scan_limit: int = 5000,
    ) -> list[dict[str, Any]]:
        """Ищет по подстроке в имени/пути, фильтруя плоский список на клиенте."""
        needle = needle.lower()
        found: list[dict[str, Any]] = []
        for item in self.files(media_type=media_type, limit=scan_limit):
            haystack = f"{item.get('name', '')} {item.get('path', '')}".lower()
            if needle in haystack:
                found.append(item)
                if len(found) >= limit:
                    break
        return found

    def last_uploaded(self, *, limit: int = 20, media_type: str | None = None) -> list[dict[str, Any]]:
        payload = self.call(
            "GET",
            "/resources/last-uploaded",
            params={"limit": limit, "media_type": media_type},
        )
        return payload.get("items", [])

    def walk(self, path: str, *, max_depth: int | None = None) -> Iterator[dict[str, Any]]:
        """Рекурсивно обходит дерево, отдавая и папки, и файлы."""
        queue = [(normalize_path(path), 0)]
        while queue:
            current, depth = queue.pop(0)
            for item in self.listdir(current):
                yield item
                if item.get("type") == "dir" and (max_depth is None or depth + 1 < max_depth):
                    queue.append((item["path"], depth + 1))

    # ------------------------------------------------------------------
    # запись
    # ------------------------------------------------------------------
    def mkdir(self, path: str, *, parents: bool = False) -> dict[str, Any]:
        target = normalize_path(path)
        if parents:
            scheme, _, rest = target.partition(":/")
            made: list[str] = []
            parts = [part for part in rest.split("/") if part]
            for part in parts[:-1]:
                made.append(part)
                try:
                    self.call("PUT", "/resources", params={"path": f"{scheme}:/" + "/".join(made)})
                except YaDiskError as exc:
                    if exc.status != 409:
                        raise
        return self.call("PUT", "/resources", params={"path": target})

    def upload(self, local_path: str, remote_path: str, *, overwrite: bool = False) -> dict[str, Any]:
        target = normalize_path(remote_path)
        link = self.call(
            "GET",
            "/resources/upload",
            params={"path": target, "overwrite": str(bool(overwrite)).lower()},
        )
        size = os.path.getsize(local_path)
        with open(local_path, "rb") as handle:
            with self.open(
                link.get("method", "PUT"),
                link["href"],
                data=handle,
                auth=False,
                headers={"Content-Length": str(size), "Content-Type": "application/octet-stream"},
            ):
                pass
        return {"path": target, "size": size}

    def download(self, remote_path: str, local_path: str) -> str:
        link = self.call(
            "GET", "/resources/download", params={"path": normalize_path(remote_path)}
        )
        parent = os.path.dirname(os.path.abspath(local_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with self.open("GET", link["href"], auth=False) as response:
            with open(local_path, "wb") as handle:
                shutil.copyfileobj(response, handle)
        return local_path

    def download_tree(self, remote_path: str, local_dir: str) -> list[str]:
        """Скачивает папку целиком, сохраняя структуру."""
        root = normalize_path(remote_path)
        prefix = root.rstrip("/") + "/"
        saved: list[str] = []
        for item in self.walk(root):
            if item.get("type") != "file":
                continue
            relative = item["path"][len(prefix):] if item["path"].startswith(prefix) else item["name"]
            destination = os.path.join(local_dir, *relative.split("/"))
            self.download(item["path"], destination)
            saved.append(destination)
        return saved

    def remove(self, path: str, *, permanently: bool = False) -> dict[str, Any]:
        return self.call(
            "DELETE",
            "/resources",
            params={
                "path": normalize_path(path),
                "permanently": str(bool(permanently)).lower(),
            },
        )

    def move(self, src: str, dst: str, *, overwrite: bool = False) -> dict[str, Any]:
        return self.call(
            "POST",
            "/resources/move",
            params={
                "from": normalize_path(src),
                "path": normalize_path(dst),
                "overwrite": str(bool(overwrite)).lower(),
            },
        )

    def copy(self, src: str, dst: str, *, overwrite: bool = False) -> dict[str, Any]:
        return self.call(
            "POST",
            "/resources/copy",
            params={
                "from": normalize_path(src),
                "path": normalize_path(dst),
                "overwrite": str(bool(overwrite)).lower(),
            },
        )

    def publish(self, path: str) -> str:
        target = normalize_path(path)
        self.call("PUT", "/resources/publish", params={"path": target})
        return self.meta(target, limit=1, fields="public_url").get("public_url", "")

    def unpublish(self, path: str) -> dict[str, Any]:
        return self.call("PUT", "/resources/unpublish", params={"path": normalize_path(path)})

    # ------------------------------------------------------------------
    # публичные ссылки (чужие или свои, без доступа к самому диску)
    # ------------------------------------------------------------------
    def public_meta(self, public_key: str, *, path: str | None = None, limit: int = 200) -> dict[str, Any]:
        return self.call(
            "GET",
            "/public/resources",
            params={"public_key": public_key, "path": path, "limit": limit},
        )

    def download_public(self, public_key: str, local_path: str, *, path: str | None = None) -> str:
        link = self.call(
            "GET",
            "/public/resources/download",
            params={"public_key": public_key, "path": path},
        )
        parent = os.path.dirname(os.path.abspath(local_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with self.open("GET", link["href"], auth=False) as response:
            with open(local_path, "wb") as handle:
                shutil.copyfileobj(response, handle)
        return local_path
