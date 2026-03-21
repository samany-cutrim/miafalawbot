"""
Google Sheets via gspread.
Carrega credenciais diretamente do JSON em memória.
"""
import asyncio
import json
import logging
import os

import gspread
from google.oauth2.service_account import Credentials

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
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        sa_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "/secrets/service-account.json")
        creds = Credentials.from_service_account_file(sa_file, scopes=SCOPES)
    return gspread.authorize(creds)


def _salvar_sync(row: dict):
    sh = _client().open_by_key(SPREADSHEET_ID)
    valores = [row.get(col, "") for col in COLUNAS]

    ws = sh.worksheet("Precedentes")
    ws.append_row(valores, value_input_option="USER_ENTERED")
    logger.info("Salvo em Precedentes: %s", row.get("NÚMERO DO PROCESSO"))


def _buscar_sync() -> list[dict]:
    sh = _client().open_by_key(SPREADSHEET_ID)
    return sh.worksheet("Precedentes").get_all_records()


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
            ws.update(rows, value_input_option="RAW")
        logger.info("Sessões salvas no Sheets: %d", len(sessoes))
    except Exception as e:
        logger.warning("Erro ao salvar sessões no Sheets: %s", e)


async def carregar_sessoes() -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _carregar_sessoes_sync)


async def salvar_sessoes(sessoes: dict):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _salvar_sessoes_sync, sessoes)
