"""
Cliente Google Chat API.
Usa Workload Identity Federation (credential config) ou Service Account JSON.
"""
import json
import logging
import os
import httpx
import google.auth
import google.auth.transport.requests
from google.oauth2 import service_account

logger = logging.getLogger(__name__)
_credentials = None

SCOPES = ["https://www.googleapis.com/auth/chat.bot"]
CREDENTIAL_CONFIG = {
    "universe_domain": "googleapis.com",
    "type": "external_account",
    "audience": "//iam.googleapis.com/projects/323398124593/locations/global/workloadIdentityPools/render-pool/providers/render-provider",
    "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
    "token_url": "https://sts.googleapis.com/v1/token",
    "credential_source": {
        "url": "https://oidc.render.com/token?audience=decisoes-fa-bot",
        "headers": {"Content-Type": "application/json"}
    },
    "service_account_impersonation_url": "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/decisoes-fa-bot@decisoes-fa-bot.iam.gserviceaccount.com:generateAccessToken"
}


def _load_sa_info() -> dict | None:
    for var in ["GOOGLE_SERVICE_ACCOUNT_JSON", "SA_JSON", "SERVICE_ACCOUNT_JSON"]:
        val = os.environ.get(var, "").strip()
        if val and val.startswith("{"):
            try:
                return json.loads(val)
            except Exception as e:
                logger.error("Erro ao parsear %s: %s", var, e)
    return None


def _get_credentials():
    global _credentials
    if _credentials and _credentials.valid:
        return _credentials

    # Prioridade 1: Service Account JSON direto (Gmail pessoal)
    info = _load_sa_info()
    if info:
        _credentials = service_account.Credentials.from_service_account_info(
            info, scopes=SCOPES
        )
        _credentials.refresh(google.auth.transport.requests.Request())
        logger.info("Credenciais: Service Account JSON")
        return _credentials

    # Prioridade 2: Workload Identity Federation (Render + projeto corporativo)
    try:
        import tempfile
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(CREDENTIAL_CONFIG, tmp)
        tmp.close()
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = tmp.name
        _credentials, _ = google.auth.default(scopes=SCOPES)
        _credentials.refresh(google.auth.transport.requests.Request())
        logger.info("Credenciais: Workload Identity Federation")
        return _credentials
    except Exception as e:
        logger.error("Workload Identity falhou: %s", e)

    raise RuntimeError(
        "Nenhuma credencial disponível. "
        "Configure GOOGLE_SERVICE_ACCOUNT_JSON ou use Workload Identity."
    )


def _get_token() -> str:
    creds = _get_credentials()
    if not creds.valid:
        creds.refresh(google.auth.transport.requests.Request())
    return creds.token


async def send_message(space_name: str, text: str):
    url = f"https://chat.googleapis.com/v1/{space_name}/messages"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            url,
            headers={"Authorization": f"Bearer {_get_token()}"},
            json={"text": text},
        )
    if r.status_code not in (200, 201):
        logger.error("Erro ao enviar mensagem [%s]: %s", r.status_code, r.text)
    else:
        logger.info("Mensagem enviada para %s", space_name)


async def download_file(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        r = await client.get(url, headers={"Authorization": f"Bearer {_get_token()}"})
        r.raise_for_status()
        return r.content
