"""Тесты клиента Яндекс.Диска и MCP-сервера. Сеть не используется — HTTP подменён."""

from __future__ import annotations

import email.message
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.yadisk.api import YaDiskError, YandexDisk, human_size, normalize_path
from tools.yadisk import mcp_server


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes = b"", status: int = 200) -> None:
        super().__init__(body)
        self.status = status
        self.headers = email.message.Message()


def json_response(payload: object) -> FakeResponse:
    return FakeResponse(json.dumps(payload).encode("utf-8"))


def http_error(status: int, payload: object = None, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = email.message.Message()
    if retry_after:
        headers["Retry-After"] = retry_after
    body = json.dumps(payload or {}).encode("utf-8")
    return urllib.error.HTTPError("https://example/", status, "err", headers, io.BytesIO(body))


class FakeOpener:
    """Отдаёт заранее заготовленные ответы и запоминает запросы."""

    def __init__(self, *responses: object) -> None:
        self.queue = list(responses)
        self.requests: list[urllib.request.Request] = []  # type: ignore[name-defined]

    def open(self, request, timeout=None):  # noqa: ANN001 - совместимость с urllib
        self.requests.append(request)
        if not self.queue:
            raise AssertionError(f"нет заготовленного ответа для {request.full_url}")
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def urls(self) -> list[str]:
        return [request.full_url for request in self.requests]


def client(*responses: object, sleeps: list[float] | None = None) -> YandexDisk:
    opener = FakeOpener(*responses)
    disk = YandexDisk("t0ken", opener=opener, sleep=(sleeps.append if sleeps is not None else (lambda _: None)))
    disk.opener = opener  # type: ignore[attr-defined]
    return disk


class TestPaths(unittest.TestCase):
    def test_normalize(self) -> None:
        self.assertEqual(normalize_path(None), "disk:/")
        self.assertEqual(normalize_path("/"), "disk:/")
        self.assertEqual(normalize_path("Фото"), "disk:/Фото")
        self.assertEqual(normalize_path("/Фото/2024"), "disk:/Фото/2024")
        self.assertEqual(normalize_path("disk:/Фото"), "disk:/Фото")
        self.assertEqual(normalize_path("app:/данные"), "app:/данные")
        self.assertEqual(normalize_path("trash:/x"), "trash:/x")

    def test_human_size(self) -> None:
        self.assertEqual(human_size(None), "-")
        self.assertEqual(human_size(512), "512B")
        self.assertEqual(human_size(2048), "2.0KB")


class TestRequests(unittest.TestCase):
    def test_disk_info_sends_oauth_header(self) -> None:
        disk = client(json_response({"total_space": 10, "used_space": 4}))
        self.assertEqual(disk.disk_info()["total_space"], 10)
        request = disk.opener.requests[0]
        self.assertEqual(request.get_header("Authorization"), "OAuth t0ken")
        self.assertTrue(request.full_url.endswith("/v1/disk/"))

    def test_listdir_paginates(self) -> None:
        page_one = {"_embedded": {"total": 3, "items": [{"name": "a"}, {"name": "b"}]}}
        page_two = {"_embedded": {"total": 3, "items": [{"name": "c"}]}}
        disk = client(json_response(page_one), json_response(page_two))
        names = [item["name"] for item in disk.listdir("/Фото", page=2)]
        self.assertEqual(names, ["a", "b", "c"])
        self.assertIn("offset=2", disk.opener.urls[1])

    def test_listdir_on_file_raises(self) -> None:
        disk = client(json_response({"type": "file", "name": "a.txt"}))
        with self.assertRaises(YaDiskError):
            list(disk.listdir("/a.txt"))

    def test_find_filters_by_substring(self) -> None:
        items = [
            {"name": "кора-01.jpg", "path": "disk:/a/кора-01.jpg"},
            {"name": "other.jpg", "path": "disk:/a/other.jpg"},
        ]
        disk = client(json_response({"items": items}))
        found = disk.find("кора", limit=10)
        self.assertEqual([item["name"] for item in found], ["кора-01.jpg"])

    def test_files_respects_limit(self) -> None:
        disk = client(json_response({"items": [{"name": "1"}, {"name": "2"}]}))
        self.assertEqual(len(list(disk.files(limit=2, page=50))), 2)
        self.assertIn("limit=2", disk.opener.urls[0])

    def test_walk_descends(self) -> None:
        root = {"_embedded": {"total": 2, "items": [
            {"name": "sub", "type": "dir", "path": "disk:/sub"},
            {"name": "top.txt", "type": "file", "path": "disk:/top.txt"},
        ]}}
        sub = {"_embedded": {"total": 1, "items": [
            {"name": "deep.txt", "type": "file", "path": "disk:/sub/deep.txt"},
        ]}}
        disk = client(json_response(root), json_response(sub))
        self.assertEqual(
            [item["name"] for item in disk.walk("/")], ["sub", "top.txt", "deep.txt"]
        )


class TestTransfers(unittest.TestCase):
    def test_download_writes_file_without_auth_on_href(self) -> None:
        disk = client(
            json_response({"href": "https://downloader.example/f", "method": "GET"}),
            FakeResponse(b"content"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "nested", "file.bin")
            disk.download("/file.bin", target)
            with open(target, "rb") as handle:
                self.assertEqual(handle.read(), b"content")
        self.assertIsNone(disk.opener.requests[1].get_header("Authorization"))

    def test_upload_puts_with_content_length(self) -> None:
        disk = client(
            json_response({"href": "https://uploader.example/f", "method": "PUT"}),
            FakeResponse(b"", status=201),
        )
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "a.txt")
            with open(source, "wb") as handle:
                handle.write(b"12345")
            result = disk.upload(source, "/a.txt", overwrite=True)
        self.assertEqual(result, {"path": "disk:/a.txt", "size": 5})
        self.assertIn("overwrite=true", disk.opener.urls[0])
        put = disk.opener.requests[1]
        self.assertEqual(put.get_method(), "PUT")
        self.assertEqual(put.get_header("Content-length"), "5")

    def test_publish_returns_url(self) -> None:
        disk = client(json_response({}), json_response({"public_url": "https://disk.yandex.ru/d/xyz"}))
        self.assertEqual(disk.publish("/Фото"), "https://disk.yandex.ru/d/xyz")


class TestErrors(unittest.TestCase):
    def test_404_becomes_yadiskerror(self) -> None:
        disk = client(http_error(404, {"description": "Не удалось найти запрошенный ресурс."}))
        with self.assertRaises(YaDiskError) as ctx:
            disk.meta("/нет")
        self.assertEqual(ctx.exception.status, 404)
        self.assertIn("Не удалось найти", str(ctx.exception))

    def test_429_is_retried(self) -> None:
        sleeps: list[float] = []
        disk = client(http_error(429, retry_after="2"), json_response({"used_space": 1}), sleeps=sleeps)
        self.assertEqual(disk.disk_info()["used_space"], 1)
        self.assertEqual(sleeps, [2.0])

    def test_400_is_not_retried(self) -> None:
        disk = client(http_error(400, {"description": "плохой путь"}))
        with self.assertRaises(YaDiskError):
            disk.meta("/x")
        self.assertEqual(len(disk.opener.requests), 1)

    def test_mkdir_parents_tolerates_existing(self) -> None:
        disk = client(http_error(409, {"description": "уже есть"}), json_response({"href": "x"}))
        disk.mkdir("/a/b", parents=True)
        self.assertIn("path=disk%3A%2Fa", disk.opener.urls[0])
        self.assertIn("path=disk%3A%2Fa%2Fb", disk.opener.urls[1])


class TestMcpServer(unittest.TestCase):
    def session(self, *responses: object) -> tuple[mcp_server.Session, FakeOpener]:
        opener = FakeOpener(*responses)
        disk = YandexDisk("t0ken", opener=opener, sleep=lambda _: None)
        return mcp_server.Session(lambda: disk), opener

    def test_initialize_echoes_supported_protocol(self) -> None:
        session, _ = self.session()
        response = mcp_server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05"}}, session)
        self.assertEqual(response["result"]["protocolVersion"], "2024-11-05")
        self.assertEqual(response["result"]["serverInfo"]["name"], "yadisk")

    def test_initialize_falls_back_on_unknown_protocol(self) -> None:
        session, _ = self.session()
        response = mcp_server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "1999-01-01"}}, session)
        self.assertEqual(response["result"]["protocolVersion"], mcp_server.DEFAULT_PROTOCOL)

    def test_notifications_get_no_reply(self) -> None:
        session, _ = self.session()
        self.assertIsNone(
            mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, session))

    def test_tools_list_is_valid(self) -> None:
        session, _ = self.session()
        tools = mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session)["result"]["tools"]
        self.assertGreaterEqual(len(tools), 10)
        for tool in tools:
            self.assertEqual(set(tool) , {"name", "description", "inputSchema"})
            self.assertEqual(tool["inputSchema"]["type"], "object")
            for field in tool["inputSchema"].get("required", []):
                self.assertIn(field, tool["inputSchema"]["properties"])

    def test_tools_call_returns_text_content(self) -> None:
        session, _ = self.session(json_response({"total_space": 1024, "used_space": 512}))
        response = mcp_server.handle(
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "yadisk_disk_info", "arguments": {}}}, session)
        result = response["result"]
        self.assertFalse(result["isError"])
        self.assertIn("1.0KB", result["content"][0]["text"])

    def test_api_error_is_reported_as_tool_error(self) -> None:
        session, _ = self.session(http_error(404, {"description": "нет ресурса"}))
        result = mcp_server.handle(
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "yadisk_stat", "arguments": {"path": "/нет"}}}, session)["result"]
        self.assertTrue(result["isError"])
        self.assertIn("нет ресурса", result["content"][0]["text"])

    def test_missing_required_argument_is_reported(self) -> None:
        session, _ = self.session()
        result = mcp_server.handle(
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
             "params": {"name": "yadisk_stat", "arguments": {}}}, session)["result"]
        self.assertTrue(result["isError"])
        self.assertIn("параметра", result["content"][0]["text"])

    def test_unknown_tool_and_method(self) -> None:
        session, _ = self.session()
        result = mcp_server.handle(
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
             "params": {"name": "nope", "arguments": {}}}, session)["result"]
        self.assertTrue(result["isError"])
        error = mcp_server.handle({"jsonrpc": "2.0", "id": 7, "method": "resources/list"}, session)
        self.assertEqual(error["error"]["code"], -32601)

    def test_serve_round_trip(self) -> None:
        session, _ = self.session()
        stdin = io.StringIO(
            '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
            '{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
            "\n"
            '{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n'
        )
        stdout = io.StringIO()
        mcp_server.serve(stdin, stdout, session)
        lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual([message["id"] for message in lines], [1, 2])

    def test_read_text_downloads_and_truncates(self) -> None:
        session, _ = self.session(
            json_response({"href": "https://downloader.example/f"}), FakeResponse("привет мир".encode()))
        result = mcp_server.handle(
            {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
             "params": {"name": "yadisk_read_text", "arguments": {"path": "/a.txt", "max_bytes": 12}}},
            session)["result"]
        payload = json.loads(result["content"][0]["text"])
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["text"], "привет")


if __name__ == "__main__":
    unittest.main(verbosity=2)
