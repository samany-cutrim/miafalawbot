/**
 * Mia Falaw Bot — router de failover (Cloudflare Worker)
 *
 * Recebe toda requisição destinada ao backend e encaminha para o Render
 * principal. Se o principal não responder dentro do timeout, responder com
 * erro de servidor (5xx) ou falhar a conexão, a requisição é reenviada
 * automaticamente para o Render de fallback.
 *
 * Configuração via variáveis (wrangler.toml [vars] ou secrets no dashboard):
 *   PRIMARY_URL       - backend principal (ex: https://mia-falaw-bot-ngs5.onrender.com)
 *   FALLBACK_URL      - backend de fallback (ex: https://miafalawbot-evjo.onrender.com)
 *   PRIMARY_TIMEOUT_MS - tempo máximo (ms) aguardando o principal antes do fallback
 */

const DEFAULT_PRIMARY_URL = "https://mia-falaw-bot-ngs5.onrender.com";
const DEFAULT_FALLBACK_URL = "https://miafalawbot-evjo.onrender.com";
const DEFAULT_TIMEOUT_MS = 15000;

// Cabeçalhos hop-by-hop / de framing de corpo: como o corpo já é buferizado
// inteiro antes de reenviar, esses cabeçalhos do request original (ex.
// Transfer-Encoding: chunked) ficam inconsistentes com o corpo real e podem
// corromper o POST reenviado — o fetch() recalcula Content-Length sozinho.
const HOP_BY_HOP_HEADERS = [
  "host",
  "content-length",
  "transfer-encoding",
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "upgrade",
];

export default {
  async fetch(request, env) {
    const primaryUrl = env.PRIMARY_URL || DEFAULT_PRIMARY_URL;
    const fallbackUrl = env.FALLBACK_URL || DEFAULT_FALLBACK_URL;
    const timeoutMs = Number(env.PRIMARY_TIMEOUT_MS || DEFAULT_TIMEOUT_MS);

    const incoming = new URL(request.url);
    const hasBody = !["GET", "HEAD"].includes(request.method);
    const bodyBuffer = hasBody ? await request.arrayBuffer() : undefined;

    const buildRequest = (base) => {
      const target = new URL(incoming.pathname + incoming.search, base);
      const headers = new Headers(request.headers);
      for (const h of HOP_BY_HOP_HEADERS) headers.delete(h);
      return new Request(target, {
        method: request.method,
        headers,
        body: bodyBuffer,
      });
    };

    try {
      const response = await fetchWithTimeout(buildRequest(primaryUrl), timeoutMs);
      if (response.status < 500) {
        return response;
      }
      console.log(`[router] primary respondeu ${response.status}, tentando fallback`);
    } catch (err) {
      console.log(`[router] primary falhou (${err}), tentando fallback`);
    }

    try {
      return await fetchWithTimeout(buildRequest(fallbackUrl), timeoutMs);
    } catch (err) {
      return new Response(
        JSON.stringify({
          error: "Backends indisponíveis (primary e fallback falharam)",
          detail: String(err),
        }),
        { status: 502, headers: { "content-type": "application/json" } },
      );
    }
  },
};

async function fetchWithTimeout(request, timeoutMs) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(request, { signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}
