# Доступ к Яндекс.Диску для Claude

Готового коннектора Яндекс.Диска в каталоге Claude нет, поэтому здесь лежит свой:
MCP-сервер на stdio плюс командная строка. Внешних зависимостей нет — нужен только
Python 3.9+.

```
tools/yadisk/
├── api.py          клиент REST API Яндекс.Диска
├── auth.py         поиск и хранение OAuth-токена
├── cli.py          командная строка (ls, get, put, find, publish…)
└── mcp_server.py   MCP-сервер: 14 инструментов для Claude
```

## Шаг 1. Получить OAuth-токен

1. Откройте <https://oauth.yandex.ru/client/new/> и создайте приложение.
   * Платформа — «Веб-сервисы», Redirect URI — `https://oauth.yandex.ru/verification_codes`
   * Права: `cloud_api:disk.info`, `cloud_api:disk.read`, `cloud_api:disk.write`
     (если Claude нужен только просмотр — не выдавайте `disk.write`)
2. Скопируйте **ClientID**.
3. Выполните:

   ```bash
   python3 -m tools.yadisk.cli auth --client-id <ClientID>
   ```

   Команда покажет ссылку; подтвердите доступ, скопируйте `access_token` из адресной
   строки и вставьте в приглашение. Токен ляжет в `~/.config/yadisk/token` с правами `600`.

Токен можно задать и напрямую — переменной `YANDEX_DISK_TOKEN` или файлом,
путь к которому указан в `YANDEX_DISK_TOKEN_FILE`. В репозиторий его не кладите.

## Шаг 2. Подключить как MCP-сервер

```bash
claude mcp add yadisk -- python3 /абсолютный/путь/kopa35/tools/yadisk/mcp_server.py
```

Токен сервер возьмёт из `~/.config/yadisk/token`. Если держите его в переменной,
добавьте `--env YANDEX_DISK_TOKEN=<токен>`. Проверка: `claude mcp list` — сервер должен
отвечать `✓ Connected`.

После этого в диалоге доступны инструменты:

| Инструмент | Что делает |
| --- | --- |
| `yadisk_disk_info` | занятое и свободное место |
| `yadisk_list` | содержимое папки, при `recursive` — дерево |
| `yadisk_stat` | размер, дата, md5, публичная ссылка |
| `yadisk_find` | поиск по подстроке с фильтром `image` / `video` / `document` |
| `yadisk_recent` | последние загруженные файлы |
| `yadisk_download` | скачать файл или папку на компьютер |
| `yadisk_read_text` | прочитать текстовый файл прямо в ответ |
| `yadisk_upload` | загрузить файл на Диск |
| `yadisk_mkdir` `yadisk_move` `yadisk_copy` `yadisk_delete` | операции с файлами |
| `yadisk_publish` `yadisk_unpublish` | публичные ссылки |

`yadisk_delete` по умолчанию отправляет в корзину; безвозвратно — только с
`permanently: true`.

## Шаг 3. Или просто из терминала

```bash
python3 -m tools.yadisk.cli df                       # сколько занято
python3 -m tools.yadisk.cli ls /Фотоархив            # список
python3 -m tools.yadisk.cli ls / -r --depth 2        # дерево
python3 -m tools.yadisk.cli find кора --media-type image
python3 -m tools.yadisk.cli get /Фотоархив/2024 ./archive -r
python3 -m tools.yadisk.cli put ./отчёт.pdf /Отчёты/отчёт.pdf -f
python3 -m tools.yadisk.cli publish /Отчёты/отчёт.pdf
```

Флаг `--json` у любой команды отдаёт сырой ответ API — удобно для скриптов
и конвейеров обработки медиаархива.

## Про облачные сессии Claude Code

В сессии на claude.ai/code исходящий трафик проходит через egress-прокси с политикой
сети. По умолчанию `cloud-api.yandex.net` и `webdav.yandex.ru` закрыты — прокси
отвечает `403`, и никакой токен это не обходит. Варианты:

* **запускать Claude Code локально** — тогда MCP-сервер работает без ограничений;
* **разрешить домены в окружении** — настройки сетевого доступа задаются при создании
  окружения, см. <https://code.claude.com/docs/en/claude-code-on-the-web>;
  нужны `cloud-api.yandex.net`, `downloader.disk.yandex.ru`, `uploader*.disk.yandex.net`,
  `oauth.yandex.ru`;
* **разово переложить нужные файлы** в Google Drive — этот коннектор уже подключён
  и в облачной сессии работает.

## Тесты

```bash
python3 -m unittest discover -s tests
```

Сеть в тестах не используется: HTTP подменяется заглушкой, проверяются пагинация,
повтор при `429`, заголовки выгрузки и весь цикл MCP-протокола.
