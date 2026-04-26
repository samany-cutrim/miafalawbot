"""
Mia Falaw Bot — Google Chat App (Workspace Add-on, endpoint HTTP no Render)
v25 — Background task para análise de PDF (resolve timeout de 30s do Google Chat)
     Fluxo sem Forms: PDF enviado direto no chat após selecionar cliente/tipo
"""

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# JWT — Google Chat envia Bearer token em toda requisição
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


from bot.config import GITHUB_TOKEN, GOOGLE_SERVICE_ACCOUNT_FILE, WEBHOOK_URL
from bot.handlers import (
    cancelar_sessao_data,
    confirmar_sessao_data,
    corrigir_sessao_data,
    dispensar_email_sessao_data,
    esta_aguardando_correcao,
    obter_relatorio_pendente,
    extrair_texto_pdf,
    gerar_email_sessao_data,
    marcar_aguardando_correcao,
    processar_busca,
    processar_texto_chat,
    carregar_sessoes,
    salvar_sessoes,
)
from bot.webhook import send_webhook, send_card, send_interactive_card

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# URL do endpoint — obrigatório para action.function em Workspace Add-on
ENDPOINT_URL = "https://mia-falaw-bot.onrender.com/chat"


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Mia Falaw Bot v25 iniciado.")
    yield


app = FastAPI(title="Mia Falaw Bot", lifespan=lifespan)


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


def _message_text_response(text: str) -> dict:
    return {
        "hostAppDataAction": {
            "chatDataAction": {
                "createMessageAction": {
                    "message": {"text": text}
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
                                    _primary_button("📎 Enviar decisão", "open_decision_dialog", open_dialog=True),
                                    _primary_button("🔍 Buscar precedentes", "open_busca_card"),
                                    _primary_button("❓ Ajuda", "open_ajuda"),
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
                                    _primary_button("✅ Favoráveis", "buscar_favoraveis"),
                                    _primary_button("❌ Desfavoráveis", "buscar_desfavoraveis"),
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
                                    "1. Clique em <b>Enviar decisão</b><br>"
                                    "2. Preencha o cliente e tipo de responsabilidade<br>"
                                    "3. Clique em <b>Confirmar</b><br>"
                                    "4. Envie o PDF da decisão diretamente no chat<br>"
                                    "5. Aguarde a análise automática"
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
                                    "<b>Corrigir</b> — clique e fale a correção no chat"
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
                                "text": "Após confirmar, escolha <b>Sim</b> para gerar sugestão de e-mail."
                            }
                        }
                    ],
                },
                {
                    "widgets": [
                        {
                            "buttonList": {
                                "buttons": [
                                    _primary_button("📎 Enviar decisão", "open_decision_dialog", open_dialog=True),
                                    _primary_button("◀ Menu", "open_home"),
                                ]
                            }
                        }
                    ]
                },
            ],
        },
    }


def _analysis_webhook_card(advogado: str, analysis_text: str) -> dict:
    """Card com análise + botões enviado via Incoming Webhook (background task)."""
    return {
        "cardId": "analysis_actions",
        "card": {
            "header": {"title": "Mia Falaw Bot", "subtitle": f"✅ Análise concluída — aguardando confirmação"},
            "sections": [
                {
                    "widgets": [
                        {"textParagraph": {"text": _as_html(f"✅ Análise concluída, {advogado}!\n\n{analysis_text}")}},
                        {
                            "buttonList": {
                                "buttons": [
                                    _primary_button("✅ Confirmar", "confirm_decision"),
                                    _primary_button("❌ Cancelar", "cancel_decision"),
                                    _primary_button("✏️ Corrigir", "request_correction"),
                                ]
                            }
                        },
                        {
                            "textParagraph": {
                                "text": "<i>Para corrigir: clique em ✏️ Corrigir e depois mencione @Mia Falaw Bot com a instrução.</i>"
                            }
                        },
                    ]
                }
            ],
        },
    }


def _analysis_actions_card(analysis_text: str) -> dict:
    return {
        "cardId": "analysis_actions",
        "card": {
            "header": {
                "title": "Mia Falaw Bot",
                "subtitle": "✅ Análise concluída — aguardando confirmação",
            },
            "sections": [
                {
                    "header": "📋 Relatório da Decisão",
                    "collapsible": True,
                    "uncollapsibleWidgetsCount": 2,
                    "widgets": [
                        {"textParagraph": {"text": _as_html(analysis_text)}},
                    ],
                },
                {
                    "header": "O que deseja fazer?",
                    "widgets": [
                        {
                            "buttonList": {
                                "buttons": [
                                    _primary_button("✅ Confirmar e salvar", "confirm_decision"),
                                    _primary_button("✏️ Corrigir", "request_correction"),
                                    _primary_button("❌ Cancelar", "cancel_decision"),
                                ]
                            }
                        }
                    ],
                },
            ],
        },
    }


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
# DIALOG MODAL — Enviar decisão (SEM Forms, apenas cliente/tipo)
# ---------------------------------------------------------------------------

