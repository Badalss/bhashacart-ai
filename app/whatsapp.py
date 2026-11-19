from urllib.parse import urlsplit

import httpx

from .config import Store, settings
from .telemetry import hop

MAX_AUDIO = 4 * 1024 * 1024


class WhatsApp:
    def __init__(self, http: httpx.AsyncClient, phone_id: str, store: Store):
        self.http = http
        self.base = f"https://graph.facebook.com/{settings().whatsapp_graph_version}"
        self.phone_id = phone_id
        self.headers = {"Authorization": f"Bearer {store.whatsapp_token.get_secret_value()}"}

    async def send(self, recipient: str, kind: str, payload: dict) -> None:
        with hop("whatsapp_send"):
            response = await self.http.post(
                f"{self.base}/{self.phone_id}/messages", headers=self.headers,
                json={"messaging_product": "whatsapp", "recipient_type": "individual",
                      "to": recipient, "type": kind, kind: payload},
            )
            response.raise_for_status()

    async def buttons(self, recipient: str, body: str, choices: list[tuple[str, str]]) -> None:
        await self.send(recipient, "interactive", {
            "type": "button", "body": {"text": body}, "action": {"buttons": [
                {"type": "reply", "reply": {"id": key, "title": label}}
                for key, label in choices
            ]},
        })

    async def download(self, media_id: str) -> tuple[bytes, str]:
        with hop("whatsapp_media_download"):
            metadata = await self.http.get(f"{self.base}/{media_id}", headers=self.headers)
            metadata.raise_for_status()
            url = metadata.json()["url"]
            parsed = urlsplit(url)
            if parsed.scheme != "https" or parsed.hostname not in {
                "lookaside.fbsbx.com", "lookaside.facebook.com", "graph.facebook.com"
            } or parsed.port not in (None, 443):
                raise ValueError("unexpected WhatsApp media host")
            async with self.http.stream("GET", url, headers=self.headers) as response:
                response.raise_for_status()
                mime = response.headers.get("Content-Type", "").split(";")[0]
                if mime not in {"audio/ogg", "audio/mpeg", "audio/mp4", "audio/aac",
                                "audio/amr", "application/octet-stream"}:
                    raise ValueError("unsupported WhatsApp audio")
                audio = bytearray()
                async for chunk in response.aiter_bytes():
                    audio.extend(chunk)
                    if len(audio) > MAX_AUDIO:
                        raise ValueError("WhatsApp audio exceeds processing limit")
            return bytes(audio), "audio/ogg" if mime == "application/octet-stream" else mime

    async def send_audio(self, recipient: str, audio: bytes) -> None:
        with hop("whatsapp_media_upload"):
            upload = await self.http.post(
                f"{self.base}/{self.phone_id}/media", headers=self.headers,
                data={"messaging_product": "whatsapp"},
                files={"file": ("reply.mp3", audio, "audio/mpeg")},
            )
            upload.raise_for_status()
        await self.send(recipient, "audio", {"id": upload.json()["id"]})
