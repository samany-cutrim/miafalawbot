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


async def send_card_with_user_token(space_name: str, card: dict, oauth_token: str, thread_name: str = "") -> bool:
    """
    Posta um card interativo usando o token OAuth do usuário (Apps Script).
    O token pertence ao contexto do workspace e tem permissão para postar no espaço.

    space_name: ex 'spaces/AAQAZKvRf_I'
    card: dict no formato cardsV2
    oauth_token: token OAuth obtido via ScriptApp.getOAuthToken() no Apps Script
    thread_name: se informado, posta na mesma thread (ex: 'spaces/AAA/threads/BBB')
    """
    try:
        url = f"https://chat.googleapis.com/v1/{space_name}/messages"
        payload: dict = {"cardsV2": [card]}
        if thread_name:
            payload["thread"] = {"name": thread_name}

        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {oauth_token}"},
            )

        if r.status_code in (200, 201):
            logger.info("[ChatAPI] Card enviado via user token para %s", space_name)
            return True

        logger.error("[ChatAPI] Falha user token [%s]: %s", r.status_code, r.text[:300])
        return False

    except Exception as e:
        logger.exception("[ChatAPI] Erro ao enviar card via user token: %s", e)
        return False


async def send_card_with_service_account(
    space_name: str,
    card: dict,
    sa_file: str,
    thread_name: str = "",
) -> bool:
    """
    Posta um card interativo (com botões funcionais) via Google Chat REST API
    usando Service Account. Retorna True se enviado com sucesso.

    space_name: ex 'spaces/AAQAZKvRf_I'
    card: dict no formato cardsV2
    sa_file: caminho para o arquivo JSON da Service Account
    thread_name: se informado, posta na mesma thread (ex: 'spaces/AAA/threads/BBB')
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
        payload: dict = {"cardsV2": [card]}
        if thread_name:
            payload["thread"] = {"name": thread_name}

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
