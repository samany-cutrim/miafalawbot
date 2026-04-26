"""
Google Sheets via gspread.
Carrega credenciais diretamente do JSON em memória.
"""
import asyncio
import json
import logging
import os

import gspread

from bot.config import SPREADSHEET_ID, COLUNAS

logger = logging.getLogger(__name__)

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


def _salvar_sync(row: dict):
    sh = _client().open_by_key(SPREADSHEET_ID)
    valores = [row.get(col, "") for col in COLUNAS]

    ws = _aba_precedentes(sh)
    ws.append_row(valores, value_input_option="USER_ENTERED")
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

SESSOES_ABA = "_sessoes_pendentes"


def _carregar_sessoes_sync() -> dict:
    try:
        sh = _client().open_by_key(SPREADSHEET_ID)
        try:
            ws = sh.worksheet(SESSOES_ABA)
        except gspread.WorksheetNotFound:
            return {}
        dados = ws.get_all_values()
        if len(dados) < 2:
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
        return sessoes
    except Exception as e:
        logger.warning("Erro ao carregar sessões do Sheets: %s", e)
        return {}


def _salvar_sessoes_sync(sessoes: dict):
    try:
        sh = _client().open_by_key(SPREADSHEET_ID)
        try:
            ws = sh.worksheet(SESSOES_ABA)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=SESSOES_ABA, rows=100, cols=2)

        # Reconstrói a aba inteira
        rows = [["chave", "dados"]]
        for chave, dados in sessoes.items():
            rows.append([chave, json.dumps(dados, ensure_ascii=False)])

        ws.clear()
        if rows:
            ws.update("A1", rows)
        logger.info("Sessões salvas no Sheets: %d", len(sessoes))
    except Exception as e:
        logger.warning("Erro ao salvar sessões no Sheets: %s", e)


async def carregar_sessoes() -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _carregar_sessoes_sync)


async def salvar_sessoes(sessoes: dict):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _salvar_sessoes_sync, sessoes)
