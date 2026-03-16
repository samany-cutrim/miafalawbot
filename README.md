# DecisionFA Bot v2

Bot do Google Chat para registro automático de decisões judiciais.
Um grupo compartilhado → todos os advogados postam → planilha atualizada automaticamente.

---

## Como os advogados usam

**Postar uma decisão:**
```
Cliente: iFood
Tipo: OL
[PDF anexado]
```
> Cliente e Tipo são opcionais — se não informados, a IA tenta detectar da própria decisão.

**Buscar precedentes:**
```
/favoraveis vínculo empregatício
/desfavoraveis responsabilidade subsidiária
/ajuda
```

---

## Tipos de responsabilidade aceitos
`OL` · `Nuvem` · `Terceirização` · `Subsidiária` · `Ex Funcionário` · `Ex-Foodlovers` · `Marketplace`

---

## O que é registrado na planilha

| Coluna | Origem |
|---|---|
| DATA DO REGISTRO | Automático |
| ADVOGADO | Nome do remetente no Google Chat |
| TRT | Extraído pela IA |
| NÚMERO DO PROCESSO | Extraído pela IA |
| NOME DO RECLAMANTE | Extraído pela IA |
| CLIENTE | Texto da mensagem (prioridade) ou IA |
| TIPO DE RESPONSABILIDADE | Texto da mensagem (prioridade) ou IA |
| TIPO DE DECISÃO | Extraído pela IA |
| RESULTADO DA DECISÃO | Extraído pela IA |
| DATA DA DECISÃO | Extraído pela IA |
| ENTENDIMENTOS FAVORÁVEIS | Extraído pela IA |
| ENTENDIMENTOS DESFAVORÁVEIS | Extraído pela IA |
| FUNDAMENTOS JURÍDICOS | Extraído pela IA |
| VALOR DA CONDENAÇÃO | Extraído pela IA |
| RESUMO | Gerado pela IA |
| OBSERVAÇÕES | Gerado pela IA |

---

## Deploy no Google Cloud Run

### 1. Criar Service Account

```bash
gcloud iam service-accounts create decisionfa-bot \
  --display-name="DecisionFA Bot"
```

Baixe o JSON da chave no Console: **IAM → Service Accounts → decisionfa-bot → Keys → Add Key → JSON**

### 2. Compartilhar a planilha

Abra `https://docs.google.com/spreadsheets/d/1DNb3UWPAfd3wTYXGtr_4MDy7RoYOhSxbr2_FEVvDDaY`
e compartilhe com o e-mail da Service Account como **Editor**.

A aba `Precedentes` precisa existir com os cabeçalhos na linha 1
(o bot cria abas de cliente automaticamente).

### 3. Subir o JSON como Secret

```bash
gcloud secrets create sa-decisionfa \
  --data-file=service-account.json

gcloud secrets add-iam-policy-binding sa-decisionfa \
  --member="serviceAccount:decisionfa-bot@SEU-PROJETO.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"
```

### 4. Build e deploy

```bash
# Build
gcloud builds submit --tag gcr.io/SEU-PROJETO/decisionfa-bot

# Deploy
gcloud run deploy decisionfa-bot \
  --image gcr.io/SEU-PROJETO/decisionfa-bot \
  --platform managed \
  --region us-central1 \
  --allow-unauthenticated \
  --set-env-vars ANTHROPIC_API_KEY=sk-ant-XXXX \
  --set-env-vars SPREADSHEET_ID=1DNb3UWPAfd3wTYXGtr_4MDy7RoYOhSxbr2_FEVvDDaY \
  --set-secrets /secrets/service-account.json=sa-decisionfa:latest \
  --set-env-vars GOOGLE_SERVICE_ACCOUNT_FILE=/secrets/service-account.json \
  --memory 512Mi \
  --concurrency 80 \
  --min-instances 0 \
  --max-instances 5
```

### 5. Registrar o bot no Google Chat API

No Google Cloud Console:
```
APIs & Services → Google Chat API → Configuration
  App name: Decisão FA Bot
  Webhook URL: https://URL-DO-CLOUD-RUN/webhook
  Events: MESSAGE
```

### 6. Adicionar o bot ao grupo

No grupo `https://chat.google.com/room/AAQA6i0sAWc`:
```
Membros → Adicionar pessoas e bots → Decisão FA Bot
```

---

## Teste local

```bash
pip install -r requirements.txt

export ANTHROPIC_API_KEY=sk-ant-XXXX
export SPREADSHEET_ID=1DNb3UWPAfd3wTYXGtr_4MDy7RoYOhSxbr2_FEVvDDaY
export GOOGLE_SERVICE_ACCOUNT_FILE=./service-account.json

uvicorn main:app --reload --port 8080

# Expor para o Google Chat (necessário HTTPS):
ngrok http 8080
```

---

## Health check

```
GET /health → {"status": "ok"}
```
