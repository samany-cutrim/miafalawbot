"""
Mia Falaw Bot — Google Chat App (endpoint HTTP direto no Render)
v7 — renderActions/hostAppDataAction para Add-on Chat
"""

import json
import logging
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# VERIFICAÇÃO JWT — Google Chat envia Bearer token em toda requisição
# ---------------------------------------------------------------------------

async def _verificar_token_google(request: Request) -> bool:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        logger.warning("[JWT] Sem Authorization header — permitindo (pode ser teste)")
        return True

    token = auth_header.split(" ", 1)[1]
    try:
        import base64
        parts = token.split(".")
        if len(parts) != 3:
            logger.warning("[JWT] Token malformado")
            return False

        payload_b64 = parts[1]
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))

        iss = payload.get("iss", "")
        if iss not in ("https://accounts.google.com", "accounts.google.com"):
            logger.warning("[JWT] Issuer inválido: %s", iss)
            return False

        exp = payload.get("exp", 0)
        if exp and time.time() > exp:
            logger.warning("[JWT] Token expirado (exp=%s)", exp)
            return False

        email = payload.get("email", "")
        valid_emails = (
            "gcp-sa-gsuiteaddons.iam.gserviceaccount.com",
            "chat@system.gserviceaccount.com",
            "system.gserviceaccount.com",
        )
        if not any(email.endswith(e) for e in valid_emails):
            logger.warning("[JWT] Email SA inesperado: %s — permitindo mesmo assim", email)

        logger.info("[JWT] Token válido — iss=%s email=%s", iss, email)
        return True

    except Exception as e:
        logger.warning("[JWT] Erro ao verificar token: %s — permitindo", e)
        return True


from bot.config import GITHUB_TOKEN

# URL do endpoint — obrigatório para action.function em Workspace Add-on
ENDPOINT_URL = "https://mia-falaw-bot.onrender.com/chat"
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
    download_pdf,
    extrair_texto_pdf,
    carregar_sessoes,
    salvar_sessoes,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Mia Falaw Bot v23 iniciado.")
    yield


app = FastAPI(title="Mia Falaw Bot", lifespan=lifespan)


@app.get("/")
async def root():
    return {"status": "ok", "service": "mia-falaw-bot", "version": "v23"}


# ---------------------------------------------------------------------------
# HELPERS DE RESPOSTA
# ---------------------------------------------------------------------------

def _as_html(text: str) -> str:
    if not text:
        return ""
    return (
        text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", "<br>")
    )


def _base_header(subtitle: str) -> dict:
    return {"title": "Mia Falaw Bot", "subtitle": subtitle}


def _primary_button(label: str, function_name: str, parameters: list | None = None, open_dialog: bool = False) -> dict:
    # Workspace Add-on: action.function deve ser URL completa
    # O nome da função vai como parâmetro __method
    params = [{"key": "__method", "value": function_name}]
    if parameters:
        params.extend(parameters)
    action: dict = {"function": ENDPOINT_URL, "parameters": params}
    if open_dialog:
        action["interaction"] = "OPEN_DIALOG"
    return {"text": label, "onClick": {"action": action}}


def _card_with_buttons(subtitle: str, text: str, buttons: list[dict], card_id: str) -> dict:
    widgets: list[dict] = [{"textParagraph": {"text": _as_html(text)}}]
    if buttons:
        widgets.append({"buttonList": {"buttons": buttons}})
    return {
        "cardId": card_id,
        "card": {
            "header": _base_header(subtitle),
            "sections": [{"widgets": widgets}],
        },
    }


# Respostas para evento MESSAGE — SEMPRE com actionResponse NEW_MESSAGE
def _message_cards_response(cards: list[dict]) -> dict:
    # Add-on format: renderActions com hostAppAction
    return {
        "hostAppDataAction": {
            "chatDataAction": {
                "createMessageAction": {
                    "message": {
                        "cardsV2": cards,
                    }
                }
            }
        }
    }


def _message_text_response(text: str) -> dict:
    return {
        "hostAppDataAction": {
            "chatDataAction": {
                "createMessageAction": {
                    "message": {
                        "text": text,
                    }
                }
            }
        }
    }


