"""
Cliente Google Chat API.
- Envia mensagens via API Key (mais simples para bots no Google Chat)
- Baixa arquivos via Service Account (necessário para anexos autenticados)
"""
import json
import logging
import os
import httpx
from google.oauth2 import service_account
from google.auth.transport.requests import Request as GoogleRequest

logger = logging.getLogger(__name__)
_sa_credentials = None

SCOPES = ["https://www.googleapis.com/auth/chat.bot"]


def _get_api_key() -> str:
    key = os.environ.get("GOOGLE_CHAT_API_KEY", "")
    if not key:
        raise RuntimeError("GOOGLE_CHAT_API_KEY não definida.")
    return key


def _load_sa_info() -> dict | None:
    for var in ["GOOGLE_SERVICE_ACCOUNT_JSON", "SA_JSON", "SERVICE_ACCOUNT_JSON"]:
        val = os.environ.get(var, "").strip()
        if val and val.startswith("{"):
            try:
                return json.loads(val)
            except Exception as e:
                logger.error("Erro ao parsear %s: %s", var, e)
    return None


def _get_sa_token() -> str:
    """Token da Service Account — usado apenas para download de anexos."""
    global _sa_credentials
    if _sa_credentials and _sa_credentials.valid:
        return _sa_credentials.token

    info = _load_sa_info()
    if info:
        _sa_credentials = service_account.Credentials.from_service_account_info(
            info, scopes=SCOPES
        )
    else:
        sa_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "/secrets/service-account.json")
        _sa_credentials = service_account.Credentials.from_service_account_file(
            sa_file, scopes=SCOPES
        )

    _sa_credentials.refresh(GoogleRequest())
    return _sa_credentials.token


async def send_message(space_name: str, text: str):
    """Envia mensagem usando API Key."""
    url = f"https://chat.googleapis.com/v1/{space_name}/messages"
    api_key = _get_api_key()
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            url,
            params={"key": api_key},
            json={"text": text},
        )
    if r.status_code not in (200, 201):
        logger.error("Erro ao enviar mensagem [%s]: %s", r.status_code, r.text)
    else:
        logger.info("Mensagem enviada para %s", space_name)


async def download_file(url: str) -> bytes:
    """Baixa anexo usando token da Service Account."""
    token = _get_sa_token()
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        r = await client.get(url, headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        return r.content