TIPOS_RESPONSABILIDADE = [
    "OL", "Nuvem", "Terceirização", "Subsidiária",
    "Ex Funcionário", "Ex-Foodlovers", "Marketplace",
]


def _decision_dialog_body() -> dict:
    return {
        "sections": [
            {
                "header": "Configurar análise de decisão",
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
                            "text": (
                                "📌 Após confirmar, envie o <b>PDF da decisão</b> "
                                "diretamente neste chat. A análise será automática."
                            )
                        }
                    },
                    {
                        "buttonList": {
                            "buttons": [
                                _primary_button("✅ Confirmar", "submit_decision_dialog"),
                            ]
                        }
                    },
                ],
            }
        ]
    }


def _dialog_response() -> dict:
    body = _decision_dialog_body()
    card = {"sections": body["sections"]}
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
    sessoes = await carregar_sessoes()
    chave = advogado.strip().lower().split()[0] if advogado else "advogado"
    row = sessoes.get(chave) or {}
    return bool(row.get("_aguardando_pdf"))


async def _get_hints_pdf(advogado: str) -> tuple[str, str]:
    sessoes = await carregar_sessoes()
    chave = advogado.strip().lower().split()[0] if advogado else "advogado"
    row = sessoes.get(chave) or {}
    return row.get("_cliente_hint") or "", row.get("_tipo_hint") or ""


# ---------------------------------------------------------------------------
# BACKGROUND TASK — Download + Análise do PDF
# Roda fora do ciclo de request/response, envia resultado via Webhook
# ---------------------------------------------------------------------------

async def _processar_pdf_background(
    resource_name: str,
    advogado: str,
    cliente: str,
    tipo: str,
    space_name: str,
    webhook_url: str,
):
    """
    Executado em background após resposta imediata ao Google Chat.
    Download do PDF → extração de texto → análise IA → envia resultado via webhook.
    """
    try:
        pdf_bytes = await _download_pdf_chat(resource_name)
        if not pdf_bytes:
            await send_webhook(
                webhook_url,
                "⚠️ Não foi possível baixar o PDF. Tente enviar novamente."
            )
            return

        texto_pdf = extrair_texto_pdf(pdf_bytes)
        if not texto_pdf or len(texto_pdf.strip()) < 50:
            await send_webhook(
                webhook_url,
                "⚠️ Não foi possível extrair texto do PDF. O arquivo pode estar escaneado."
            )
            return

        resultado = await processar_texto_chat(
            texto_pdf=texto_pdf,
            advogado=advogado,
            cliente=cliente,
            tipo_responsabilidade=tipo,
        )

        # Envia card interativo com botões via Chat REST API (Service Account)
        primeiro = advogado.strip().split()[0]
        card = _analysis_actions_card(resultado)
        enviado = await send_interactive_card(
            space_name=space_name,
            card=card,
            sa_file=GOOGLE_SERVICE_ACCOUNT_FILE,
            fallback_text=f"✅ Análise concluída, {primeiro}!\n\nMencione @Mia Falaw Bot para ver o resultado.",
        )
        if not enviado and webhook_url:
            await send_webhook(
                webhook_url,
                f"✅ *Análise concluída, {primeiro}!*\n\nMencione *@Mia Falaw Bot* no chat para ver o resultado completo. 👉"
            )

    except Exception as exc:
        logger.exception("[BG] Erro ao processar PDF em background: %s", exc)
        try:
            await send_webhook(
                webhook_url,
                f"⚠️ Erro ao analisar o PDF: {str(exc)[:200]}\nTente enviar novamente."
            )
        except Exception:
            pass


