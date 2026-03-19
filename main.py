"""
DecisionFA Bot v3 — Render + Google Apps Script

Fluxo:
  1. Apps Script detecta PDF postado no grupo do Chat
  2. Chama POST /processar com {pdf_url, advogado, texto, webhook_url}
  3. Este servidor baixa o PDF, analisa com Claude, salva na planilha
  4. Responde o resultado via Incoming Webhook no grupo

Vantagem: não precisa de bot registrado no Google Workspace.
O Apps Script usa as credenciais OAuth do próprio usuário (advogado).
"""

import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, BackgroundTasks, Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from bot.handlers import processar_pdf, processar_busca, get_ajuda
from bot.webhook import send_webhook

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("DecisionFA Bot v3 iniciado.")
    yield


app = FastAPI(title="DecisionFA Bot v3", lifespan=lifespan)


# ---------------------------------------------------------------------------
# MODELOS
# ---------------------------------------------------------------------------

class PdfRequest(BaseModel):
    pdf_url: str          # URL de download do anexo (com token OAuth do Apps Script)
    advogado: str         # displayName do remetente
    texto: str = ""       # texto da mensagem (pode ter Cliente: / Tipo:)
    webhook_url: str      # Incoming Webhook do espaço


class BuscaRequest(BaseModel):
    tipo: str             # "favoraveis" ou "desfavoraveis"
    tema: str
    webhook_url: str


class AjudaRequest(BaseModel):
    webhook_url: str


# ---------------------------------------------------------------------------
# ENDPOINTS
# ---------------------------------------------------------------------------

@app.post("/processar")
async def processar(req: PdfRequest, background_tasks: BackgroundTasks):
    """Recebe evento do Apps Script e processa em background."""
    background_tasks.add_task(_run_pdf, req)
    return JSONResponse({"status": "processando"})


@app.post("/buscar")
async def buscar(req: BuscaRequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(_run_busca, req)
    return JSONResponse({"status": "processando"})


@app.post("/ajuda")
async def ajuda(req: AjudaRequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(_run_ajuda, req)
    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# BACKGROUND TASKS
# ---------------------------------------------------------------------------

async def _run_pdf(req: PdfRequest):
    try:
        await send_webhook(req.webhook_url, "⏳ *Analisando decisão...*\nAguarde alguns instantes.")
        resultado = await processar_pdf(req.pdf_url, req.advogado, req.texto)
        await send_webhook(req.webhook_url, resultado)
    except Exception as e:
        logger.exception("Erro ao processar PDF: %s", e)
        await send_webhook(req.webhook_url, "⚠️ Erro interno ao processar a decisão. Tente reenviar.")


async def _run_busca(req: BuscaRequest):
    try:
        await send_webhook(req.webhook_url, f"🔍 Buscando precedentes...")
        resultado = await processar_busca(req.tipo, req.tema)
        await send_webhook(req.webhook_url, resultado)
    except Exception as e:
        logger.exception("Erro na busca: %s", e)
        await send_webhook(req.webhook_url, "⚠️ Erro ao buscar precedentes.")


async def _run_ajuda(req: AjudaRequest):
    await send_webhook(req.webhook_url, get_ajuda())


# ---------------------------------------------------------------------------
# HEALTH
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "version": "v3"}
