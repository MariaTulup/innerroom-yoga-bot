#!/usr/bin/env python3
"""Inner Room Telegram bot.

Runs with long polling and only uses Python's standard library.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any


TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", "").strip()
DATABASE_PATH = Path(os.environ.get("DATABASE_PATH", "data/bot.sqlite3"))
PORT = int(os.environ.get("PORT", "10000"))
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").strip()
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()

WELCOME_TEXT = """привет, друг 🤍

рада, что ты здесь

Inner Room — это пространство для регулярной практики йоги

современная культура хорошо умеет разделять ум, тело и всё остальное.
йога, как мне кажется, — про то, чтобы постепенно собрать это обратно.

если хочешь быть частью Inner Room,
оплати участие и пришли сюда скрин —
я открою тебе доступ к чату 🤍

ссылка для оплаты:
https://tbank.ru/cf/5ESdKOHUGPu"""

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("innerroom-bot")


class TelegramAPIError(RuntimeError):
    pass


def api(method: str, **params: Any) -> Any:
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    encoded = urllib.parse.urlencode(
        {key: value for key, value in params.items() if value is not None}
    ).encode()
    request = urllib.request.Request(url, data=encoded)

    try:
        with urllib.request.urlopen(request, timeout=70) as response:
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError) as exc:
        raise TelegramAPIError(str(exc)) from exc

    if not payload.get("ok"):
        raise TelegramAPIError(payload.get("description", "Unknown Telegram error"))
    return payload.get("result")