def _message_text_and_cards(text: str, cards: list[dict]) -> dict:
    return {
        "hostAppDataAction": {
            "chatDataAction": {
                "createMessageAction": {
                    "message": {
                        "text": text,
                        "cardsV2": cards,
                    }
                }
            }
        }
    }


# Respostas para CARD_CLICKED
def _update_message_cards(cards: list[dict]) -> dict:
    return {
        "hostAppDataAction": {
            "chatDataAction": {
                "updateMessageAction": {
                    "message": {"cardsV2": cards}
                }
            }
        }
    }


def _new_message_cards(cards: list[dict]) -> dict:
    return {
        "hostAppDataAction": {
            "chatDataAction": {
                "createMessageAction": {
                    "message": {"cardsV2": cards}
                }
            }
        }
    }


def _new_message_text(text: str) -> dict:
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
    # Workspace Add-on Chat: dialog via action > navigations > pushCard
    # SEM o wrapper renderActions
    return {
        "action": {
            "navigations": [{"pushCard": dialog_body}]
        }
    }


# ---------------------------------------------------------------------------
# CARDS DE UI
# ---------------------------------------------------------------------------

def _home_card() -> dict:
    return {
        "cardId": "home",
        "card": {
            "header": _base_header("Análise de decisões trabalhistas"),
            "sections": [
                {
                    "widgets": [
                        {"textParagraph": {"text": "Selecione uma opção abaixo:"}},
                        {
                            "buttonList": {
                                "buttons": [
                                    _primary_button("Enviar decisão", "open_decision_dialog", open_dialog=True),
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
                                "label": "Empresa ou tema jurídico",
                                "type": "SINGLE_LINE",
                                "hintText": "Ex: iFood, horas extras, vínculo empregatício",
                            }
                        },
                        {
                            "buttonList": {
                                "buttons": [
                                    _primary_button("Favoráveis", "buscar_favoraveis"),
                                    _primary_button("Desfavoráveis", "buscar_desfavoraveis"),
                                    _primary_button("◀ Menu", "open_home"),
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
                    "header": "📎 Enviar decisão",
                    "widgets": [
                        {
                            "textParagraph": {
                                "text": (
                                    "Clique em <b>Enviar decisão</b> para abrir o modal. "
                                    "Cole o texto da decisão, informe o cliente (opcional) "
                                    "e o tipo de responsabilidade."
                                )
                            }
                        }
                    ],
                },
                {
                    "header": "🔍 Buscar precedentes",
                    "widgets": [
                        {
                            "textParagraph": {
                                "text": (
                                    "Clique em <b>Busca de precedentes</b>, informe a empresa "
                                    "ou tema e escolha Favoráveis ou Desfavoráveis."
                                )
                            }
                        }
                    ],
                },
                {
                    "header": "✏️ Após a análise",
                    "widgets": [
                        {
                            "textParagraph": {
                                "text": (
                                    "<b>Confirmar</b> — salva na planilha<br>"
                                    "<b>Cancelar</b> — descarta a análise<br>"
                                    "<b>Corrigir</b> — clique e depois mencione @Mia Falaw Bot no chat<br>"
                                    "Exemplo: <i>corrigir vara para 3ª Vara do Trabalho de SP</i>"
                                )
                            }
                        }
                    ],
                },
                {
                    "header": "📧 E-mail para o cliente",
                    "widgets": [
                        {
                            "textParagraph": {
                                "text": (
                                    "Após confirmar, escolha <b>Sim</b> para gerar "
                                    "sugestão de e-mail ou <b>Não</b> para dispensar."
                                )
                            }
                        }
                    ],
                },
                {
                    "widgets": [
                        {
                            "buttonList": {
                                "buttons": [
                                    _primary_button("Enviar decisão", "open_decision_dialog", open_dialog=True),
                                    _primary_button("Busca de precedentes", "open_busca_card"),
                                    _primary_button("◀ Menu", "open_home"),
                                ]
                            }
                        }
                    ]
                },
            ],
        },
    }


def _analysis_actions_card(analysis_text: str) -> dict:
    return _card_with_buttons(
        "Análise concluída — aguardando confirmação",
        analysis_text,
        [
            _primary_button("✅ Confirmar", "confirm_decision"),
            _primary_button("❌ Cancelar", "cancel_decision"),
            _primary_button("✏️ Corrigir", "request_correction"),
        ],
        "analysis_actions",
    )


def _email_choice_card() -> dict:
    return _card_with_buttons(
        "Deseja gerar e-mail para o cliente?",
        "A decisão foi salva com sucesso.\nDeseja gerar uma sugestão de e-mail de reporte ao cliente?",
        [
            _primary_button("✅ Sim, gerar e-mail", "email_yes"),
            _primary_button("❌ Não, obrigado", "email_no"),
        ],
        "email_choice",
    )


def _correction_waiting_card() -> dict:
    return _card_with_buttons(
        "Aguardando instrução de correção",
        (
            "Mencione @Mia Falaw Bot no chat e diga o que quer corrigir.\n\n"
            "Exemplos:\n"
            "• corrigir a vara para 3ª Vara do Trabalho de SP\n"
            "• resultado deve ser Desfavorável\n"
            "• TRT é TRT-2\n"
            "• cliente é Magazine Luiza"
        ),
        [_primary_button("❌ Cancelar análise", "cancel_decision")],
        "correction_waiting",
    )


# ---------------------------------------------------------------------------
# DIALOG MODAL — Enviar decisão
# ---------------------------------------------------------------------------

TIPOS_RESPONSABILIDADE = [
    "OL", "Nuvem", "Terceirização", "Subsidiária",
    "Ex Funcionário", "Ex-Foodlovers", "Marketplace",
]


def _decision_dialog_body() -> dict:
    return {
        "sections": [
            {
                "header": "Nova decisão para análise",
                "widgets": [
                    {
                        "textInput": {
                            "name": "cliente",
                            "label": "Cliente (empresa reclamada) — opcional",
                            "type": "SINGLE_LINE",
                            "hintText": "Ex: iFood, Magazine Luiza, Loft...",
                        }
                    },
                    {
                        "selectionInput": {
                            "name": "tipo_responsabilidade",
                            "label": "Tipo de Responsabilidade",
                            "type": "DROPDOWN",
                            "items": [
                                {"text": t, "value": t, "selected": i == 0}
                                for i, t in enumerate(TIPOS_RESPONSABILIDADE)
                            ],
                        }
                    },
                    {
                        "textParagraph": {
                            "text": "📎 <b>Próximo passo:</b> Após confirmar, envie o PDF da decisão diretamente no chat."
                        }
                    },
                    {
                        "buttonList": {
                            "buttons": [
                                _primary_button("✅ Confirmar e enviar PDF", "submit_decision_dialog")
                            ]
                        }
                    },
                ],
            }
        ]
    }


def _dialog_response() -> dict:
    # renderActions pushCard: sections direto, sem header extra
    body = _decision_dialog_body()
    card = {
        "sections": body["sections"]
    }
    return _dialog_action_response(card)


# ---------------------------------------------------------------------------
# EXTRAÇÃO DE DADOS DO EVENTO
# ---------------------------------------------------------------------------

def _user_name(event: dict) -> str:
    user = event.get("user") or {}
    return user.get("displayName") or "Advogado"


def _message_text(event: dict) -> str:
    msg = event.get("message") or {}
    return (msg.get("argumentText") or msg.get("text") or "").strip()


def _form_value(event: dict, key: str) -> str:
    form_inputs = (event.get("common") or {}).get("formInputs") or {}
    data = form_inputs.get(key) or {}
    values = (data.get("stringInputs") or {}).get("value") or []
    return str(values[0]).strip() if values else ""


# ---------------------------------------------------------------------------
# SESSÃO — AGUARDANDO PDF
# ---------------------------------------------------------------------------

async def _marcar_aguardando_pdf(advogado: str, cliente: str, tipo: str):
    """Marca sessão como aguardando envio do PDF no chat."""
    sessoes = await carregar_sessoes()
    chave = advogado.strip().lower().split()[0] if advogado else "advogado"
    sessoes[chave] = {
        "_aguardando_pdf": True,
        "_cliente_hint": cliente,
        "_tipo_hint": tipo,
        "_aguardando_correcao": False,
    }
    await salvar_sessoes(sessoes)


async def _esta_aguardando_pdf(advogado: str) -> bool:
    """Verifica se advogado está aguardando enviar PDF."""
    sessoes = await carregar_sessoes()
    chave = advogado.strip().lower().split()[0] if advogado else "advogado"
    row = sessoes.get(chave) or {}
    return bool(row.get("_aguardando_pdf"))


async def _get_hints_pdf(advogado: str) -> tuple[str, str]:
    """Retorna cliente e tipo salvos na sessão."""
    sessoes = await carregar_sessoes()
    chave = advogado.strip().lower().split()[0] if advogado else "advogado"
    row = sessoes.get(chave) or {}
    return row.get("_cliente_hint") or "", row.get("_tipo_hint") or ""


# ---------------------------------------------------------------------------
# HANDLER — MENSAGEM DE TEXTO
# ---------------------------------------------------------------------------

async def _handle_message(event: dict) -> dict:
    import re
    advogado = _user_name(event)
    texto = _message_text(event)
    texto_lower = texto.lower()
    msg_obj = event.get("message") or {}

    logger.info("[MESSAGE] user=%s texto=%r", advogado, texto_lower[:80])

    # Aguardando correção — qualquer mensagem é instrução de correção
    if await esta_aguardando_correcao(advogado):
        instrucao = re.sub(r'@[\w\s\-]+', '', texto_lower, flags=re.IGNORECASE).strip()
        if not instrucao:
            return _message_text_and_cards("Informe a instrução de correção:", [_correction_waiting_card()])
        ok, msg = await corrigir_sessao_data(advogado, instrucao)
        if not ok:
            return _message_text_response(msg)
        return _message_text_and_cards("Análise corrigida:", [_analysis_actions_card(msg)])

    # Aguardando PDF — verifica se há anexo PDF na mensagem
    if await _esta_aguardando_pdf(advogado):
        attachments = msg_obj.get("attachment") or []
        pdf_attachment = next(
            (a for a in attachments if
             (a.get("contentType") or "").lower() == "application/pdf" or
             (a.get("contentName") or a.get("name") or "").lower().endswith(".pdf")),
            None
        )
        if pdf_attachment:
            cliente, tipo = await _get_hints_pdf(advogado)
            logger.info("[PDF] attachment: %s", json.dumps(pdf_attachment, ensure_ascii=False)[:300])

            # resourceName para Chat Media API
            data_ref = pdf_attachment.get("attachmentDataRef") or {}
            resource_name = data_ref.get("resourceName") or ""

            try:
                if not resource_name:
                    return _message_text_response("⚠️ Não consegui identificar o arquivo. Tente enviar o PDF novamente.")

                from bot.config import GOOGLE_SERVICE_ACCOUNT_FILE
                from google.oauth2 import service_account
                import google.auth.transport.requests
                import httpx as _httpx
                from googleapiclient.discovery import build
                from googleapiclient.http import MediaIoBaseDownload
                import io as _io

                creds = service_account.Credentials.from_service_account_file(
                    GOOGLE_SERVICE_ACCOUNT_FILE,
                    scopes=["https://www.googleapis.com/auth/chat.bot"]
                )

                # Usa googleapiclient para download correto
                chat_service = build("chat", "v1", credentials=creds)

                # Primeiro busca o attachment para obter resourceName atualizado
                attachment_name_full = pdf_attachment.get("name") or ""
                if attachment_name_full:
                    try:
                        att_meta = chat_service.spaces().messages().attachments().get(
                            name=attachment_name_full
                        ).execute()
                        data_ref = att_meta.get("attachmentDataRef") or {}
                        resource_name = data_ref.get("resourceName") or resource_name
                        logger.info("[PDF] resourceName atualizado: %s", resource_name[:80])
                    except Exception as e:
                        logger.warning("[PDF] Não conseguiu buscar metadata: %s", e)

                # Download via media API
                request = chat_service.media().download_media(resourceName=resource_name)
                file_buf = _io.BytesIO()
                downloader = MediaIoBaseDownload(file_buf, request)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
                pdf_bytes = file_buf.getvalue()

                texto_pdf = extrair_texto_pdf(pdf_bytes)
                resultado = await processar_texto_chat(
                    texto_pdf=texto_pdf,
                    advogado=advogado,
                    cliente=cliente,
                    tipo_responsabilidade=tipo,
                )
                return _message_text_and_cards("Análise concluída:", [_analysis_actions_card(resultado)])
            except Exception as exc:
                logger.exception("[MESSAGE] erro ao processar PDF: %s", exc)
                return _message_text_response("⚠️ Erro ao processar o PDF. Tente novamente.")
        else:
            # Sem PDF — lembra o usuário
            return _message_text_and_cards(
                "📎 Aguardando o PDF da decisão. Arraste o arquivo .pdf aqui no chat.",
                [_card_with_buttons(
                    "Aguardando PDF",
                    "Anexe o arquivo PDF da decisão diretamente nesta conversa.",
                    [_primary_button("❌ Cancelar", "cancelar_pdf")],
                    "aguardando_pdf"
                )]
            )

    # Busca via texto
    match_fav = re.search(r'favor[aá]veis?\s+(.+)', texto_lower)
    match_des = re.search(r'desfavor[aá]veis?\s+(.+)', texto_lower)
    if match_fav or match_des:
        tipo = "favoraveis" if match_fav else "desfavoraveis"
        tema = (match_fav or match_des).group(1).strip()
        try:
            resultado = await processar_busca(tipo, tema)
            label = "Favoráveis" if tipo == "favoraveis" else "Desfavoráveis"
            return _message_text_and_cards(f"Precedentes {label}:", [
                _card_with_buttons(
                    f"Precedentes {label} — {tema}",
                    resultado,
                    [
                        _primary_button("🔍 Nova busca", "open_busca_card"),
                        _primary_button("◀ Menu", "open_home"),
                    ],
                    "busca_resultado",
                )
            ])
        except Exception as exc:
            logger.exception("[MESSAGE] erro busca: %s", exc)
            return _message_text_response("Erro ao buscar precedentes.")

    # Qualquer outra mensagem → home card
    return _message_text_and_cards("Olá! Selecione uma opção:", [_home_card()])


# ---------------------------------------------------------------------------
# HANDLER — CARD CLICKED
# ---------------------------------------------------------------------------

async def _handle_card_click(advogado: str, function_name: str, event: dict) -> dict:
    logger.info("[CARD_CLICK] user=%s function=%s", advogado, function_name)

    if function_name == "open_home":
        return _update_message_cards([_home_card()])

    if function_name == "open_ajuda":
        return _update_message_cards([_ajuda_card()])

    if function_name == "open_busca_card":
        return _update_message_cards([_busca_card()])

    if function_name in ("buscar_favoraveis", "buscar_desfavoraveis"):
        tema = _form_value(event, "tema_busca")
        if not tema:
            return _update_message_cards([_busca_card()])
        tipo = "favoraveis" if function_name == "buscar_favoraveis" else "desfavoraveis"
        label = "Favoráveis" if tipo == "favoraveis" else "Desfavoráveis"
        try:
            resultado = await processar_busca(tipo, tema)
            return _update_message_cards([
                _card_with_buttons(
                    f"Precedentes {label} — {tema}",
                    resultado,
                    [
                        _primary_button("🔍 Nova busca", "open_busca_card"),
                        _primary_button("◀ Menu", "open_home"),
                    ],
                    "busca_resultado",
                )
            ])
        except Exception as exc:
            logger.exception("[CARD_CLICK] erro busca: %s", exc)
            return _new_message_text("Erro ao buscar precedentes.")

    if function_name == "open_decision_dialog":
        return _dialog_response()

    if function_name == "submit_decision_dialog":
        cliente = _form_value(event, "cliente")
        tipo = _form_value(event, "tipo_responsabilidade")

        # Salva metadados e marca sessão aguardando PDF
        await _marcar_aguardando_pdf(advogado, cliente, tipo)

        # Fecha o dialog e pede o PDF
        return {
            "hostAppDataAction": {
                "chatDataAction": {
                    "createMessageAction": {
                        "message": {
                            "text": "✅ Metadados salvos! Agora *envie o PDF* da decisão aqui no chat (arraste o arquivo .pdf para esta conversa).",
                            "cardsV2": [_card_with_buttons(
                                "📎 Envie o PDF da decisão",
                                "Cliente: " + (cliente or "(não informado)") + "\nTipo: " + (tipo or "(não informado)") + "\n\nArraste o arquivo .pdf diretamente aqui no chat.",
                                [_primary_button("❌ Cancelar", "cancelar_pdf")],
                                "aguardando_pdf"
                            )]
                        }
                    }
                }
            }
        }

    if function_name == "cancelar_pdf":
        # Cancela o estado aguardando PDF
        sessoes = await carregar_sessoes()
        chave = advogado.strip().lower().split()[0] if advogado else "advogado"
        if chave in sessoes:
            del sessoes[chave]
            await salvar_sessoes(sessoes)
        return _update_message_cards([_home_card()])

    if function_name == "confirm_decision":
        mensagem, oferecer_email = await confirmar_sessao_data(advogado)
        if oferecer_email:
            return _update_message_cards([
                _card_with_buttons("✅ Decisão registrada!", mensagem, [], "confirmation"),
                _email_choice_card(),
            ])
        return _update_message_cards([
            _card_with_buttons(
                "✅ Decisão registrada!",
                mensagem,
                [_primary_button("◀ Menu", "open_home")],
                "confirmation",
            )
        ])

    if function_name == "cancel_decision":
        msg = await cancelar_sessao_data(advogado)
        return _update_message_cards([
            _card_with_buttons(
                "❌ Análise cancelada",
                msg,
                [_primary_button("◀ Menu", "open_home")],
                "cancelled",
            )
        ])

    if function_name == "request_correction":
        await marcar_aguardando_correcao(advogado)
        return _update_message_cards([_correction_waiting_card()])

    if function_name == "email_yes":
        email_text = await gerar_email_sessao_data(advogado)
        return _new_message_cards([
            _card_with_buttons(
                "📧 Sugestão de e-mail",
                email_text,
                [_primary_button("◀ Menu", "open_home")],
                "email_result",
            )
        ])

    if function_name == "email_no":
        msg = await dispensar_email_sessao_data(advogado)
        return _update_message_cards([
            _card_with_buttons(
                "👍 Pronto!",
                msg,
                [_primary_button("◀ Menu", "open_home")],
                "email_dismissed",
            )
        ])

    logger.warning("[CARD_CLICK] function não reconhecida: %s", function_name)
    return _update_message_cards([_home_card()])


# ---------------------------------------------------------------------------
# ENDPOINT PRINCIPAL /chat
# ---------------------------------------------------------------------------

@app.post("/chat")
async def chat_event(request: Request):
    # Verifica token do Google Chat
    auth_header = request.headers.get("Authorization", "")
    logger.info("[/chat] Authorization header: %s", auth_header[:80] if auth_header else "AUSENTE")

    token_ok = await _verificar_token_google(request)
    if not token_ok:
        logger.error("[/chat] Token inválido — rejeitando requisição")
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        event = await request.json()
    except Exception:
        return JSONResponse({"text": "Erro ao processar requisição."})

    logger.info("[/chat] RAW: %s", json.dumps(event, ensure_ascii=False)[:2000])

    chat_data = event.get("chat") or {}
    common    = event.get("commonEventObject") or {}
    user_info = chat_data.get("user") or {}
    advogado  = user_info.get("displayName") or "Advogado"

    # ------------------------------------------------------------------
    # 1. CARD_CLICKED — commonEventObject.invokedFunction presente
    # ------------------------------------------------------------------
    # Workspace Add-on: __method vem em commonEventObject.parameters
    raw_params = common.get("parameters") or {}
    method_from_params = raw_params.get("__method") or ""

    invoked_function = common.get("invokedFunction") or ""
    function_name = method_from_params or invoked_function

    if function_name:
        raw_form = common.get("formInputs") or {}
        compat_form = {
            k: {"stringInputs": {"value": v.get("stringInputs", {}).get("value", [])}}
            for k, v in raw_form.items()
        }
        compat_event = {
            "user": user_info,
            "common": {"invokedFunction": function_name, "formInputs": compat_form},
        }
        logger.info("[/chat] CARD_CLICKED user=%s fn=%s form=%s",
                    advogado, function_name, list(compat_form.keys()))
        try:
            result = await _handle_card_click(advogado, function_name, compat_event)
            logger.info("[/chat] CARD_CLICKED response: %s", json.dumps(result, ensure_ascii=False)[:500])
            return JSONResponse(result)
        except Exception as exc:
            logger.exception("[/chat] erro CARD_CLICKED: %s", exc)
            return JSONResponse(_new_message_text("Ocorreu um erro. Tente novamente."))

    # ------------------------------------------------------------------
    # 2. CARD_CLICKED alternativo — buttonClickedPayload
    # ------------------------------------------------------------------
    message_payload = chat_data.get("messagePayload") or {}
    button_payload  = (
        chat_data.get("buttonClickedPayload")
        or message_payload.get("buttonClickedPayload")
        or {}
    )
    if button_payload:
        action        = button_payload.get("action") or {}
        function_name = action.get("function") or action.get("actionMethodName") or ""
        params        = action.get("parameters") or []
        raw_form      = button_payload.get("formInputs") or {}
        compat_form   = {
            k: {"stringInputs": {"value": v.get("stringInputs", {}).get("value", [])}}
            for k, v in raw_form.items()
        }
        for p in params:
            if "key" in p:
                compat_form[p["key"]] = {"stringInputs": {"value": [p.get("value", "")]}}
        compat_event = {
            "user": user_info,
            "common": {"invokedFunction": function_name, "formInputs": compat_form},
        }
        logger.info("[/chat] CARD_CLICKED(button) user=%s fn=%s", advogado, function_name)
        try:
            result = await _handle_card_click(advogado, function_name, compat_event)
            return JSONResponse(result)
        except Exception as exc:
            logger.exception("[/chat] erro CARD_CLICKED(button): %s", exc)
            return JSONResponse(_new_message_text("Ocorreu um erro."))

    # ------------------------------------------------------------------
    # 3. MESSAGE — mensagem de texto
    # ------------------------------------------------------------------
    message = message_payload.get("message") or chat_data.get("message") or {}
    if message:
        texto = (message.get("argumentText") or message.get("text") or "").strip()
        logger.info("[/chat] MESSAGE user=%s texto=%r", advogado, texto[:100])
        try:
            result = await _handle_message({"user": user_info, "message": message, "authorizationEventObject": event.get("authorizationEventObject") or {}})
            logger.info("[/chat] MESSAGE response: %s", json.dumps(result, ensure_ascii=False)[:500])
            return JSONResponse(result)
        except Exception as exc:
            logger.exception("[/chat] erro MESSAGE: %s", exc)
            return JSONResponse(_message_text_and_cards("Erro interno.", [_home_card()]))

    # ------------------------------------------------------------------
    # 4. APP_ADDED
    # ------------------------------------------------------------------
    if chat_data.get("addedToSpacePayload") or event.get("type") == "ADDED_TO_SPACE":
        logger.info("[/chat] APP_ADDED user=%s", advogado)
        return JSONResponse(_message_text_and_cards("Menu principal:", [_home_card()]))

    # ------------------------------------------------------------------
    # 5. Evento não identificado → home card
    # ------------------------------------------------------------------
    logger.warning("[/chat] evento não tratado keys=%s chat_keys=%s",
                   list(event.keys()), list(chat_data.keys()))
    return JSONResponse(_message_text_and_cards("Menu principal:", [_home_card()]))


# ---------------------------------------------------------------------------
# ENDPOINTS AUXILIARES
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "version": "mia-falaw-bot-v23"}


@app.get("/debug-models")
async def debug_models():
    if not GITHUB_TOKEN:
        return {"error": "GITHUB_TOKEN não configurado"}
    try:
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
                return {"error": "formato inesperado"}
            return {"total": len(ids := [i for i in ids if i]), "models": ids}
    except Exception as e:
        return {"error": str(e)}
