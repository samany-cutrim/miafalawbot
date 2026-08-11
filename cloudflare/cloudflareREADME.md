# Router de failover (Cloudflare Worker)

Este Worker fica na frente dos dois deploys do Render e dá uma URL única e
estável para o bot. Toda requisição é enviada primeiro para o Render
principal; se ele não responder a tempo, der erro 5xx ou a conexão falhar, o
Worker tenta automaticamente o Render de fallback.

- Principal: `https://mia-falaw-bot-ngs5.onrender.com`
- Fallback: `https://miafalawbot-evjo.onrender.com`

## Deploy

Requer [Node.js](https://nodejs.org) e uma conta Cloudflare (grátis).

```bash
cd cloudflare
npx wrangler login          # abre o navegador para autenticar na sua conta Cloudflare
npx wrangler deploy         # publica o Worker
```

Ao final do deploy, o Wrangler mostra a URL pública do Worker, algo como:

```
https://mia-falaw-bot-router.<seu-subdominio>.workers.dev
```

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

Edite `wrangler.toml` (`[vars]`) e rode `npx wrangler deploy` de novo, ou
ajuste as variáveis direto no dashboard Cloudflare
(Workers & Pages → mia-falaw-bot-router → Settings → Variables).
