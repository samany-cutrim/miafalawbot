"""
Mia Falaw Bot

Modo principal sem admin de Workspace:
- Google Apps Script + Incoming Webhook + endpoints HTTP.

Modo opcional:
- Google Chat App (endpoint /chat), se houver permissao.
"""

import base64
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import BackgroundTasks, FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from bot.config import GITHUB_TOKEN
from bot.handlers import (
    cancelar_sessao,
    cancelar_sessao_data,
    confirmar_sessao,
    confirmar_sessao_data,
    corrigir_sessao,
    corrigir_sessao_data,
    dispensar_email_sessao,
    dispensar_email_sessao_data,
    esta_aguardando_correcao,
    gerar_email_sessao,
    gerar_email_sessao_data,
    get_ajuda_card,
    get_link,
    marcar_aguardando_correcao,
    processar_busca,
    processar_pdf,
    processar_pdf_bytes,
    processar_texto,
    processar_texto_chat,
)
from bot.webhook import send_card, send_webhook

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Mia Falaw Bot iniciado.")
    yield


app = FastAPI(title="Mia Falaw Bot", lifespan=lifespan)


@app.get("/")
async def root():
    return {"status": "ok", "service": "mia-falaw-bot"}


# ---------------------------------------------------------------------------
# REQUEST MODELS (Apps Script / Webhook)
# ---------------------------------------------------------------------------

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


class TextoRequest(BaseModel):
    texto_pdf: str
    advogado: str
    texto: str = ""
    webhook_url: str


class TextoSyncRequest(BaseModel):
    texto_pdf: str
    advogado: str
    texto: str = ""


class BuscaRequest(BaseModel):
    tipo: str
    tema: str
    webhook_url: str


class BuscaSyncRequest(BaseModel):
    tipo: str
    tema: str


class AjudaRequest(BaseModel):
    webhook_url: str


class LinkRequest(BaseModel):
    webhook_url: str


class ConfirmarRequest(BaseModel):
    advogado: str
    webhook_url: str


class CancelarRequest(BaseModel):
    advogado: str
    webhook_url: str


class CorrigirRequest(BaseModel):
    advogado: str
    instrucao: str
    webhook_url: str


class SimRequest(BaseModel):
    advogado: str
    webhook_url: str


class NaoRequest(BaseModel):
    advogado: str
    webhook_url: str


# ---------------------------------------------------------------------------
# APPS SCRIPT / WEBHOOK ENDPOINTS (SEM ADMIN)
# ---------------------------------------------------------------------------

@app.post("/ajuda")
async def ajuda(req: AjudaRequest):
    await send_card(req.webhook_url, get_ajuda_card())
    return JSONResponse({"status": "ok"})


@app.post("/link")
async def link(req: LinkRequest):
    await send_webhook(req.webhook_url, get_link())
    return JSONResponse({"status": "ok"})


@app.post("/buscar")
async def buscar(req: BuscaRequest):
    await send_webhook(req.webhook_url, "🔍 Buscando precedentes...")
    try:
        resultado = await processar_busca(req.tipo, req.tema)
        await send_webhook(req.webhook_url, resultado)
    except Exception as e:
        logger.exception("Erro na busca: %s", e)
        await send_webhook(req.webhook_url, "⚠️ Erro ao buscar precedentes.")
    return JSONResponse({"status": "ok"})


@app.post("/buscar-sync")
async def buscar_sync(req: BuscaSyncRequest):
    """Retorna resultado da busca diretamente, sem webhook (para o Chat App modal)."""
    try:
        resultado = await processar_busca(req.tipo, req.tema)
        return JSONResponse({"status": "ok", "resultado": resultado})
    except Exception as e:
        logger.exception("Erro na busca sync: %s", e)
        return JSONResponse({"status": "erro", "mensagem": "Erro ao buscar precedentes."}, status_code=500)


@app.post("/confirmar")
async def confirmar(req: ConfirmarRequest):
    await confirmar_sessao(req.advogado, req.webhook_url)
    return JSONResponse({"status": "ok"})


