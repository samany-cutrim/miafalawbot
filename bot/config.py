"""
Configurações via variáveis de ambiente.
O JSON da Service Account pode ser passado diretamente como variável
GOOGLE_SERVICE_ACCOUNT_JSON (string JSON) em vez de arquivo.
"""
import os
import tempfile

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
SPREADSHEET_ID    = os.environ.get("SPREADSHEET_ID", "1DNb3UWPAfd3wTYXGtr_4MDy7RoYOhSxbr2_FEVvDDaY")

# Suporte a JSON direto como variável de ambiente (para Leapcell e similares)
_sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
if _sa_json:
    _tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    _tmp.write(_sa_json)
    _tmp.flush()
    GOOGLE_SERVICE_ACCOUNT_FILE = _tmp.name
else:
    GOOGLE_SERVICE_ACCOUNT_FILE = os.environ.get(
        "GOOGLE_SERVICE_ACCOUNT_FILE", "/secrets/service-account.json"
    )

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
