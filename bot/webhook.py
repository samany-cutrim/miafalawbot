"""
Envia mensagens via Incoming Webhook do Google Chat.
Não requer nenhuma credencial de bot — só a URL do webhook.
"""
import logging
import httpx

logger = logging.getLogger(__name__)


async def send_webhook(webhook_url: str, text: str):
    """Posta mensagem no espaço via Incoming Webhook."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(webhook_url, json={"text": text})
    if r.status_code not in (200, 201):
        logger.error("Erro no webhook [%s]: %s", r.status_code, r.text)
    else:
        logger.info("Webhook enviado com sucesso.")
