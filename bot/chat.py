"""
Cliente Google Chat API.
Carrega credenciais diretamente do JSON em memória.
"""
import json
import logging
import os
import httpx
from google.oauth2 import service_account
from google.auth.transport.requests import Request as GoogleRequest

logger = logging.getLogger(__name__)
_credentials = None


def _get_credentials():
    global _credentials
    if _credentials and _credentials.valid:
        return _credentials

    scopes = ["https://www.googleapis.com/auth/chat.bot"]

    # Tenta carregar do JSON direto (variável de ambiente)
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if sa_json:
        info = json.loads(sa_json)
        _credentials = service_account.Credentials.from_service_account_info(
            info, scopes=scopes
        )
    else:
        # Fallback: arquivo
        sa_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "/secrets/service-account.json")
        _credentials = service_account.Credentials.from_service_account_file(
            sa_file, scopes=scopes
        )

    _credentials.refresh(GoogleRequest())
    return _credentials


def _get_token() -> str:
    creds = _get_credentials()
    if not creds.valid:
        creds.refresh(GoogleRequest())
    return creds.token


async def send_message(space_name: str, text: str):
    url = f"https://chat.googleapis.com/v1/{space_name}/messages"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            url,
            headers={"Authorization": f"Bearer {_get_token()}"},
            json={"text": text},
        )
    if r.status_code not in (200, 201):
        logger.error("Erro ao enviar mensagem [%s]: %s", r.status_code, r.text)
    else:
        logger.info("Mensagem enviada para %s", space_name)


async def download_file(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        r = await client.get(url, headers={"Authorization": f"Bearer {_get_token()}"})
        r.raise_for_status()
        return r.content
