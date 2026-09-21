"""Telegram Bot API client for NAZMan alerts.

Sends messages to a chat via the bot API over HTTPS using httpx (already a
production dependency).  A single posted message needs no ambient state, so
the ASD client is created per call; callers may inject a transport (e.g.
``httpx.MockTransport``) for tests.
"""

import httpx
from typing import Optional

from .exceptions import NAZManError

API_URL = "https://api.telegram.org/bot{token}/sendMessage"


async def send_telegram_message(
    bot_token: str,
    chat_id: str,
    text: str,
    timeout: float = 10.0,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> bool:
    """Send ``text`` to ``chat_id`` via the Telegram bot API.

    Raises :class:`NAZManError` when the request fails or Telegram reports a
    non-ok response (the API description is surfaced so setup mistakes like a
    wrong chat id are visible to the user).
    """
    token = (bot_token or "").strip()
    chat = (chat_id or "").strip()
    if not token or not chat:
        raise NAZManError("Telegram bot token and chat id are required")

    url = API_URL.format(token=token)
    payload = {"chat_id": chat, "text": text}

    try:
        async with httpx.AsyncClient(transport=transport, timeout=timeout) as client:
            response = await client.post(url, json=payload)
            body = response.json()
    except httpx.HTTPError as e:
        raise NAZManError(f"Telegram request failed: {e}") from e
    except ValueError as e:
        raise NAZManError(f"Telegram returned a non-JSON response: {e}") from e

    if response.status_code >= 400:
        raise NAZManError(
            f"Telegram API error {response.status_code}: "
            f"{body.get('description', body)}"
        )
    if not isinstance(body, dict) or not body.get("ok"):
        raise NAZManError(f"Telegram rejected the message: {body}")

    return True