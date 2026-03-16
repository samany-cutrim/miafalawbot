"""
Google Sheets via gspread.
Aba "Precedentes": tabela geral de todas as decisões.
Aba por cliente: criada automaticamente se não existir.
"""
import asyncio
import logging
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

from bot.config import GOOGLE_SERVICE_ACCOUNT_FILE, GOOGLE_SHEETS_SCOPES, SPREADSHEET_ID, COLUNAS

logger = logging.getLogger(__name__)


def _client():
    creds = Credentials.from_service_account_file(
        GOOGLE_SERVICE_ACCOUNT_FILE, scopes=GOOGLE_SHEETS_SCOPES
    )
    return gspread.authorize(creds)


def _salvar_sync(row: dict):
    sh = _client().open_by_key(SPREADSHEET_ID)
    valores = [row.get(col, "") for col in COLUNAS]

    # Aba geral
    ws = sh.worksheet("Precedentes")
    ws.append_row(valores, value_input_option="USER_ENTERED")
    logger.info("Salvo em Precedentes: %s", row.get("NÚMERO DO PROCESSO"))

    # Aba do cliente
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
