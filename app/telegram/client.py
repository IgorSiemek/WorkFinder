from __future__ import annotations

import httpx


class TelegramError(Exception):
    def __init__(self, code: int, description: str, retry_after: int = 0):
        super().__init__(description)
        self.code = code
        self.retry_after = retry_after


class TelegramClient:
    def __init__(self, token: str):
        self.http = httpx.AsyncClient(
            base_url=f"https://api.telegram.org/bot{token}/",
            timeout=40,
        )

    async def call(self, method: str, **payload):  # noqa: ANN003, ANN201
        response = await self.http.post(method, json=payload)
        try:
            body = response.json()
        except ValueError as exc:
            raise TelegramError(
                response.status_code, "Nieprawidłowa odpowiedź Telegram API"
            ) from exc
        if not body.get("ok"):
            raise TelegramError(
                body.get("error_code", response.status_code),
                body.get("description", "Telegram API error"),
                body.get("parameters", {}).get("retry_after", 0),
            )
        return body["result"]

    async def close(self) -> None:
        await self.http.aclose()
