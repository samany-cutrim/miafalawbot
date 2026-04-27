"""
Google Sheets via gspread.
Carrega credenciais diretamente do JSON em memória.
"""
import asyncio
import json
import logging
import os

import gspread
from gspread.utils import rowcol_to_a1

from bot.config import SPREADSHEET_ID, COLUNAS

logger = logging.getLogger(__name__)

# Fallback em memória para não perder fluxo quando o Sheets estiver indisponível.
_SESSOES_CACHE: dict = {}

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


def _client():
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if sa_json:
        info = json.loads(sa_json)
        return gspread.service_account_from_dict(info, scopes=SCOPES)
    else:
        sa_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "/secrets/service-account.json")
        return gspread.service_account(filename=sa_file, scopes=SCOPES)


def _aba_precedentes(sh):
    """Retorna a worksheet de precedentes, tentando pelo nome e depois pela primeira aba."""
    try:
        return sh.worksheet("Precedentes")
    except gspread.WorksheetNotFound:
        pass
    # Fallback: primeira aba com qualquer nome
    logger.warning("Aba 'Precedentes' não encontrada. Usando a primeira aba da planilha.")
    return sh.get_worksheet(0)


def _worksheet_by_names(sh, names: list[str]):
    for name in names:
        try:
            return sh.worksheet(name)
        except gspread.WorksheetNotFound:
            continue
    raise gspread.WorksheetNotFound()


def _salvar_sync(row: dict):
    sh = _client().open_by_key(SPREADSHEET_ID)
    valores = []
    for col in COLUNAS:
        v = row.get(col, "")
        if isinstance(v, list):
            v = "\n".join(str(i) for i in v)
        elif v is None:
            v = ""
        valores.append(str(v) if not isinstance(v, str) else v)

    ws = _aba_precedentes(sh)
    next_row = len(ws.col_values(1)) + 1
    start = rowcol_to_a1(next_row, 1)
    end = rowcol_to_a1(next_row, len(valores))
    ws.update(f"{start}:{end}", [valores], value_input_option="USER_ENTERED")
    logger.info("Salvo em Precedentes: %s", row.get("NÚMERO DO PROCESSO"))


def _buscar_sync() -> list[dict]:
    sh = _client().open_by_key(SPREADSHEET_ID)
    return _aba_precedentes(sh).get_all_records()


async def salvar_decisao(row: dict):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _salvar_sync, row)


async def buscar_precedentes() -> list[dict]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _buscar_sync)


# ---------------------------------------------------------------------------
# SESSÕES PENDENTES — salvas numa aba oculta da planilha
# ---------------------------------------------------------------------------

SESSOES_ABAS = ["_sessoes_pendentes", "sessoes pendentes", "sessoes_pendentes"]


def _carregar_sessoes_sync() -> dict:
    global _SESSOES_CACHE
    try:
        sh = _client().open_by_key(SPREADSHEET_ID)
        try:
            ws = _worksheet_by_names(sh, SESSOES_ABAS)
        except gspread.WorksheetNotFound:
            return dict(_SESSOES_CACHE)
        dados = ws.get_all_values()
        if len(dados) < 2:
            _SESSOES_CACHE = {}
            return {}
        # Linha 1 = headers, demais = dados
        # Formato: chave | json_dados
        sessoes = {}
        for row in dados[1:]:
            if len(row) >= 2 and row[0]:
                try:
                    sessoes[row[0]] = json.loads(row[1])
                except Exception:
                    pass
        _SESSOES_CACHE = dict(sessoes)
        return sessoes
    except Exception as e:
        logger.warning("Erro ao carregar sessões do Sheets: %r", e)
        return dict(_SESSOES_CACHE)


def _salvar_sessoes_sync(sessoes: dict):
    global _SESSOES_CACHE
    try:
        sh = _client().open_by_key(SPREADSHEET_ID)
        try:
            ws = _worksheet_by_names(sh, SESSOES_ABAS)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=SESSOES_ABAS[0], rows=100, cols=2)

        # Reconstrói a aba inteira
        rows = [["chave", "dados"]]
        for chave, dados in sessoes.items():
            rows.append([chave, json.dumps(dados, ensure_ascii=False)])

        ws.clear()
        if rows:
            ws.update("A1", rows)
        _SESSOES_CACHE = dict(sessoes)
        logger.info("Sessões salvas no Sheets: %d", len(sessoes))
    except Exception as e:
        # Mantém cache local para não quebrar o fluxo do chat em caso de falha do Sheets.
        _SESSOES_CACHE = dict(sessoes)
        logger.warning("Erro ao salvar sessões no Sheets: %r", e)


async def carregar_sessoes() -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _carregar_sessoes_sync)


async def salvar_sessoes(sessoes: dict):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _salvar_sessoes_sync, sessoes)


# ---------------------------------------------------------------------------
# OAUTH TOKENS — refresh tokens dos usuários para postar cards interativos
# ---------------------------------------------------------------------------

TOKENS_ABAS = ["_oauth_tokens", "oauth tokens", "oauth_tokens"]


def _salvar_token_sync(chave: str, refresh_token: str):
    """Salva/atualiza o refresh_token do usuário (chave = primeiro nome em lower)."""
    try:
        sh = _client().open_by_key(SPREADSHEET_ID)
        try:
            ws = _worksheet_by_names(sh, TOKENS_ABAS)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=TOKENS_ABAS[0], rows=50, cols=3)
            ws.update("A1", [["chave", "refresh_token"]])

        dados = ws.get_all_values()
        for i, row in enumerate(dados[1:], 2):
            if row and row[0] == chave:
                ws.update(f"A{i}", [[chave, refresh_token]])
                logger.info("Token OAuth atualizado para %s", chave)
                return
        ws.append_row([chave, refresh_token])
        logger.info("Token OAuth salvo para %s", chave)
    except Exception as e:
        logger.error("Erro ao salvar token OAuth: %s", e)


def _carregar_token_sync(chave: str) -> str | None:
    """Retorna o refresh_token do usuário ou None se não autorizado."""
    try:
        sh = _client().open_by_key(SPREADSHEET_ID)
        ws = _worksheet_by_names(sh, TOKENS_ABAS)
        dados = ws.get_all_values()
        for row in dados[1:]:
            if row and row[0] == chave:
                return row[1] if len(row) > 1 and row[1] else None
        return None
    except gspread.WorksheetNotFound:
        return None
    except Exception as e:
        logger.warning("Erro ao carregar token OAuth: %s", e)
        return None


async def salvar_token_oauth(chave: str, refresh_token: str):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _salvar_token_sync, chave, refresh_token)


async def carregar_token_oauth(chave: str) -> str | None:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _carregar_token_sync, chave)
