# Mia Falaw Bot

Este projeto suporta dois modos:

1. Modo sem admin (recomendado): Apps Script + Incoming Webhook + endpoints HTTP.
2. Modo opcional: Google Chat App em /chat (somente se houver permissao para criar bot no Workspace).

## Modo sem admin (Apps Script)

Fluxo:

1. Usuario envia decisao via Google Form (PDF + campos).
2. Apps Script extrai texto do PDF e chama o backend em /processar-texto.
3. Backend responde no espaco do Chat via Incoming Webhook.
4. Comandos no chat sao lidos por polling (a cada 1 minuto) e encaminhados ao backend.

Comandos suportados:

- /ajuda
- /link
- /favoraveis [tema]
- /desfavoraveis [tema]
- /confirmar
- /cancelar
- /corrigir [instrucao]
- /sim
- /nao

## Endpoints ativos

Apps Script / Webhook:

- POST /processar-texto
- POST /processar
- POST /processar-base64
- POST /buscar
- POST /ajuda
- POST /link
- POST /confirmar
- POST /cancelar
- POST /corrigir
- POST /sim
- POST /nao

Opcional Chat App:

- POST /chat

Utilitarios:

- GET /health
- GET /debug-models

## Variaveis de ambiente

- GITHUB_TOKEN
- SPREADSHEET_ID
- GOOGLE_SERVICE_ACCOUNT_JSON (recomendado)
- GOOGLE_SERVICE_ACCOUNT_FILE (fallback)

Observacao de modelos:

- O bot usa apenas GitHub Copilot (GitHub Models).
- Ordem fixa: Claude primeiro, Gemini em segundo.
- Modelos GPT nao sao utilizados.

## Configuracao do Apps Script

1. Abra apps_script/Codigo.gs e preencha:
- RENDER_URL
- WEBHOOK_URL
- FORM_ID
- SPACE_NAME
2. Publique/salve o projeto Apps Script.
3. Execute instalarTriggers para criar os gatilhos.
4. Teste com testarConexao e testarWebhook.

## Limite importante

Sem criar um Chat App admin, nao existe evento CARD_CLICKED nativo no Incoming Webhook.
Por isso, no modo sem admin, a interacao segue por comandos de texto no chat.
