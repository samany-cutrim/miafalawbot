"""
Cliente Google Chat API via Service Account.
"""
import logging
import httpx
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from bot.config import GOOGLE_SERVICE_ACCOUNT_FILE, GOOGLE_CHAT_SCOPES

logger = logging.getLogger(__name__)
_credentials = None


def _get_token() -> str:
    global _credentials
    if _credentials is None or not _credentials.valid:
        creds = Credentials.from_service_account_file(
            GOOGLE_SERVICE_ACCOUNT_FILE, scopes=GOOGLE_CHAT_SCOPES
        )
        creds.refresh(GoogleRequest())
        _credentials = creds
    elif not _credentials.valid:
        _credentials.refresh(GoogleRequest())
    return _credentials.token


async def send_message(space_name: str, text: str):
    """Envia mensagem de texto simples ao espaço."""
    url = f"https://chat.googleapis.com/v1/{space_name}/messages"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            url,
            headers={"Authorization": f"Bearer {_get_token()}"},
            json={"text": text},
        )
    if r.status_code not in (200, 201):
        logger.error("Erro ao enviar mensagem [%s]: %s", r.status_code, r.text)


async def download_file(url: str) -> bytes:
    """Baixa arquivo autenticado do Google Chat."""
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        r = await client.get(url, headers={"Authorization": f"Bearer {_get_token()}"})
        r.raise_for_status()
        return r.content
