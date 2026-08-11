"""
Configurações via variáveis de ambiente.
"""
import os
import json
import tempfile

GITHUB_TOKEN      = os.environ.get("GITHUB_TOKEN", "")

# Provedores extras de IA (OpenAI-compatible) — usados como fallback quando o
# GitHub Copilot esgota os modelos disponíveis ou falha. Cada um só entra no
# rodízio se a respectiva API key estiver configurada.
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL   = os.environ.get("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")
GROQ_API_KEY        = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL          = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
AIMLAPI_API_KEY     = os.environ.get("AIMLAPI_API_KEY", "")
AIMLAPI_MODEL       = os.environ.get("AIMLAPI_MODEL", "meta-llama/Llama-3.3-70B-Instruct-Turbo")

SPREADSHEET_ID    = os.environ.get("SPREADSHEET_ID", "1DNb3UWPAfd3wTYXGtr_4MDy7RoYOhSxbr2_FEVvDDaY")
WEBHOOK_URL       = os.environ.get("WEBHOOK_URL", "")
APPS_SCRIPT_DOPOST_URL = os.environ.get("APPS_SCRIPT_DOPOST_URL", "")
OAUTH_CLIENT_ID   = os.environ.get("OAUTH_CLIENT_ID", "")
OAUTH_CLIENT_SECRET = os.environ.get("OAUTH_CLIENT_SECRET", "")

_sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
if _sa_json:
    try:
        json.loads(_sa_json)
        _tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        _tmp.write(_sa_json)
        _tmp.close()
        GOOGLE_SERVICE_ACCOUNT_FILE = _tmp.name
    except Exception as e:
        raise RuntimeError(f"GOOGLE_SERVICE_ACCOUNT_JSON inválido: {e}")
else:
    GOOGLE_SERVICE_ACCOUNT_FILE = os.environ.get(
        "GOOGLE_SERVICE_ACCOUNT_FILE", "/secrets/service-account.json"
    )

GOOGLE_CHAT_SCOPES = ["https://www.googleapis.com/auth/chat.bot"]
GOOGLE_SHEETS_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

# Colunas da planilha (ordem exata) — atualize a planilha para incluir as novas colunas
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
    "JUIZ/RELATOR",
    "VARA/TURMA",
    "ENTENDIMENTOS FAVORÁVEIS",
    "ENTENDIMENTOS DESFAVORÁVEIS",
    "FUNDAMENTOS JURÍDICOS",
    "VALOR DA CONDENAÇÃO",
    "RESUMO",
    "OBSERVAÇÕES",
]
