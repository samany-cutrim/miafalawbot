"""
Mia Falaw Bot — Google Chat App (endpoint HTTP direto no Render)
"""

import json
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from bot.config import GITHUB_TOKEN
from bot.handlers import (
    cancelar_sessao_data,
    confirmar_sessao_data,
    corrigir_sessao_data,
    dispensar_email_sessao_data,
    esta_aguardando_correcao,
    gerar_email_sessao_data,
    marcar_aguardando_correcao,
    processar_busca,
    processar_texto_chat,
)

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
# ENDPOINT /chat — Google Chat App
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
    return {"text": text}


def _cards_response(cards: list[dict]) -> dict:
    return {"cardsV2": cards}


def _update_cards_response(cards: list[dict]) -> dict:
    """Usado em CARD_CLICKED para atualizar card existente."""
    return {
        "actionResponse": {"type": "UPDATE_MESSAGE"},
        "cardsV2": cards,
    }


def _update_text_response(text: str) -> dict:
    """Usado em CARD_CLICKED para responder com texto."""
    return {
        "actionResponse": {"type": "NEW_MESSAGE"},
        "text": text,
    }


def _dialog_action_response(dialog_body: dict) -> dict:
    """Abre um dialog em resposta a CARD_CLICKED."""
    return {
        "actionResponse": {
            "type": "DIALOG",
            "dialogAction": {
                "actionStatus": {"statusCode": "OK"},
                "dialog": {"body": dialog_body},
            },
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
    logger.info("[/chat] RAW: %s", json.dumps(event, ensure_ascii=False)[:3000])

    chat_data = event.get("chat") or {}
    common = event.get("commonEventObject") or {}
    message_payload = chat_data.get("messagePayload") or {}

    # CARD_CLICKED: function em commonEventObject.invokedFunction
    invoked_function = common.get("invokedFunction") or ""
    if invoked_function:
        user_info = chat_data.get("user") or {}
        advogado = user_info.get("displayName") or "Advogado"
        # formInputs fica em commonEventObject.formInputs
        raw_form = common.get("formInputs") or {}
        compat_form = {
            k: {"stringInputs": {"value": v.get("stringInputs", {}).get("value", [])}}
            for k, v in raw_form.items()
        }
        compat_event = {"user": user_info, "common": {"invokedFunction": invoked_function, "formInputs": compat_form}}
        logger.info("[/chat] CARD_CLICKED user=%s function=%s form_keys=%s", advogado, invoked_function, list(compat_form.keys()))
        try:
            return await _handle_card_click_new(advogado, invoked_function, compat_event)
        except Exception as exc:
            logger.exception("[/chat] erro CARD_CLICKED: %s", exc)
            return _update_text_response("Ocorreu um erro. Tente novamente.")

    logger.info("[/chat] keys=%s payload_keys=%s chat_keys=%s", list(event.keys()), list(message_payload.keys()), list(chat_data.keys()))

    user_info = chat_data.get("user") or {}
    advogado = user_info.get("displayName") or "Advogado"

    # CARD_CLICKED: buttonClickedPayload pode estar em chat_data ou em messagePayload
    button_payload = chat_data.get("buttonClickedPayload") or message_payload.get("buttonClickedPayload") or {}
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

    # MESSAGE: em messagePayload ou chat_data direto
    message = message_payload.get("message") or chat_data.get("message") or {}
    if message:
        texto = (message.get("argumentText") or message.get("text") or "").strip()
        logger.info("[/chat] MESSAGE user=%s texto=%r", advogado, texto[:100])
        try:
            return await _handle_message({"user": user_info, "message": message})
        except Exception as exc:
            logger.exception("[/chat] erro MESSAGE: %s", exc)
            return _cards_response([_home_card()])

    logger.warning("[/chat] evento nao tratado chat_keys=%s full_chat=%s", list(chat_data.keys()), str(chat_data)[:500])
    return _cards_response([_home_card()])

    # Evento de autorizacao / verificacao do endpoint — retorna vazio
    if "authorizationEventObject" in event:
        return {}

    logger.warning("[/chat] evento nao reconhecido: %s", str(event)[:500])
    return _text_response("Evento nao suportado.")


@app.get("/health")
async def health():
    return {"status": "ok", "version": "mia-falaw-bot-v3-chatapp"}


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