def database() -> sqlite3.Connection:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            created_at INTEGER NOT NULL
        )
        """
    )
    return connection


def register_user(connection: sqlite3.Connection, user: dict[str, Any]) -> bool:
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO users (user_id, username, first_name, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (
            user["id"],
            user.get("username"),
            user.get("first_name"),
            int(time.time()),
        ),
    )
    connection.commit()
    return cursor.rowcount == 1


def user_label(user: dict[str, Any]) -> str:
    username = user.get("username")
    if username:
        return f"@{username}"
    return user.get("first_name") or "без username"


def send_text(chat_id: int, text: str) -> None:
    api(
        "sendMessage",
        chat_id=chat_id,
        text=text,
        disable_web_page_preview="true",
    )


def handle_admin_message(message: dict[str, Any], admin_id: int) -> None:
    text = message.get("text", "")
    if not text.startswith("/reply"):
        return

    parts = text.split(maxsplit=2)
    if len(parts) < 3:
        send_text(admin_id, "Использование: /reply ID текст сообщения")
        return

    try:
        recipient_id = int(parts[1].strip("<>"))
        send_text(recipient_id, parts[2])
    except ValueError:
        send_text(admin_id, "❌ ID пользователя должен быть числом")
    except TelegramAPIError as exc:
        send_text(admin_id, f"❌ Не удалось отправить сообщение: {exc}")
    else:
        send_text(admin_id, f"✅ Сообщение отправлено пользователю {recipient_id}")


def handle_client_message(
    connection: sqlite3.Connection,
    message: dict[str, Any],
    admin_id: int,
) -> None:
    user = message.get("from") or {}
    chat = message.get("chat") or {}
    user_id = user.get("id")
    chat_id = chat.get("id")
    message_id = message.get("message_id")

    if not all(isinstance(value, int) for value in (user_id, chat_id, message_id)):
        return

    is_new = register_user(connection, user)
    if is_new:
        send_text(
            admin_id,
            f"🔔 Новый пользователь!\n👤 {user_label(user)} | ID: {user_id}",
        )

    text = message.get("text", "")
    if text.startswith("/start"):
        send_text(chat_id, WELCOME_TEXT)
        return

    send_text(
        admin_id,
        f"💬 Сообщение от клиента:\n👤 {user_label(user)} | ID: {user_id}",
    )
    try:
        api(
            "forwardMessage",
            chat_id=admin_id,
            from_chat_id=chat_id,
            message_id=message_id,
        )
    except TelegramAPIError:
        api(
            "copyMessage",
            chat_id=admin_id,
            from_chat_id=chat_id,
            message_id=message_id,
        )


def handle_update(
    connection: sqlite3.Connection,
    update: dict[str, Any],
    admin_id: int,
) -> None:
    message = update.get("message")
    if not isinstance(message, dict):
        return

    sender_id = (message.get("from") or {}).get("id")
    if sender_id == admin_id:
        handle_admin_message(message, admin_id)
    else:
        handle_client_message(connection, message, admin_id)


def run() -> None:
    if not TOKEN:
        raise SystemExit("BOT_TOKEN is required")
    try:
        admin_id = int(ADMIN_ID_RAW)
    except ValueError as exc:
        raise SystemExit("ADMIN_ID must be a number") from exc

    connection = database()
    bot = api("getMe")
    logger.info("Starting @%s", bot.get("username"))

    if WEBHOOK_URL:
        run_webhook(connection, admin_id)
    else:
        run_polling(connection, admin_id)


def run_webhook(connection: sqlite3.Connection, admin_id: int) -> None:
    webhook_endpoint = f"{WEBHOOK_URL.rstrip('/')}/telegram"
    api(
        "setWebhook",
        url=webhook_endpoint,
        secret_token=WEBHOOK_SECRET or None,
        allowed_updates=json.dumps(["message"]),
    )
    logger.info("Webhook configured at %s", webhook_endpoint)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b"Inner Room bot is running"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            if self.path != "/telegram":
                self.send_error(404)
                return
            if WEBHOOK_SECRET:
                received = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
                if received != WEBHOOK_SECRET:
                    self.send_error(403)
                    return

            try:
                length = int(self.headers.get("Content-Length", "0"))
                update = json.loads(self.rfile.read(length))
                handle_update(connection, update, admin_id)
            except Exception:
                logger.exception("Failed to process webhook update")
                self.send_error(500)
                return

            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            logger.info("HTTP " + format, *args)

    server = HTTPServer(("0.0.0.0", PORT), Handler)
    logger.info("Listening on port %s", PORT)
    server.serve_forever()


def run_polling(connection: sqlite3.Connection, admin_id: int) -> None:
    api("deleteWebhook", drop_pending_updates="false")
    logger.info("Long polling enabled")

    offset: int | None = None
    while True:
        try:
            updates = api(
                "getUpdates",
                offset=offset,
                timeout=50,
                allowed_updates=json.dumps(["message"]),
            )
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    handle_update(connection, update, admin_id)
                except Exception:
                    logger.exception("Failed to process update %s", update["update_id"])
        except TelegramAPIError as exc:
            logger.warning("Telegram connection error: %s", exc)
            time.sleep(5)
        except Exception:
            logger.exception("Unexpected polling error")
            time.sleep(5)


if __name__ == "__main__":
    run()
#!/usr/bin/env python3
"""Inner Room Telegram bot.

Runs with long polling and only uses Python's standard library.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", "").strip()
DATABASE_PATH = Path(os.environ.get("DATABASE_PATH", "data/bot.sqlite3"))

WELCOME_TEXT = """привет, друг 🤍

рада, что ты здесь

Inner Room — это пространство для регулярной практики йоги

современная культура хорошо умеет разделять ум, тело и всё остальное.
йога, как мне кажется, — про то, чтобы постепенно собрать это обратно.

если хочешь быть частью Inner Room,
оплати участие и пришли сюда скрин —
я открою тебе доступ к чату 🤍

ссылка для оплаты:
https://tbank.ru/cf/5ESdKOHUGPu"""

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("innerroom-bot")


class TelegramAPIError(RuntimeError):
    pass


def api(method: str, **params: Any) -> Any:
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    encoded = urllib.parse.urlencode(
        {key: value for key, value in params.items() if value is not None}
    ).encode()
    request = urllib.request.Request(url, data=encoded)

    try:
        with urllib.request.urlopen(request, timeout=70) as response:
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError) as exc:
        raise TelegramAPIError(str(exc)) from exc

    if not payload.get("ok"):
        raise TelegramAPIError(payload.get("description", "Unknown Telegram error"))
    return payload.get("result")


