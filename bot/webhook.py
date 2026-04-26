"""
Envia mensagens via Incoming Webhook do Google Chat.
Suporta texto simples e cards formatados.
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
    """Posta um card formatado no espaço via Incoming Webhook."""
    fallback_text = card.pop("_fallback_text", "Erro ao formatar mensagem.")
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(webhook_url, json={"cardsV2": [card]})
    if r.status_code not in (200, 201):
        logger.error("Erro no webhook card [%s]: %s", r.status_code, r.text)
        await send_webhook(webhook_url, fallback_text)

