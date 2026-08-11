# Router de failover (Cloudflare Worker)

Este Worker fica na frente dos dois deploys do Render e dá uma URL única e
estável para o bot. Toda requisição é enviada primeiro para o Render
principal; se ele não responder a tempo, der erro 5xx ou a conexão falhar, o
Worker tenta automaticamente o Render de fallback.

- Principal: `https://mia-falaw-bot-ngs5.onrender.com`
- Fallback: `https://miafalawbot-evjo.onrender.com`

O `wrangler.toml` fica na **raiz do repositório** (não dentro de `cloudflare/`)
apontando `main = "cloudflare/worker.js"`. Isso é proposital: se o deploy for
feito conectando o Cloudflare direto no repositório Git (opção abaixo), o
build roda `wrangler deploy` a partir da raiz — se o `wrangler.toml` estivesse
dentro da subpasta, o Cloudflare não o encontraria, tentaria detectar um
projeto estático e falharia com "Could not detect a directory containing
static files".

## Deploy — opção A: linha de comando (rápido, manual)

Requer [Node.js](https://nodejs.org) e uma conta Cloudflare (grátis).

```bash
npx wrangler login          # abre o navegador para autenticar na sua conta Cloudflare
npx wrangler deploy         # publica o Worker (rodar a partir da raiz do repo)
```

Ao final do deploy, o Wrangler mostra a URL pública do Worker, algo como:

```
https://mia-falaw-bot-router.<seu-subdominio>.workers.dev
```

## Deploy — opção B: conectado ao Git (auto-deploy a cada push)

No dashboard Cloudflare: **Workers & Pages → Create → Import a repository**,
selecione este repositório. Deixe o **Root directory** em branco (raiz) e o
**Deploy command** como `npx wrangler deploy` (padrão). Como o
`wrangler.toml` já está na raiz, o build encontra a configuração de primeira
— não precisa mexer em "Root directory" nem em nenhuma configuração
escondida.

Depois de conectado, todo push na branch configurada dispara um novo deploy
automaticamente.

## Usando o Worker no bot

Depois de publicado, troque a URL do Render pela URL do Worker nos lugares
que chamam o backend externamente:

1. `apps_script/Codigo.gs` → variável `RENDER_URL` no topo do arquivo.
2. Qualquer webhook/integração externa que hoje aponta direto para
   `mia-falaw-bot-ngs5.onrender.com`.

Não é necessário mudar nada dentro do próprio `main.py`/`bot/` — o Worker só
importa para quem *chama* o backend de fora (Apps Script, integrações
externas). O self-ping interno do Render (`main.py`, evita sleep no free
tier) continua batendo no próprio serviço normalmente.

> OAuth (`bot/oauth.py`, `OAUTH_REDIRECT_URI`) continua apontando para o
> Render principal — o redirect URI do Google OAuth precisa bater
> exatamente com o cadastrado no Google Cloud Console, então só troque para
> a URL do Worker se você também atualizar o client OAuth no Console e
> configurar um domínio customizado no Worker (workers.dev não é
> recomendado como redirect URI de produção).

## Ajustando o timeout ou as URLs

Edite o `wrangler.toml` na raiz do repositório (`[vars]`) e rode
`npx wrangler deploy` de novo (ou dê push, se estiver no modo Git-conectado),
ou ajuste as variáveis direto no dashboard Cloudflare
(Workers & Pages → mia-falaw-bot-router → Settings → Variables).
