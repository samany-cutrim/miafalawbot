"""
Configurações via variáveis de ambiente.
"""
import os

ANTHROPIC_API_KEY        = os.environ["ANTHROPIC_API_KEY"]
SPREADSHEET_ID           = os.environ.get("SPREADSHEET_ID", "1DNb3UWPAfd3wTYXGtr_4MDy7RoYOhSxbr2_FEVvDDaY")
GOOGLE_SERVICE_ACCOUNT_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "/secrets/service-account.json")

GOOGLE_CHAT_SCOPES = ["https://www.googleapis.com/auth/chat.bot"]
GOOGLE_SHEETS_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

# Colunas da planilha (ordem exata)
COLUNAS = [
    "DATA DO REGISTRO",
    "ADVOGADO",
    "TRT",
    "NÚMERO DO PROCESSO",
    "NOME DO RECLAMANTE",
    "CLIENTE",
    "TIPO DE RESPONSABILIDADE",
    "TIPO DE DECISÃO",
    "RESULTADO DA DECISÃO",
    "DATA DA DECISÃO",
    "ENTENDIMENTOS FAVORÁVEIS",
    "ENTENDIMENTOS DESFAVORÁVEIS",
    "FUNDAMENTOS JURÍDICOS",
    "VALOR DA CONDENAÇÃO",
    "RESUMO",
    "OBSERVAÇÕES",
]
