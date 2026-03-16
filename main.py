"""
DecisionFA Bot v2 — Google Chat (grupo compartilhado)

Fluxo simplificado:
  • Advogado envia PDF (+ opcionalmente "Cliente: X" e "Tipo: Y" no texto)
  • Bot analisa com Claude e registra na planilha automaticamente
  • Sem multi-step, sem sessões — cada mensagem é processada de forma independente
  • Suporta N advogados postando simultaneamente
"""

import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, BackgroundTasks, Request
from fastapi.responses import JSONResponse

from bot.handlers import handle_pdf_postagem, handle_busca, send_ajuda

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("DecisionFA Bot v2 iniciado.")
    yield


app = FastAPI(title="DecisionFA Bot v2", lifespan=lifespan)


# ---------------------------------------------------------------------------
# PARSE DO PAYLOAD
# ---------------------------------------------------------------------------

def parse_payload(body: dict) -> dict:
    chat = body.get("chat", {})
    msg_payload = chat.get("messagePayload", {})

    message = (
        msg_payload.get("message")
        or body.get("message")
        or chat.get("message")
        or {}
    )
    space = (
        msg_payload.get("space")
        or body.get("space")
        or message.get("space")
        or {}
    )

    space_name = space.get("name", "")

    raw_text = (
        message.get("text")
        or message.get("argumentText")
        or body.get("text")
        or ""
    )
    # Remove menções ao bot (<users/123456>)
    text = re.sub(r"<[^>]+>", "", raw_text).strip()

    # Nome do remetente
    sender = message.get("sender") or body.get("sender") or {}
    advogado = sender.get("displayName") or sender.get("name") or "Advogado"

    # Anexos
    attachments = (
        message.get("attachment")
        or message.get("attachments")
        or msg_payload.get("message", {}).get("attachment")
        or []
    )
    if isinstance(attachments, dict):
        attachments = [attachments]

    # Tipo de evento
    event_type = body.get("type", "")

    return {
        "space_name": space_name,
        "text": text,
        "advogado": advogado,
        "attachments": attachments,
        "event_type": event_type,
    }


def find_pdf(attachments: list) -> dict | None:
    for a in attachments:
        if (
            a.get("contentType") == "application/pdf"
            or a.get("mimeType") == "application/pdf"
            or "pdf" in (a.get("source") or "").lower()
            or "pdf" in (a.get("name") or "").lower()
            or a.get("downloadUri")  # anexo do Chat geralmente é PDF se enviado com PDF
        ):
            return a
    return None


# ---------------------------------------------------------------------------
# WEBHOOK
# ---------------------------------------------------------------------------

@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Responde 200 imediatamente (requisito do Google Chat: max 30s).
    Processa tudo em background.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "ok"})

    parsed = parse_payload(body)
    logger.info(
        "Evento recebido: type=%s space=%s advogado=%s attachments=%d texto='%s'",
        parsed["event_type"],
        parsed["space_name"],
        parsed["advogado"],
        len(parsed["attachments"]),
        parsed["text"][:80],
    )

    if parsed["space_name"]:
        background_tasks.add_task(process_event, parsed)

    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# PROCESSAMENTO
# ---------------------------------------------------------------------------

async def process_event(parsed: dict):
    space_name = parsed["space_name"]
    text = parsed["text"]
    advogado = parsed["advogado"]
    attachments = parsed["attachments"]

    try:
        # 1. Postagem com PDF → análise automática
        pdf = find_pdf(attachments)
        if pdf:
            await handle_pdf_postagem(space_name, text, pdf, advogado)
            return

        # 2. Comandos de busca
        tl = text.lower()

        if tl.startswith("/favoraveis") or tl.startswith("favoraveis"):
            tema = re.sub(r"^/?(favoraveis|favoráveis)\s*", "", text, flags=re.IGNORECASE).strip()
            await handle_busca(space_name, "favoraveis", tema)
            return

        if tl.startswith("/desfavoraveis") or tl.startswith("desfavoraveis"):
            tema = re.sub(r"^/?(desfavoraveis|desfavoráveis)\s*", "", text, flags=re.IGNORECASE).strip()
            await handle_busca(space_name, "desfavoraveis", tema)
            return

        if "/ajuda" in tl or "ajuda" in tl:
            await send_ajuda(space_name)
            return

        # 3. Menção ao bot sem PDF → mostra ajuda
        if text:
            await send_ajuda(space_name)

    except Exception as e:
        logger.exception("Erro ao processar evento [%s]: %s", space_name, e)
        try:
            await send_message(space_name, "⚠️ Erro interno. Tente novamente.")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# HEALTH CHECK
# ---------------------------------------------------------------------------

@app.get("/health")
@app.get("/kaithhealthcheck")
@app.get("/kaithheathcheck")
async def health():
    return {"status": "ok"}
