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
            "header": _base_header("Análise de decisões trabalhistas"),
            "sections": [
                {
                    "widgets": [
                        {
                            "textParagraph": {
                                "text": "Selecione uma opção abaixo:"
                            }
                        },
                        {
                            "buttonList": {
                                "buttons": [
                                    _primary_button("Enviar decisão", "open_decision_dialog"),
                                    _primary_button("Busca de precedentes", "open_busca_card"),
                                    _primary_button("Ajuda", "open_ajuda"),
                                ]
                            }
                        },
                    ]
                }
            ],
        },
    }


def _busca_card() -> dict:
    return {
        "cardId": "busca",
        "card": {
            "header": _base_header("Busca de precedentes"),
            "sections": [
                {
                    "widgets": [
                        {
                            "textInput": {
                                "name": "tema_busca",
                                "label": "Empresa ou tema",
                                "type": "SINGLE_LINE",
                                "hintText": "Ex: Magazine Luiza, terceirizacao, acidente de trabalho",
                            }
                        },
                        {
                            "buttonList": {
                                "buttons": [
                                    _primary_button("Favoraveis", "buscar_favoraveis"),
                                    _primary_button("Desfavoraveis", "buscar_desfavoraveis"),
                                ]
                            }
                        },
                    ]
                }
            ],
        },
    }