async def _download_pdf_chat(resource_name: str) -> bytes | None:
    """Baixa PDF via SA token (chat.bot). Fallback: Apps Script."""
    # Tentativa 1: Service Account com escopo chat.bot
    try:
        from google.oauth2 import service_account
        import google.auth.transport.requests as _ga_requests

        _creds = service_account.Credentials.from_service_account_file(
            GOOGLE_SERVICE_ACCOUNT_FILE,
            scopes=["https://www.googleapis.com/auth/chat.bot"]
        )
        _auth_req = _ga_requests.Request()
        _creds.refresh(_auth_req)
        _token = _creds.token

        _url = f"https://chat.googleapis.com/v1/media/{resource_name}?alt=media"
        logger.info("[PDF] Tentando download SA: %s", _url[:100])

        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as cli:
            resp = await cli.get(_url, headers={"Authorization": f"Bearer {_token}"})

        if resp.status_code == 200:
            logger.info("[PDF] Download SA OK — %d bytes", len(resp.content))
            return resp.content
        logger.warning("[PDF] SA falhou (%s) — tentando Apps Script", resp.status_code)
    except Exception as e:
        logger.warning("[PDF] Erro SA: %s — tentando Apps Script", e)

    # Tentativa 2: Apps Script como proxy
    try:
        import base64 as _b64
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as cli:
            as_resp = await cli.post(
                "https://script.google.com/macros/s/AKfycbyg7B1QiZoD_YpaGBCR5qXZEX_5bnrzKTR3WgAYjYbk_xrMyTgubdEmMqa67pZG3CBz/exec",
                json={"action": "download_pdf", "resource_name": resource_name}
            )

        logger.info("[PDF] Apps Script status: %s", as_resp.status_code)

        if as_resp.status_code == 200:
            as_data = as_resp.json()
            if as_data.get("status") == "ok" and as_data.get("pdf_base64"):
                pdf_bytes = _b64.b64decode(as_data["pdf_base64"])
                logger.info("[PDF] Apps Script OK — %d bytes", len(pdf_bytes))
                return pdf_bytes
        logger.error("[PDF] Apps Script falhou: %s", as_resp.text[:200])
    except Exception as e:
        logger.error("[PDF] Erro Apps Script: %s", e)

    return None


# ---------------------------------------------------------------------------
# SAUDAÇÃO PERSONALIZADA POR HORÁRIO (fuso Brasília)
# ---------------------------------------------------------------------------

def _saudacao_personalizada(nome: str) -> str:
    """Retorna saudação por horário + primeiro nome da pessoa."""
    from datetime import datetime as _dt, timezone, timedelta
    tz_brasilia = timezone(timedelta(hours=-3))
    hora = _dt.now(tz_brasilia).hour
    if 6 <= hora < 12:
        saudacao = "Bom dia"
    elif 12 <= hora < 18:
        saudacao = "Boa tarde"
    else:
        saudacao = "Boa noite"
    primeiro = (nome or "Advogado").strip().split()[0]
    return f"{saudacao}, {primeiro}! Selecione uma opção:"


# ---------------------------------------------------------------------------
# HANDLER — MENSAGEM DE TEXTO
# ---------------------------------------------------------------------------

