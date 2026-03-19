"""
DecisionFA Bot v3 — Render + Google Apps Script
"""

import base64
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from bot.handlers import processar_pdf, processar_pdf_bytes, processar_busca, get_ajuda
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


class PdfBase64Request(BaseModel):
    pdf_base64: str
    advogado: str
    texto: str = ""
    webhook_url: str


class BuscaRequest(BaseModel):
    tipo: str
    tema: str
    webhook_url: str


class AjudaRequest(BaseModel):
    webhook_url: str


@app.post("/ajuda")
async def ajuda(req: AjudaRequest):
    await send_webhook(req.webhook_url, get_ajuda())
    return JSONResponse({"status": "ok"})


@app.post("/buscar")
async def buscar(req: BuscaRequest):
    await send_webhook(req.webhook_url, "Buscando precedentes...")
    try:
        resultado = await processar_busca(req.tipo, req.tema)
        await send_webhook(req.webhook_url, resultado)
    except Exception as e:
        logger.exception("Erro na busca: %s", e)
        await send_webhook(req.webhook_url, "Erro ao buscar precedentes.")
    return JSONResponse({"status": "ok"})


@app.post("/processar-base64")
async def processar_base64(req: PdfBase64Request, background_tasks: BackgroundTasks):
    """Recebe PDF em base64 do Apps Script e processa em background."""
    background_tasks.add_task(_run_pdf_base64, req)
    return JSONResponse({"status": "processando"})


@app.post("/processar")
async def processar(req: PdfRequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(_run_pdf, req)
    return JSONResponse({"status": "processando"})


async def _run_pdf_base64(req: PdfBase64Request):
    try:
        pdf_bytes = base64.b64decode(req.pdf_base64)
        resultado = await processar_pdf_bytes(pdf_bytes, req.advogado, req.texto)
        await send_webhook(req.webhook_url, resultado)
    except Exception as e:
        logger.exception("Erro ao processar PDF base64: %s", e)
        await send_webhook(req.webhook_url, "Erro interno ao processar a decisao. Tente reenviar.")


async def _run_pdf(req: PdfRequest):
    try:
        await send_webhook(req.webhook_url, "Analisando decisao... Aguarde.")
        resultado = await processar_pdf(req.pdf_url, req.advogado, req.texto)
        await send_webhook(req.webhook_url, resultado)
    except Exception as e:
        logger.exception("Erro ao processar PDF: %s", e)
        await send_webhook(req.webhook_url, "Erro interno ao processar a decisao. Tente reenviar.")


@app.get("/health")
async def health():
    return {"status": "ok", "version": "v3"}
