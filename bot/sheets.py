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

    cliente = row.get("CLIENTE", "Geral")
    try:
        ws_cli = sh.worksheet(cliente)
    except gspread.WorksheetNotFound:
        ws_cli = sh.add_worksheet(title=cliente, rows=1000, cols=len(COLUNAS))
        ws_cli.append_row(COLUNAS, value_input_option="USER_ENTERED")
        logger.info("Aba '%s' criada.", cliente)
    ws_cli.append_row(valores, value_input_option="USER_ENTERED")
    logger.info("Salvo na aba '%s'.", cliente)


def _buscar_sync() -> list[dict]:
    sh = _client().open_by_key(SPREADSHEET_ID)
    return sh.worksheet("Precedentes").get_all_records()


async def salvar_decisao(row: dict):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _salvar_sync, row)


async def buscar_precedentes() -> list[dict]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _buscar_sync)