async def _handle_message(event: dict, background_tasks: BackgroundTasks) -> dict:
    import re
    advogado = _user_name(event)
    texto = _message_text(event)
    texto_lower = texto.lower()
    msg_obj = event.get("message") or {}

    logger.info("[MESSAGE] user=%s texto=%r", advogado, texto_lower[:80])

    # Sessão pendente com análise pronta — mostra card de ação diretamente
    # (acontece quando o usuário menciona o bot após análise via background task)
    if not await _esta_aguardando_pdf(advogado) and not await esta_aguardando_correcao(advogado):
        relatorio_pendente = await obter_relatorio_pendente(advogado)
        if relatorio_pendente:
            primeiro = advogado.strip().split()[0]
            logger.info("[MESSAGE] Sessão pendente detectada para %s — exibindo card de análise", advogado)
            return _new_message_cards([_analysis_actions_card(relatorio_pendente)])

    # Aguardando correção
    if await esta_aguardando_correcao(advogado):
        instrucao = re.sub(r'@[\w\s\-]+', '', texto_lower, flags=re.IGNORECASE).strip()
        if not instrucao:
            return _message_text_and_cards("Informe a instrução de correção:", [_correction_waiting_card()])
        ok, msg = await corrigir_sessao_data(advogado, instrucao)
        if not ok:
            return _new_message_text(msg)
        return _new_message_cards([_analysis_actions_card(msg)])

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
            data_ref = pdf_attachment.get("attachmentDataRef") or {}
            resource_name = data_ref.get("resourceName") or ""

            logger.info("[PDF] Attachment recebido: %s", json.dumps(pdf_attachment, ensure_ascii=False)[:300])

            if not resource_name:
                return _new_message_text(
                    "⚠️ Não consegui identificar o arquivo PDF. Tente enviar novamente."
                )

            # Extrai space_name para usar a Chat REST API no background
            space_name = (msg_obj.get("space") or {}).get("name") or ""

            # Usa WEBHOOK_URL do env (variável no Render) para fallback
            webhook_url = WEBHOOK_URL
            if not space_name:
                logger.warning("[PDF] space_name não encontrado para %s", advogado)

            # Dispara análise em background — resposta imediata para o Chat
            background_tasks.add_task(
                _processar_pdf_background,
                resource_name=resource_name,
                advogado=advogado,
                cliente=cliente,
                tipo=tipo,
                space_name=space_name,
                webhook_url=webhook_url,
            )

            # Resposta imediata (<2s) — Google Chat não faz timeout
            return _new_message_text(
                f"⏳ *Analisando decisão...* {advogado}\n"
                f"Cliente: {cliente or '(detectar automaticamente)'}\n"
                f"Tipo: {tipo or 'OL'}\n\n"
                f"_A análise pode levar até 1 minuto. O resultado será enviado aqui._"
            )
        else:
            # Sem PDF — lembra o usuário
            return _message_text_and_cards(
                "📎 Envie o PDF da decisão",
                (
                    "Aguardando o PDF da decisão.\n\n"
                    "Envie o arquivo PDF diretamente neste chat ou cancele."
                ),
                [
                    _primary_button("❌ Cancelar", "cancelar_pdf"),
                ],
                "aguardando_pdf"
            )

    # Busca via texto
    match_fav = re.search(r'favor[aá]veis?\s+(.+)', texto_lower)
    match_des = re.search(r'desfavor[aá]veis?\s+(.+)', texto_lower)
    if match_fav or match_des:
        tipo_busca = "favoraveis" if match_fav else "desfavoraveis"
        tema = (match_fav or match_des).group(1).strip()
        try:
            resultado = await processar_busca(tipo_busca, tema)
            label = "Favoráveis" if tipo_busca == "favoraveis" else "Desfavoráveis"
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
            return _new_message_text("Erro ao buscar precedentes.")

    # Comandos de confirmação via texto
    if re.search(r'^/?(confirmar|confirm)', texto_lower):
        mensagem, oferecer_email = await confirmar_sessao_data(advogado)
        if oferecer_email:
            return _message_text_and_cards(
                "✅ Decisão registrada!",
                [
                    _card_with_buttons("✅ Decisão registrada!", mensagem, [], "confirmation"),
                    _email_choice_card(),
                ]
            )
        return _new_message_text(mensagem)

    if re.search(r'^/?(cancelar|cancel)', texto_lower):
        msg = await cancelar_sessao_data(advogado)
        return _new_message_text(msg)

    if re.search(r'^/?(sim|yes)', texto_lower):
        email_text = await gerar_email_sessao_data(advogado)
        return _new_message_text(email_text)

    if re.search(r'^/?(nao|não|no)', texto_lower):
        msg = await dispensar_email_sessao_data(advogado)
        return _new_message_text(msg)

    # Qualquer outra mensagem → saudação personalizada + home card
    return _message_text_and_cards(_saudacao_personalizada(advogado), [_home_card()])


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

        await _marcar_aguardando_pdf(advogado, cliente, tipo)

        # Fecha o dialog e pede o PDF direto no chat
        return {
            "hostAppDataAction": {
                "chatDataAction": {
                    "createMessageAction": {
                        "message": {
                            "text": (
                                f"✅ *{advogado}, tudo pronto!*\n\n"
                                f"📋 Cliente: {cliente or '(detectar automaticamente)'}\n"
                                f"⚖️ Tipo: {tipo or 'OL'}\n\n"
                                f"📎 *Envie o PDF da decisão agora, diretamente neste chat.*\n"
                                f"_A análise será iniciada automaticamente._"
                            ),
                        }
                    }
                }
            }
        }

    if function_name == "cancelar_pdf":
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
async def chat_event(request: Request, background_tasks: BackgroundTasks):
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
    # 1. CARD_CLICKED — parâmetro __method em commonEventObject.parameters
    # ------------------------------------------------------------------
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
        params        = action.get("parameters") or []
        raw_form      = button_payload.get("formInputs") or {}

        # Extrai __method dos parameters (padrão do bot)
        method_param = next((p.get("value") for p in params if p.get("key") == "__method"), "")
        function_name = method_param or action.get("actionMethodName") or action.get("function") or ""

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
    # 3. MESSAGE — mensagem de texto (com background_tasks para PDF)
    # ------------------------------------------------------------------
    message = message_payload.get("message") or chat_data.get("message") or {}
    if message:
        texto = (message.get("argumentText") or message.get("text") or "").strip()
        logger.info("[/chat] MESSAGE user=%s texto=%r", advogado, texto[:100])
        try:
            result = await _handle_message(
                {"user": user_info, "message": message},
                background_tasks
            )
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
        return JSONResponse(_message_text_and_cards("Olá! Selecione uma opção:", [_home_card()]))

    # ------------------------------------------------------------------
    # 5. REMOVED_FROM_SPACE
    # ------------------------------------------------------------------
    if chat_data.get("removedFromSpacePayload") or event.get("type") == "REMOVED_FROM_SPACE":
        logger.info("[/chat] REMOVED_FROM_SPACE user=%s", advogado)
        return JSONResponse({})

    # ------------------------------------------------------------------
    # 6. Evento não identificado → home card
    # ------------------------------------------------------------------
    logger.warning("[/chat] evento não tratado keys=%s chat_keys=%s",
                   list(event.keys()), list(chat_data.keys()))
    return JSONResponse(_message_text_and_cards("Menu principal:", [_home_card()]))


