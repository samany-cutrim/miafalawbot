"""
DecisionFA Bot v3 — Render + Google Apps Script
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, BackgroundTasks
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


class PdfRequest(BaseModel):
    pdf_url: str
    advogado: str
    texto: str = ""
    webhook_url: str


class BuscaRequest(BaseModel):
    tipo: str
    tema: str
    webhook_url: str


class AjudaRequest(BaseModel):
    webhook_url: str


# ---------------------------------------------------------------------------
# ENDPOINTS — todos sincrônicos para garantir entrega mesmo após hibernação
# ---------------------------------------------------------------------------

@app.post("/ajuda")
async def ajuda(req: AjudaRequest):
    """Síncrono — responde direto, sem background task."""
    await send_webhook(req.webhook_url, get_ajuda())
    return JSONResponse({"status": "ok"})


@app.post("/buscar")
async def buscar(req: BuscaRequest):
    """Síncrono — envia confirmação imediata e processa."""
    await send_webhook(req.webhook_url, "🔍 Buscando precedentes...")
    try:
        resultado = await processar_busca(req.tipo, req.tema)
        await send_webhook(req.webhook_url, resultado)
    except Exception as e:
        logger.exception("Erro na busca: %s", e)
        await send_webhook(req.webhook_url, "⚠️ Erro ao buscar precedentes.")
    return JSONResponse({"status": "ok"})


@app.post("/processar")
async def processar(req: PdfRequest, background_tasks: BackgroundTasks):
    """PDF vai em background pois pode demorar mais de 30s."""
    background_tasks.add_task(_run_pdf, req)
    return JSONResponse({"status": "processando"})


async def _run_pdf(req: PdfRequest):
    try:
        await send_webhook(req.webhook_url, "⏳ *Analisando decisão...*\nAguarde alguns instantes.")
        resultado = await processar_pdf(req.pdf_url, req.advogado, req.texto)
        await send_webhook(req.webhook_url, resultado)
    except Exception as e:
        logger.exception("Erro ao processar PDF: %s", e)
        await send_webhook(req.webhook_url, "⚠️ Erro interno ao processar a decisão. Tente reenviar.")


# ---------------------------------------------------------------------------
# HEALTH
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "version": "v3"}