def database() -> sqlite3.Connection:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            created_at INTEGER NOT NULL
        )
        """
    )
    return connection


def register_user(connection: sqlite3.Connection, user: dict[str, Any]) -> bool:
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO users (user_id, username, first_name, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (
            user["id"],
            user.get("username"),
            user.get("first_name"),
            int(time.time()),
        ),
    )
    connection.commit()
    return cursor.rowcount == 1


def user_label(user: dict[str, Any]) -> str:
    username = user.get("username")
    if username:
        return f"@{username}"
    return user.get("first_name") or "без username"


def send_text(chat_id: int, text: str) -> None:
    api(
        "sendMessage",
        chat_id=chat_id,
        text=text,
        disable_web_page_preview="true",
    )


def handle_admin_message(message: dict[str, Any], admin_id: int) -> None:
    text = message.get("text", "")
    if not text.startswith("/reply"):
        return

    parts = text.split(maxsplit=2)
    if len(parts) < 3:
        send_text(admin_id, "Использование: /reply ID текст сообщения")
        return

    try:
        recipient_id = int(parts[1].strip("<>"))
        send_text(recipient_id, parts[2])
    except ValueError:
        send_text(admin_id, "❌ ID пользователя должен быть числом")
    except TelegramAPIError as exc:
        send_text(admin_id, f"❌ Не удалось отправить сообщение: {exc}")
    else:
        send_text(admin_id, f"✅ Сообщение отправлено пользователю {recipient_id}")


def handle_client_message(
    connection: sqlite3.Connection,
    message: dict[str, Any],
    admin_id: int,
) -> None:
    user = message.get("from") or {}
    chat = message.get("chat") or {}
    user_id = user.get("id")
    chat_id = chat.get("id")
    message_id = message.get("message_id")

    if not all(isinstance(value, int) for value in (user_id, chat_id, message_id)):
        return

    is_new = register_user(connection, user)
    if is_new:
        send_text(
            admin_id,
            f"🔔 Новый пользователь!\n👤 {user_label(user)} | ID: {user_id}",
        )

    text = message.get("text", "")
    if text.startswith("/start"):
        send_text(chat_id, WELCOME_TEXT)
        return

    send_text(
        admin_id,
        f"💬 Сообщение от клиента:\n👤 {user_label(user)} | ID: {user_id}",
    )
    try:
        api(
            "forwardMessage",
            chat_id=admin_id,
            from_chat_id=chat_id,
            message_id=message_id,
        )
    except TelegramAPIError:
        api(
            "copyMessage",
            chat_id=admin_id,
            from_chat_id=chat_id,
            message_id=message_id,
        )


def handle_update(
    connection: sqlite3.Connection,
    update: dict[str, Any],
    admin_id: int,
) -> None:
    message = update.get("message")
    if not isinstance(message, dict):
        return

    sender_id = (message.get("from") or {}).get("id")
    if sender_id == admin_id:
        handle_admin_message(message, admin_id)
    else:
        handle_client_message(connection, message, admin_id)


def run() -> None:
    if not TOKEN:
        raise SystemExit("BOT_TOKEN is required")
    try:
        admin_id = int(ADMIN_ID_RAW)
    except ValueError as exc:
        raise SystemExit("ADMIN_ID must be a number") from exc

    connection = database()
    api("deleteWebhook", drop_pending_updates="false")
    bot = api("getMe")
    logger.info("Started @%s", bot.get("username"))

    offset: int | None = None
    while True:
        try:
            updates = api(
                "getUpdates",
                offset=offset,
                timeout=50,
                allowed_updates=json.dumps(["message"]),
            )
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    handle_update(connection, update, admin_id)
                except Exception:
                    logger.exception("Failed to process update %s", update["update_id"])
        except TelegramAPIError as exc:
            logger.warning("Telegram connection error: %s", exc)
            time.sleep(5)
        except Exception:
            logger.exception("Unexpected polling error")
            time.sleep(5)


if __name__ == "__main__":
    run()