# ---------------------------------------------------------------------------
# ENDPOINTS AUXILIARES
# ---------------------------------------------------------------------------

@app.post("/processar-pdf-base64")
async def processar_pdf_base64(request: Request):
    """
    Recebe PDF em base64 do Apps Script, extrai texto e processa análise.
    """
    try:
        body = await request.json()
        pdf_base64 = body.get("pdf_base64", "")
        advogado = body.get("advogado", "Advogado")
        filename = body.get("filename", "decisao.pdf")

        if not pdf_base64:
            return JSONResponse({"status": "error", "erro": "pdf_base64 ausente"}, status_code=400)

        import base64 as _b64
        pdf_bytes = _b64.b64decode(pdf_base64)
        logger.info("[PDF-BASE64] Recebido: %s — %d bytes — advogado=%s", filename, len(pdf_bytes), advogado)

        texto_pdf = extrair_texto_pdf(pdf_bytes)
        if not texto_pdf or len(texto_pdf.strip()) < 50:
            return JSONResponse({"status": "error", "erro": "Nao foi possivel extrair texto do PDF."}, status_code=422)

        try:
            cliente, tipo = await _get_hints_pdf(advogado)
            tipo = tipo or "OL"
        except Exception:
            cliente = ""
            tipo = "OL"

        resultado = await processar_texto_chat(
            texto_pdf=texto_pdf,
            advogado=advogado,
            cliente=cliente,
            tipo_responsabilidade=tipo,
        )
        logger.info("[PDF-BASE64] Analise concluida para %s", advogado)
        return JSONResponse({"status": "ok", "resultado": resultado})

    except Exception as exc:
        logger.exception("[PDF-BASE64] Erro: %s", exc)
        return JSONResponse({"status": "error", "erro": str(exc)}, status_code=500)


@app.post("/processar-texto-sync")
async def processar_texto_sync(request: Request):
    """
    Endpoint chamado pelo Apps Script após extrair texto do PDF.
    """
    try:
        body = await request.json()
        texto_pdf = body.get("texto_pdf", "")
        advogado = body.get("advogado", "Advogado")
        cliente = body.get("cliente", "")
        tipo = body.get("tipo_responsabilidade", "") or body.get("tipo", "") or "OL"

        if not texto_pdf or len(texto_pdf.strip()) < 50:
            return JSONResponse({"status": "error", "erro": "Texto ausente ou muito curto"}, status_code=400)

        logger.info("[TEXTO-SYNC] Recebido — advogado=%s chars=%d", advogado, len(texto_pdf))

        try:
            cliente_hint, tipo_hint = await _get_hints_pdf(advogado)
            cliente = cliente or cliente_hint
            tipo = tipo or tipo_hint or "OL"
        except Exception:
            pass

        resultado = await processar_texto_chat(
            texto_pdf=texto_pdf,
            advogado=advogado,
            cliente=cliente,
            tipo_responsabilidade=tipo,
        )
        logger.info("[TEXTO-SYNC] Análise concluída para %s", advogado)
        return JSONResponse({"status": "ok", "resultado": resultado})

    except Exception as exc:
        logger.exception("[TEXTO-SYNC] Erro: %s", exc)
        return JSONResponse({"status": "error", "erro": str(exc)}, status_code=500)


@app.get("/")
async def root():
    return {"status": "ok", "service": "mia-falaw-bot", "version": "v25"}


@app.get("/health")
async def health():
    return {"status": "ok", "version": "mia-falaw-bot-v25"}


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
