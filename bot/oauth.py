"""
OAuth 2.0 — autorização de usuários para postar cards interativos no Google Chat.

Fluxo:
1. Cada advogada visita /auth uma vez
2. Autoriza o app com sua conta Google (escopo chat.messages)
3. Render salva o refresh_token na planilha (aba _oauth_tokens)
4. A cada análise de PDF, Render renova o access_token e posta o card diretamente
"""

import logging
import os
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

OAUTH_CLIENT_ID     = os.environ.get("OAUTH_CLIENT_ID", "")
OAUTH_CLIENT_SECRET = os.environ.get("OAUTH_CLIENT_SECRET", "")
OAUTH_REDIRECT_URI  = os.environ.get(
    "OAUTH_REDIRECT_URI",
    "https://mia-falaw-bot-ngs5.onrender.com/auth/callback",
)

SCOPES = [
    "https://www.googleapis.com/auth/chat.messages",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]


def get_auth_url(state: str = "") -> str:
    """Gera a URL de autorização do Google OAuth."""
    params = {
        "client_id":     OAUTH_CLIENT_ID,
        "redirect_uri":  OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope":         " ".join(SCOPES),
        "access_type":   "offline",
        "prompt":        "consent",   # garante refresh_token sempre
    }
    if state:
        params["state"] = state
    return "https://accounts.google.com/o/oauth2/auth?" + urlencode(params)


async def exchange_code(code: str) -> dict:
    """Troca o código de autorização por access_token + refresh_token."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code":          code,
                "client_id":     OAUTH_CLIENT_ID,
                "client_secret": OAUTH_CLIENT_SECRET,
                "redirect_uri":  OAUTH_REDIRECT_URI,
                "grant_type":    "authorization_code",
            },
        )
        r.raise_for_status()
        return r.json()


async def refresh_access_token(refresh_token: str) -> str | None:
    """Obtém novo access_token usando o refresh_token armazenado."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "refresh_token": refresh_token,
                "client_id":     OAUTH_CLIENT_ID,
                "client_secret": OAUTH_CLIENT_SECRET,
                "grant_type":    "refresh_token",
            },
        )
        if r.status_code != 200:
            logger.error("Erro ao renovar token OAuth: %s", r.text[:200])
            return None
        return r.json().get("access_token")


async def get_user_info(access_token: str) -> dict:
    """Retorna email e displayName do usuário autenticado."""
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if r.status_code != 200:
            return {}
        return r.json()
