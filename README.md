# DecisionFA Bot v3

Bot para registro automático de decisões judiciais via Google Chat.
**Não requer aprovação de superadmin nem Google Workspace pago.**

---

## Como funciona

```
Advogado posta PDF no grupo
         ↓
  Apps Script (conta do escritório, OAuth do usuário)
  detecta mensagem a cada 1 min
         ↓
  Chama POST /processar no Render
         ↓
  Render baixa PDF → Claude analisa → salva planilha
         ↓
  Resposta via Incoming Webhook → aparece no grupo
```

---

## Passo a passo de configuração

### 1. Servidor no Render (já está rodando — só atualize)

Atualize os arquivos do repositório com esta versão v3.  
Variáveis de ambiente necessárias no Render (mesmas de antes):

```
ANTHROPIC_API_KEY=sk-ant-...
SPREADSHEET_ID=1DNb3UWPAfd3wTYXGtr_4MDy7RoYOhSxbr2_FEVvDDaY
GOOGLE_SERVICE_ACCOUNT_JSON={"type":"service_account",...}
```

Teste: `GET https://SEU-APP.onrender.com/health` deve retornar `{"status":"ok","version":"v3"}`

---

### 2. Criar o Incoming Webhook no grupo do Chat

> Qualquer membro do grupo pode fazer isso — **não precisa de admin**.

1. Abra o grupo do Chat no navegador
2. Clique no **nome do espaço** (topo) → **Apps e integrações**
3. Clique em **Adicionar webhooks**
4. Dê um nome (ex: `DecisionFA Bot`) e salve
5. **Copie a URL gerada** — ela começa com `https://chat.googleapis.com/v1/spaces/...`

---

### 3. Descobrir o ID do espaço

Na URL do grupo no navegador você verá algo como:
`https://chat.google.com/room/AAQeXi0sAWc/...`

O ID do espaço é: `spaces/AAQeXi0sAWc`

---

### 4. Criar o Google Apps Script

> Use a conta Google do escritório (a que está no grupo do Chat).

1. Acesse [script.google.com](https://script.google.com)
2. Clique em **Novo projeto**
3. Nomeie como `DecisionFA Bot`
4. Apague o código padrão e cole o conteúdo de `apps_script/Codigo.gs`
5. No topo do arquivo, preencha as 3 constantes:

```javascript
var SPACE_NAME  = "spaces/AAQeXi0sAWc";        // ID do espaço
var RENDER_URL  = "https://meu-app.onrender.com"; // URL do Render
var WEBHOOK_URL = "https://chat.googleapis.com/v1/spaces/.../messages?key=...&token=..."; // webhook copiado no passo 2
```

6. Clique em **Configurações do projeto** (ícone ⚙️) → marque **Mostrar arquivo de manifesto**
7. Clique em `appsscript.json` e substitua o conteúdo pelo arquivo `apps_script/appsscript.json`
8. Salve tudo (`Ctrl+S`)

---

### 5. Autorizar e instalar os triggers

1. No editor, selecione a função `testarConexao` no dropdown
2. Clique em **Executar** → autorize as permissões solicitadas
   - O Google vai pedir permissão para: Chat (somente leitura), Drive, requests externos
   - **Aceite tudo** — são as permissões do *seu* usuário, não de um bot
3. Veja no log: deve aparecer `Render status: 200`
4. Agora selecione `instalarTriggers` e execute
5. Veja no log: `✅ Triggers instalados com sucesso!`

---

### 6. Testar

Vá ao grupo do Chat e envie uma mensagem com um PDF anexado.  
Em até **1 minuto** o bot deve responder com a análise.

Para comandos:
- `/favoraveis vínculo empregatício`
- `/desfavoraveis responsabilidade subsidiária`
- `/ajuda`

---

## O que NÃO precisa

- ❌ Aprovação do superadmin do Workspace
- ❌ Publicar um app no Google Workspace Marketplace
- ❌ Conta Google pessoal separada
- ❌ Configuração de Workload Identity / Service Account no Chat

## O que SIM precisa

- ✅ Conta do escritório com acesso ao grupo do Chat
- ✅ A Service Account existente (só para Sheets — igual antes)
- ✅ Servidor no Render rodando

---

## Estrutura dos arquivos

```
decisoesfabot-v3/
├── main.py              # FastAPI — recebe chamadas do Apps Script
├── requirements.txt
├── Dockerfile
├── bot/
│   ├── handlers.py      # Lógica de análise (Claude + planilha)
│   ├── webhook.py       # Envia respostas via Incoming Webhook
│   ├── sheets.py        # Google Sheets (sem mudanças)
│   └── config.py        # Variáveis de ambiente (sem mudanças)
└── apps_script/
    ├── Codigo.gs        # Script que roda na conta do escritório
    └── appsscript.json  # Manifesto com escopos OAuth
```

---

## Latência

O script verifica mensagens a cada 1 minuto.  
Isso significa que o bot pode demorar até **~1 min** para começar a processar após o PDF ser postado.  
O processamento em si (Claude + planilha) leva mais ~20-30s.

Se quiser resposta mais rápida, o mínimo do trigger do Apps Script é 1 minuto — é a limitação da plataforma.
