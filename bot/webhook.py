"""
Envia mensagens via Incoming Webhook do Google Chat (texto/erros)
e via Google Chat REST API com Service Account (cards interativos).
"""
import logging
import httpx

logger = logging.getLogger(__name__)


async def send_webhook(webhook_url: str, text: str):
    """Posta mensagem de texto simples no espaço via Incoming Webhook."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(webhook_url, json={"text": text})
    if r.status_code not in (200, 201):
        logger.error("Erro no webhook [%s]: %s", r.status_code, r.text)


async def send_card(webhook_url: str, card: dict):
    """Posta um card formatado no espaço via Incoming Webhook (sem botões interativos)."""
    fallback_text = card.pop("_fallback_text", "Erro ao formatar mensagem.")
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(webhook_url, json={"cardsV2": [card]})
    if r.status_code not in (200, 201):
        logger.error("Erro no webhook card [%s]: %s", r.status_code, r.text)
        await send_webhook(webhook_url, fallback_text)


async def send_interactive_card(space_name: str, card: dict, sa_file: str, fallback_text: str = "") -> bool:
    """
    Posta um card interativo (com botões funcionais) via Google Chat REST API
    usando Service Account. Retorna True se enviado com sucesso.

    space_name: ex 'spaces/AAQAZKvRf_I'
    card: dict no formato cardsV2 (sem _fallback_text)
    sa_file: caminho para o arquivo JSON da Service Account
    """
    try:
        from google.oauth2 import service_account
        import google.auth.transport.requests as _ga_requests

        creds = service_account.Credentials.from_service_account_file(
            sa_file,
            scopes=["https://www.googleapis.com/auth/chat.bot"],
        )
        creds.refresh(_ga_requests.Request())
        token = creds.token

        url = f"https://chat.googleapis.com/v1/{space_name}/messages"
        payload = {"cardsV2": [card]}

        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
            )

        if r.status_code in (200, 201):
            logger.info("[ChatAPI] Card interativo enviado para %s", space_name)
            return True

        logger.error("[ChatAPI] Falha ao enviar card [%s]: %s", r.status_code, r.text[:300])
        return False

    except Exception as e:
        logger.exception("[ChatAPI] Erro ao enviar card interativo: %s", e)
        return False