def _ajuda_card() -> dict:
    return {
        "cardId": "ajuda",
        "card": {
            "header": _base_header("Como usar o Mia Falaw Bot"),
            "sections": [
                {
                    "widgets": [
                        {
                            "textParagraph": {
                                "text": (
                                    "<b>Enviar decisao:</b> Clique no botao, preencha o modal com o texto da decisao e envie para analise.\n\n"
                                    "<b>Busca de precedentes:</b> Clique no botao, informe a empresa ou tema e escolha Favoraveis ou Desfavoraveis.\n\n"
                                    "<b>Corrigir analise:</b> Apos uma analise, marque <b>@Mia Falaw Bot</b> e escreva a instrucao de correcao (ex: <i>resultado deve ser Desfavoravel</i>).\n\n"
                                    "<b>Confirmar / Cancelar:</b> Apos a analise, use os botoes para confirmar e salvar ou cancelar.\n\n"
                                    "<b>E-mail:</b> Ao confirmar, escolha se deseja gerar sugestao de e-mail para o cliente."
                                )
                            }
                        },
                        {
                            "buttonList": {
                                "buttons": [_primary_button("Voltar ao menu", "open_home")]
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
        "OL", "Nuvem", "Terceirizacao", "Subsidiaria",
        "Ex Funcionario", "Ex-Foodlovers", "Marketplace",
    ]
    body = {
        "sections": [
            {
                "header": "Nova decisao",
                "widgets": [
                    {"textInput": {"name": "cliente", "label": "Cliente (opcional)", "type": "SINGLE_LINE"}},
                    {
                        "selectionInput": {
                            "name": "tipo_responsabilidade",
                            "label": "Tipo de Responsabilidade",
                            "type": "DROPDOWN",
                            "items": [{"text": t, "value": t} for t in tipos],
                        }
                    },
                    {"textInput": {"name": "decisao", "label": "Decisao (cole o texto ou link do documento)", "type": "MULTIPLE_LINE"}},
                    {"buttonList": {"buttons": [_primary_button("Enviar decisao", "submit_decision_dialog")]}},
                ],
            }
        ]
    }
    return _dialog_action_response(body)


def _text_response(text: str) -> dict:
    return {
        "hostAppDataAction": {
            "chatDataAction": {
                "createMessageAction": {
                    "message": {"text": text}
                }
            }
        }
    }


def _cards_response(cards: list[dict]) -> dict:
    return {
        "hostAppDataAction": {
            "chatDataAction": {
                "createMessageAction": {
                    "message": {"cardsV2": cards}
                }
            }
        }
    }


def _update_cards_response(cards: list[dict]) -> dict:
    """Usado em CARD_CLICKED para atualizar card existente."""
    return {
        "hostAppDataAction": {
            "chatDataAction": {
                "updateMessageAction": {
                    "message": {"cardsV2": cards}
                }
            }
        }
    }


def _update_text_response(text: str) -> dict:
    """Usado em CARD_CLICKED para responder com texto."""
    return {
        "hostAppDataAction": {
            "chatDataAction": {
                "createMessageAction": {
                    "message": {"text": text}
                }
            }
        }
    }


def _dialog_action_response(dialog_body: dict) -> dict:
    """Abre um dialog em resposta a CARD_CLICKED."""
    return {
        "hostAppDataAction": {
            "chatDataAction": {
                "openDialogAction": {
                    "actionStatus": {"statusCode": "OK"},
                    "dialog": {"body": dialog_body},
                }
            }
        }
    }


async def _handle_message(event: dict) -> dict:
    advogado = _user_name(event)
    texto = _message_text(event).lower()

    if await esta_aguardando_correcao(advogado):
        if not texto:
            return _text_response("Informe no chat a instrucao de correcao.")
        ok, msg = await corrigir_sessao_data(advogado, texto)
        if not ok:
            return _text_response(msg)
        return _cards_response([_analysis_actions_card(msg)])

    import re
    match_fav = re.search(r'favor[aá]veis?\s+(.+)', texto)
    match_des = re.search(r'desfavor[aá]veis?\s+(.+)', texto)
    if match_fav or match_des:
        tipo = "favoravel" if match_fav else "desfavoravel"
        tema = (match_fav or match_des).group(1).strip()
        try:
            resultado = await processar_busca(tipo, tema)
            return _cards_response([_card_with_buttons(f"Precedentes {tipo}", resultado, [], "busca_resultado")])
        except Exception as exc:
            logger.exception("[/chat] erro busca mencao: %s", exc)
            return _text_response("Erro ao buscar precedentes.")

    return _cards_response([_home_card()])


async def _handle_card_click(event: dict) -> dict:
    advogado = _user_name(event)
    function_name = _action_function(event)
    return await _handle_card_click_new(advogado, function_name, event)


async def _handle_card_click_new(advogado: str, function_name: str, event: dict) -> dict:
    if function_name == "open_home":
        return _update_cards_response([_home_card()])

    if function_name == "open_ajuda":
        return _update_cards_response([_ajuda_card()])

    if function_name == "open_busca_card":
        return _update_cards_response([_busca_card()])

    if function_name in ("buscar_favoraveis", "buscar_desfavoraveis"):
        tema = _form_value(event, "tema_busca")
        if not tema:
            return _update_text_response("Informe a empresa ou tema no campo de busca.")
        tipo = "favoravel" if function_name == "buscar_favoraveis" else "desfavoravel"
        try:
            resultado = await processar_busca(tipo, tema)
            return _update_cards_response([
                _card_with_buttons(
                    f"Precedentes {tipo}", resultado,
                    [_primary_button("Nova busca", "open_busca_card"), _primary_button("Menu", "open_home")],
                    "busca_resultado",
                )
            ])
        except Exception as exc:
            logger.exception("[/chat] erro busca: %s", exc)
            return _update_text_response("Erro ao buscar precedentes.")

    if function_name == "open_decision_dialog":
        return _dialog_response()

    if function_name == "submit_decision_dialog":
        cliente = _form_value(event, "cliente")
        tipo = _form_value(event, "tipo_responsabilidade")
        decisao = _form_value(event, "decisao")
        if not decisao:
            return _update_text_response("Informe o conteudo da decisao no campo do modal.")
        resultado = await processar_texto_chat(
            texto_pdf=decisao,
            advogado=advogado,
            cliente=cliente,
            tipo_responsabilidade=tipo,
        )
        return _update_cards_response([_analysis_actions_card(resultado)])

    if function_name == "confirm_decision":
        mensagem, oferecer_email = await confirmar_sessao_data(advogado)
        cards = [_card_with_buttons("Confirmacao", mensagem, [], "confirmation")]
        if oferecer_email:
            cards.append(_email_choice_card())
        return _update_cards_response(cards)

    if function_name == "cancel_decision":
        return _update_text_response(await cancelar_sessao_data(advogado))

    if function_name == "request_correction":
        return _update_text_response(await marcar_aguardando_correcao(advogado))

    if function_name == "email_yes":
        return _update_text_response(await gerar_email_sessao_data(advogado))

    if function_name == "email_no":
        return _update_text_response(await dispensar_email_sessao_data(advogado))

    return _update_text_response("Acao nao reconhecida.")


@app.post("/chat")
async def chat_event(event: dict):
    chat_data = event.get("chat") or {}
    message_payload = chat_data.get("messagePayload") or {}
    logger.info("[/chat] keys=%s payload_keys=%s", list(event.keys()), list(message_payload.keys()))

    # Novo formato: dados em event['chat']['messagePayload']
    if chat_data and message_payload:
        user_info = chat_data.get("user") or {}
        advogado = user_info.get("displayName") or "Advogado"

        # MESSAGE
        message = message_payload.get("message") or {}
        if message:
            texto = (message.get("argumentText") or message.get("text") or "").strip()
            logger.info("[/chat] MESSAGE user=%s texto=%r", advogado, texto[:100])
            try:
                return await _handle_message({"user": user_info, "message": message})
            except Exception as exc:
                logger.exception("[/chat] erro MESSAGE: %s", exc)
                return _cards_response([_home_card()])

        # CARD_CLICKED
        button_payload = message_payload.get("buttonClickedPayload") or {}
        if button_payload:
            logger.info("[/chat] CARD_CLICKED payload: %s", str(button_payload)[:1000])
            action = button_payload.get("action") or {}
            function_name = action.get("function") or action.get("actionMethodName") or ""
            params = action.get("parameters") or []
            raw_form = button_payload.get("formInputs") or {}
            compat_form = {
                k: {"stringInputs": {"value": v.get("stringInputs", {}).get("value", [])}}
                for k, v in raw_form.items()
            }
            for p in params:
                if "key" in p:
                    compat_form[p["key"]] = {"stringInputs": {"value": [p["value"]]}}
            logger.info("[/chat] CARD_CLICKED user=%s function=%s form=%s", advogado, function_name, list(compat_form.keys()))
            compat_event = {"user": user_info, "common": {"invokedFunction": function_name, "formInputs": compat_form}}
            try:
                return await _handle_card_click_new(advogado, function_name, compat_event)
            except Exception as exc:
                logger.exception("[/chat] erro CARD_CLICKED: %s", exc)
                return _text_response("Ocorreu um erro. Tente novamente.")

        logger.warning("[/chat] messagePayload desconhecido: keys=%s full=%s", list(message_payload.keys()), str(message_payload)[:1000])
        return _cards_response([_home_card()])

    # Evento de autorizacao / verificacao do endpoint
    if "authorizationEventObject" in event:
        return {}

    # Formato legado com type no nivel raiz
    event_type = event.get("type", "")
    logger.info("[/chat] legado tipo=%s", event_type)
    if event_type == "ADDED_TO_SPACE":
        return _cards_response([_home_card()])
    if event_type == "MESSAGE":
        try:
            return await _handle_message(event)
        except Exception as exc:
            logger.exception("[/chat] erro _handle_message: %s", exc)
            return _cards_response([_home_card()])
    if event_type == "CARD_CLICKED":
        try:
            return await _handle_card_click(event)
        except Exception as exc:
            logger.exception("[/chat] erro _handle_card_click: %s", exc)
            return _text_response("Ocorreu um erro. Tente novamente.")

    logger.warning("[/chat] evento nao tratado: %s", event_type)
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