@app.post("/cancelar")
async def cancelar(req: CancelarRequest):
    await cancelar_sessao(req.advogado, req.webhook_url)
    return JSONResponse({"status": "ok"})


@app.post("/corrigir")
async def corrigir(req: CorrigirRequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(corrigir_sessao, req.advogado, req.instrucao, req.webhook_url)
    return JSONResponse({"status": "corrigindo"})


@app.post("/sim")
async def sim(req: SimRequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(gerar_email_sessao, req.advogado, req.webhook_url)
    return JSONResponse({"status": "gerando_email"})


@app.post("/nao")
async def nao(req: NaoRequest):
    await dispensar_email_sessao(req.advogado, req.webhook_url)
    return JSONResponse({"status": "ok"})


@app.post("/processar-texto")
async def processar_texto_endpoint(req: TextoRequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(_run_texto, req)
    return JSONResponse({"status": "processando"})


@app.post("/processar-texto-sync")
async def processar_texto_sync(req: TextoSyncRequest):
    """Processa texto de forma síncrona para uso no fluxo do Chat App modal.
    Não inclui o texto de confirmação (/confirmar, /cancelar...) — os botões
    são exibidos diretamente na card pelo Apps Script.
    """
    try:
        resultado = await processar_texto(req.texto_pdf, req.advogado, req.texto, webhook_url="", include_confirmacao_text=False)
        return JSONResponse({"status": "ok", "resultado": resultado})
    except Exception as e:
        logger.exception("Erro ao processar texto (sync): %s", e)
        return JSONResponse({"status": "erro", "mensagem": "Erro ao processar a decisão."}, status_code=500)


@app.post("/processar-base64")
async def processar_base64(req: PdfBase64Request, background_tasks: BackgroundTasks):
    background_tasks.add_task(_run_pdf_base64, req)
    return JSONResponse({"status": "processando"})


@app.post("/processar")
async def processar(req: PdfRequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(_run_pdf, req)
    return JSONResponse({"status": "processando"})


async def _run_texto(req: TextoRequest):
    try:
        resultado = await processar_texto(req.texto_pdf, req.advogado, req.texto, req.webhook_url)
        await send_webhook(req.webhook_url, resultado)
    except Exception as e:
        logger.exception("Erro ao processar texto: %s", e)
        await send_webhook(req.webhook_url, "⚠️ Erro ao processar a decisao. Tente reenviar.")


async def _run_pdf_base64(req: PdfBase64Request):
    try:
        pdf_bytes = base64.b64decode(req.pdf_base64)
        resultado = await processar_pdf_bytes(pdf_bytes, req.advogado, req.texto, req.webhook_url)
        await send_webhook(req.webhook_url, resultado)
    except Exception as e:
        logger.exception("Erro ao processar PDF base64: %s", e)
        await send_webhook(req.webhook_url, "⚠️ Erro ao processar a decisao. Tente reenviar.")


async def _run_pdf(req: PdfRequest):
    try:
        await send_webhook(req.webhook_url, "⏳ Analisando decisao... Aguarde.")
        resultado = await processar_pdf(req.pdf_url, req.advogado, req.texto, req.webhook_url)
        await send_webhook(req.webhook_url, resultado)
    except Exception as e:
        logger.exception("Erro ao processar PDF: %s", e)
        await send_webhook(req.webhook_url, "⚠️ Erro ao processar a decisao. Tente reenviar.")


# ---------------------------------------------------------------------------
# ENDPOINT /chat (opcional, se houver permissao para Chat App)
# ---------------------------------------------------------------------------

def _as_html(text: str) -> str:
    if not text:
        return ""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")


def _user_name(event: dict) -> str:
    user = event.get("user") or {}
    return user.get("displayName") or "Advogado"


def _message_text(event: dict) -> str:
    msg = event.get("message") or {}
    text = msg.get("argumentText") or msg.get("text") or ""
    return text.strip()


def _action_function(event: dict) -> str:
    action = (event.get("action") or {})
    invoked = action.get("actionMethodName")
    if invoked:
        return invoked
    common = event.get("common") or {}
    invoked = common.get("invokedFunction")
    return invoked or ""


def _form_value(event: dict, key: str) -> str:
    form_inputs = (event.get("common") or {}).get("formInputs") or {}
    data = form_inputs.get(key) or {}
    str_inputs = data.get("stringInputs") or {}
    values = str_inputs.get("value") or []
    if values:
        return str(values[0]).strip()
    return ""


def _base_header(subtitle: str) -> dict:
    return {
        "title": "Mia Falaw Bot",
        "subtitle": subtitle,
    }


def _primary_button(label: str, function_name: str) -> dict:
    return {
        "text": label,
        "onClick": {
            "action": {
                "function": function_name,
            }
        },
    }


def _card_with_buttons(subtitle: str, text: str, buttons: list[dict], card_id: str) -> dict:
    widgets = [{"textParagraph": {"text": _as_html(text)}}]
    if buttons:
        widgets.append({"buttonList": {"buttons": buttons}})
    return {
        "cardId": card_id,
        "card": {
            "header": _base_header(subtitle),
            "sections": [{"widgets": widgets}],
        },
    }


def _home_card() -> dict:
    return {
        "cardId": "home",
        "card": {
            "header": _base_header("Analise de decisoes trabalhistas"),
            "sections": [
                {
                    "widgets": [
                        {
                            "textParagraph": {
                                "text": "Clique em <b>Enviar decisao</b> para abrir o modal e iniciar a analise."
                            }
                        },
                        {
                            "buttonList": {
                                "buttons": [_primary_button("Enviar decisao", "open_decision_dialog")]
                            }
                        },
                    ]
                }
            ],
        },
    }


def _analysis_actions_card(analysis_text: str) -> dict:
    return _card_with_buttons(
        "Analise concluida",
        analysis_text,
        [
            _primary_button("Confirmar", "confirm_decision"),
            _primary_button("Cancelar", "cancel_decision"),
            _primary_button("Corrigir", "request_correction"),
        ],
        "analysis_actions",
    )


def _email_choice_card() -> dict:
    return _card_with_buttons(
        "Sugestao de e-mail",
        "Deseja gerar sugestao de e-mail para o cliente?",
        [_primary_button("Sim", "email_yes"), _primary_button("Nao", "email_no")],
        "email_choice",
    )


def _dialog_response() -> dict:
    tipos = [
        "OL",
        "Nuvem",
        "Terceirizacao",
        "Subsidiaria",
        "Ex Funcionario",
        "Ex-Foodlovers",
        "Marketplace",
    ]
    return {
        "actionResponse": {
            "type": "DIALOG",
            "dialogAction": {
                "dialog": {
                    "body": {
                        "sections": [
                            {
                                "header": "Nova decisao",
                                "widgets": [
                                    {
                                        "textInput": {
                                            "name": "cliente",
                                            "label": "Cliente (opcional)",
                                            "type": "SINGLE_LINE",
                                        }
                                    },
                                    {
                                        "selectionInput": {
                                            "name": "tipo_responsabilidade",
                                            "label": "Tipo de Responsabilidade",
                                            "type": "DROPDOWN",
                                            "items": [{"text": tipo, "value": tipo} for tipo in tipos],
                                        }
                                    },
                                    {
                                        "textInput": {
                                            "name": "decisao",
                                            "label": "Decisao (cole o texto ou link do documento)",
                                            "type": "MULTIPLE_LINE",
                                        }
                                    },
                                    {
                                        "buttonList": {
                                            "buttons": [_primary_button("Enviar decisao", "submit_decision_dialog")]
                                        }
                                    },
                                ],
                            }
                        ]
                    }
                }
            },
        }
    }


def _text_response(text: str) -> dict:
    return {"text": text}


def _cards_response(cards: list[dict]) -> dict:
    return {"cardsV2": cards}


async def _handle_message(event: dict) -> dict:
    advogado = _user_name(event)
    texto = _message_text(event)

    if await esta_aguardando_correcao(advogado):
        if not texto:
            return _text_response("Informe no chat a instrucao de correcao.")
        ok, msg = await corrigir_sessao_data(advogado, texto)
        if not ok:
            return _text_response(msg)
        return _cards_response([_analysis_actions_card(msg)])

    return _cards_response([_home_card()])


async def _handle_card_click(event: dict) -> dict:
    advogado = _user_name(event)
    function_name = _action_function(event)

    if function_name == "open_decision_dialog":
        return _dialog_response()

    if function_name == "submit_decision_dialog":
        cliente = _form_value(event, "cliente")
        tipo = _form_value(event, "tipo_responsabilidade")
        decisao = _form_value(event, "decisao")

        if not decisao:
            return _text_response("Informe o conteudo da decisao no campo do modal.")

        resultado = await processar_texto_chat(
            texto_pdf=decisao,
            advogado=advogado,
            cliente=cliente,
            tipo_responsabilidade=tipo,
        )
        return _cards_response([_analysis_actions_card(resultado)])

    if function_name == "confirm_decision":
        mensagem, oferecer_email = await confirmar_sessao_data(advogado)
        cards = [_card_with_buttons("Confirmacao", mensagem, [], "confirmation")]
        if oferecer_email:
            cards.append(_email_choice_card())
        return _cards_response(cards)

    if function_name == "cancel_decision":
        return _text_response(await cancelar_sessao_data(advogado))

    if function_name == "request_correction":
        return _text_response(await marcar_aguardando_correcao(advogado))

    if function_name == "email_yes":
        return _text_response(await gerar_email_sessao_data(advogado))

    if function_name == "email_no":
        return _text_response(await dispensar_email_sessao_data(advogado))

    return _text_response("Acao nao reconhecida.")


@app.post("/chat")
async def chat_event(event: dict):
    # Evento de autorização do Google Chat (verificação do endpoint)
    if "authorizationEventObject" in event and not event.get("type"):
        return {}

    event_type = event.get("type", "")
    logger.info("[/chat] tipo=%s user=%s", event_type, (event.get("user") or event.get("message", {}).get("sender") or {}).get("displayName", "?"))

    if event_type == "ADDED_TO_SPACE":
        return _cards_response([_home_card()])

    if event_type == "MESSAGE":
        try:
            return await _handle_message(event)
        except Exception as exc:
            logger.exception("[/chat] erro em _handle_message: %s", exc)
            return _cards_response([_home_card()])

    if event_type == "CARD_CLICKED":
        try:
            return await _handle_card_click(event)
        except Exception as exc:
            logger.exception("[/chat] erro em _handle_card_click: %s", exc)
            return _text_response("Ocorreu um erro. Tente novamente.")

    logger.warning("[/chat] evento nao tratado: %s | payload: %s", event_type, str(event)[:500])
    return _text_response("Evento nao suportado.")


@app.get("/health")
async def health():
    return {"status": "ok", "version": "mia-falaw-bot-v2-appscript"}


@app.get("/debug-models")
async def debug_models():
    if not GITHUB_TOKEN:
        return {"error": "GITHUB_TOKEN nao configurado"}
    try:
        # Usa httpx direto para evitar erro de parsing Pydantic do SDK openai
        async with httpx.AsyncClient(timeout=15) as cli:
            r = await cli.get(
                "https://models.inference.ai.azure.com/models",
                headers={"Authorization": f"Bearer {GITHUB_TOKEN}"},
            )
            if r.status_code != 200:
                return {"error": f"HTTP {r.status_code}", "body": r.text[:500]}
            data = r.json()
            if isinstance(data, list):
                ids = sorted(m.get("id", "") or m.get("name", "") for m in data if isinstance(m, dict))
            elif isinstance(data, dict):
                items = data.get("data") or data.get("models") or data.get("value") or []
                ids = sorted(m.get("id", "") or m.get("name", "") for m in items if isinstance(m, dict))
            else:
                return {"error": "formato inesperado", "raw": str(data)[:500]}
            ids = [i for i in ids if i]
            return {"total": len(ids), "models": ids}
    except Exception as e:
        return {"error": str(e)}
