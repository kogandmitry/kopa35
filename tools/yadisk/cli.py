"""Командная строка для Яндекс.Диска: ls, get, put, find, publish и прочее.

Примеры:
    python3 -m tools.yadisk.cli df
    python3 -m tools.yadisk.cli ls /Фотоархив
    python3 -m tools.yadisk.cli find кора --media-type image
    python3 -m tools.yadisk.cli get /Фотоархив/2024 ./archive --recursive
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from .api import MEDIA_TYPES, YaDiskError, YandexDisk, human_size
from .auth import CLIENT_ID_ENV, TokenNotFound, authorize_url, resolve_token, save_token


def _client(args: argparse.Namespace) -> YandexDisk:
    return YandexDisk(resolve_token(args.token))


def _emit(args: argparse.Namespace, payload: Any, lines: list[str]) -> None:
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for line in lines:
            print(line)


def _row(item: dict[str, Any]) -> str:
    kind = "d" if item.get("type") == "dir" else "-"
    size = "" if item.get("type") == "dir" else human_size(item.get("size"))
    modified = (item.get("modified") or "")[:19].replace("T", " ")
    return f"{kind} {size:>9}  {modified:19}  {item.get('path', item.get('name', ''))}"


def cmd_auth(args: argparse.Namespace) -> int:
    client_id = args.client_id or os.environ.get(CLIENT_ID_ENV)
    if not client_id:
        print(f"Укажите --client-id или переменную {CLIENT_ID_ENV}.", file=sys.stderr)
        return 2
    print("Откройте ссылку, подтвердите доступ и скопируйте значение access_token")
    print("из адресной строки (часть после #access_token= до первого &):\n")
    print("   " + authorize_url(client_id) + "\n")
    token = input("Токен: ").strip()
    if not token:
        print("Пустой токен — ничего не сохранено.", file=sys.stderr)
        return 1
    info = YandexDisk(token).disk_info()
    path = save_token(token)
    print(f"Токен сохранён в {path}")
    print(f"Диск: {human_size(info.get('used_space'))} из {human_size(info.get('total_space'))} занято")
    return 0


def cmd_df(args: argparse.Namespace) -> int:
    info = _client(args).disk_info()
    total, used = info.get("total_space"), info.get("used_space")
    free = total - used if isinstance(total, int) and isinstance(used, int) else None
    _emit(
        args,
        info,
        [
            f"Всего:    {human_size(total)}",
            f"Занято:   {human_size(used)}",
            f"Свободно: {human_size(free)}",
            f"Корзина:  {human_size(info.get('trash_size'))}",
        ],
    )
    return 0


def cmd_ls(args: argparse.Namespace) -> int:
    client = _client(args)
    items = list(client.walk(args.path, max_depth=args.depth)) if args.recursive \
        else list(client.listdir(args.path, sort=args.sort))
    _emit(args, items, [_row(item) for item in items] or ["(пусто)"])
    return 0


def cmd_stat(args: argparse.Namespace) -> int:
    meta = _client(args).meta(args.path, limit=1)
    _emit(
        args,
        meta,
        [
            f"путь:     {meta.get('path')}",
            f"тип:      {meta.get('type')}",
            f"размер:   {human_size(meta.get('size'))}",
            f"изменён:  {meta.get('modified')}",
            f"md5:      {meta.get('md5', '-')}",
            f"ссылка:   {meta.get('public_url', '-')}",
        ],
    )
    return 0


def cmd_find(args: argparse.Namespace) -> int:
    items = _client(args).find(
        args.query, media_type=args.media_type, limit=args.limit, scan_limit=args.scan_limit
    )
    _emit(args, items, [_row(item) for item in items] or ["Ничего не найдено"])
    return 0


def cmd_recent(args: argparse.Namespace) -> int:
    items = _client(args).last_uploaded(limit=args.limit, media_type=args.media_type)
    _emit(args, items, [_row(item) for item in items] or ["(пусто)"])
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    client = _client(args)
    if args.recursive:
        saved = client.download_tree(args.remote, args.local)
        _emit(args, saved, [f"Скачано файлов: {len(saved)} → {args.local}"])
    else:
        path = client.download(args.remote, args.local)
        _emit(args, {"path": path}, [f"Скачано: {path}"])
    return 0


def cmd_put(args: argparse.Namespace) -> int:
    result = _client(args).upload(args.local, args.remote, overwrite=args.overwrite)
    _emit(args, result, [f"Загружено: {result['path']} ({human_size(result['size'])})"])
    return 0


def cmd_mkdir(args: argparse.Namespace) -> int:
    result = _client(args).mkdir(args.path, parents=args.parents)
    _emit(args, result, [f"Создано: {args.path}"])
    return 0


def cmd_rm(args: argparse.Namespace) -> int:
    result = _client(args).remove(args.path, permanently=args.permanently)
    where = "удалено безвозвратно" if args.permanently else "перемещено в корзину"
    _emit(args, result, [f"{args.path}: {where}"])
    return 0


def cmd_mv(args: argparse.Namespace) -> int:
    result = _client(args).move(args.src, args.dst, overwrite=args.overwrite)
    _emit(args, result, [f"{args.src} → {args.dst}"])
    return 0


def cmd_cp(args: argparse.Namespace) -> int:
    result = _client(args).copy(args.src, args.dst, overwrite=args.overwrite)
    _emit(args, result, [f"{args.src} ⇒ {args.dst}"])
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    url = _client(args).publish(args.path)
    _emit(args, {"public_url": url}, [url or "(ссылка не получена)"])
    return 0


def cmd_unpublish(args: argparse.Namespace) -> int:
    result = _client(args).unpublish(args.path)
    _emit(args, result, [f"Публичная ссылка снята: {args.path}"])
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="yadisk", description="Работа с Яндекс.Диском")
    parser.add_argument("--token", help="OAuth-токен (по умолчанию — из окружения или файла)")
    parser.add_argument("--json", action="store_true", help="вывести сырой JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    auth = sub.add_parser("auth", help="получить и сохранить токен")
    auth.add_argument("--client-id", help=f"ClientID приложения (или {CLIENT_ID_ENV})")
    auth.set_defaults(func=cmd_auth)

    sub.add_parser("df", help="занятое место").set_defaults(func=cmd_df)

    ls = sub.add_parser("ls", help="содержимое папки")
    ls.add_argument("path", nargs="?", default="/")
    ls.add_argument("-r", "--recursive", action="store_true")
    ls.add_argument("--depth", type=int, help="ограничение глубины при -r")
    ls.add_argument("--sort", default="name", help="name, size, modified, created (с - для убывания)")
    ls.set_defaults(func=cmd_ls)

    stat_cmd = sub.add_parser("stat", help="сведения о файле или папке")
    stat_cmd.add_argument("path")
    stat_cmd.set_defaults(func=cmd_stat)

    find = sub.add_parser("find", help="поиск по подстроке в имени или пути")
    find.add_argument("query", nargs="?", default="")
    find.add_argument("--media-type", choices=MEDIA_TYPES)
    find.add_argument("--limit", type=int, default=50)
    find.add_argument("--scan-limit", type=int, default=5000, help="сколько файлов просмотреть")
    find.set_defaults(func=cmd_find)

    recent = sub.add_parser("recent", help="последние загруженные файлы")
    recent.add_argument("--limit", type=int, default=20)
    recent.add_argument("--media-type", choices=MEDIA_TYPES)
    recent.set_defaults(func=cmd_recent)

    get = sub.add_parser("get", help="скачать файл или папку")
    get.add_argument("remote")
    get.add_argument("local")
    get.add_argument("-r", "--recursive", action="store_true")
    get.set_defaults(func=cmd_get)

    put = sub.add_parser("put", help="загрузить файл")
    put.add_argument("local")
    put.add_argument("remote")
    put.add_argument("-f", "--overwrite", action="store_true")
    put.set_defaults(func=cmd_put)

    mkdir = sub.add_parser("mkdir", help="создать папку")
    mkdir.add_argument("path")
    mkdir.add_argument("-p", "--parents", action="store_true")
    mkdir.set_defaults(func=cmd_mkdir)

    rm = sub.add_parser("rm", help="удалить (по умолчанию — в корзину)")
    rm.add_argument("path")
    rm.add_argument("--permanently", action="store_true")
    rm.set_defaults(func=cmd_rm)

    mv = sub.add_parser("mv", help="переместить")
    mv.add_argument("src")
    mv.add_argument("dst")
    mv.add_argument("-f", "--overwrite", action="store_true")
    mv.set_defaults(func=cmd_mv)

    cp = sub.add_parser("cp", help="скопировать")
    cp.add_argument("src")
    cp.add_argument("dst")
    cp.add_argument("-f", "--overwrite", action="store_true")
    cp.set_defaults(func=cmd_cp)

    publish = sub.add_parser("publish", help="опубликовать и получить ссылку")
    publish.add_argument("path")
    publish.set_defaults(func=cmd_publish)

    unpublish = sub.add_parser("unpublish", help="снять публичную ссылку")
    unpublish.add_argument("path")
    unpublish.set_defaults(func=cmd_unpublish)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except TokenNotFound as exc:
        print(exc, file=sys.stderr)
        return 2
    except YaDiskError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
