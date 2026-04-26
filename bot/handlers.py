"""
Handlers v3 — com confirmação humana antes de salvar na planilha.
Sessões armazenadas em memória por advogado (chave = nome do advogado).
"""

import asyncio
import io
import json
import logging
import re
from datetime import datetime, timezone, timedelta
import pdfplumber

_TZ_BRASILIA = timezone(timedelta(hours=-3))

def _now_br() -> datetime:
    return datetime.now(_TZ_BRASILIA)

from bot.sheets import salvar_decisao, buscar_precedentes, carregar_sessoes, salvar_sessoes
from bot.config import GITHUB_TOKEN
from bot.webhook import send_webhook

import httpx

logger = logging.getLogger(__name__)

# GitHub Copilot (via openai SDK)
_copilot = None
_copilot_ok = False
_cached_model_order: list[str] | None = None
_invalid_models: set[str] = set()
_last_success_model: str | None = None
if GITHUB_TOKEN:
    try:
        from openai import AsyncOpenAI
        _copilot = AsyncOpenAI(
            api_key=GITHUB_TOKEN,
            base_url="https://models.inference.ai.azure.com",
        )
        _copilot_ok = True
        logger.info("GitHub Copilot (openai SDK) inicializado com sucesso.")
    except Exception as e:
        logger.error("Falha ao inicializar GitHub Copilot: %s", e)

# ---------------------------------------------------------------------------
# SESSÕES PENDENTES — persistidas no Google Sheets para sobreviver a restarts
# ---------------------------------------------------------------------------

def _chave_sessao(nome: str) -> str:
    """Normaliza o nome para usar como chave de sessão.
    Ex: 'Samany Cutrim' → 'samany', 'samany' → 'samany'
    """
    if not nome:
        return "advogado"
    return nome.strip().lower().split()[0]


# ---------------------------------------------------------------------------
# MAPA DE SIGLAS
# ---------------------------------------------------------------------------

SIGLAS: dict = {
    "Samany Cutrim":                      "SC",
    "samany":                             "SC",
    "Letícia Silva":                      "LSS",
    "leticia":                            "LSS",
    "Pollyanna Rodrigues Godoy Dias":     "PGD",
    "pollyanna":                          "PGD",
    "Tatiana G. Ferraz Andrade":          "TGFA",
    "tatiana":                            "TGFA",
    "Natany Valentim Gonçalves":          "NVG",
    "natany":                             "NVG",
    "Fernando Attilio Trevisan Júnior":   "FAT",
    "fernando":                           "FAT",
    "Camilla Mele Martinez":              "CMM",
    "camilla":                            "CMM",
    "Beatriz Agar Domingues da Silva":    "BADS",
    "beatriz":                            "BADS",
    "Lilian Missora Matsumoto":           "LMM",
    "lilian":                             "LMM",
    "Indyara Tomé de Brito":              "ITB",
    "indyara":                            "ITB",
}

def resolver_sigla(display_name: str) -> str:
    if not display_name:
        return "N/A"
    if display_name in SIGLAS:
        return SIGLAS[display_name]
    lower = display_name.strip().lower()
    for nome, sigla in SIGLAS.items():
        if nome.lower() == lower:
            return sigla
    primeiro = lower.split()[0] if lower.split() else lower
    for nome, sigla in SIGLAS.items():
        if nome.lower().startswith(primeiro):
            return sigla
    for nome, sigla in SIGLAS.items():
        partes = nome.lower().split()
        if all(p in lower for p in partes[:2]):
            return sigla
    partes = display_name.strip().split()
    sigla_auto = "".join(p[0].upper() for p in partes if p)
    logger.warning("Advogado '%s' não encontrado. Sigla: '%s'", display_name, sigla_auto)
    return sigla_auto


TIPOS_VALIDOS = ["OL", "Nuvem", "Terceirização", "Subsidiária", "Ex Funcionário", "Ex-Foodlovers", "Marketplace"]


# ---------------------------------------------------------------------------
# PARSE DA MENSAGEM
# ---------------------------------------------------------------------------

def parse_mensagem(text: str) -> dict:
    result = {"cliente": None, "tipo": None}
    m = re.search(r"cliente[:\s]+([^\n,/]+)", text, re.IGNORECASE)
    if m:
        result["cliente"] = m.group(1).strip()
    m = re.search(r"tipo[:\s]+([^\n,/]+)", text, re.IGNORECASE)
    if m:
        result["tipo"] = m.group(1).strip()
    else:
        for t in TIPOS_VALIDOS:
            if re.search(rf"\b{re.escape(t)}\b", text, re.IGNORECASE):
                result["tipo"] = t
                break
    return result


# ---------------------------------------------------------------------------
# EXTRAÇÃO DE TEXTO DO PDF
# ---------------------------------------------------------------------------

def extrair_texto_pdf(pdf_bytes: bytes) -> str:
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            return "\n".join(p.extract_text() or "" for p in pdf.pages)
    except Exception as e:
        logger.exception("Erro ao extrair PDF: %s", e)
        return ""


async def download_pdf(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.content


# ---------------------------------------------------------------------------
# EXTRAÇÃO DO TRT DO NÚMERO DO PROCESSO
# ---------------------------------------------------------------------------

def extrair_trt_do_processo(numero: str) -> str:
    if not numero or numero == "N/A":
        return "N/A"
    try:
        m = re.search(r'\d+-\d+\.\d{4}\.(\d)\.(\d{2})\.\d+', numero)
        if m and m.group(1) == "5":
            tribunal = m.group(2).lstrip("0") or m.group(2)
            return f"TRT-{tribunal}"
        m2 = re.search(r'\.5\.(\d{2})\.', numero)
        if m2:
            tribunal = m2.group(1).lstrip("0") or m2.group(1)
            return f"TRT-{tribunal}"
    except Exception:
        pass
    return "N/A"


# ---------------------------------------------------------------------------
# CHAMADA À IA
# ---------------------------------------------------------------------------

# Modelos via GitHub Copilot (models.inference.ai.azure.com)
# Ordem de preferência: Claude > Gemini > GPT > Llama (último recurso — respostas genéricas).
_GITHUB_MODELS_FALLBACK = [
    # Claude (Anthropic) — melhor qualidade jurídica
    "claude-sonnet-4-6",
    "claude-sonnet-4-5",
    "claude-3-7-sonnet",
    "claude-sonnet-4",
    "claude-3-5-sonnet",
    "claude-haiku-4-5",
    "claude-3-5-haiku",
    # Gemini — segunda opção
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    # GPT — terceira opção
    "gpt-4.1",
    "gpt-4o",
    "gpt-5",
    "gpt-4.1-mini",
    "gpt-4o-mini",
    "gpt-5-mini",
    # Llama — ÚLTIMO RECURSO (respostas genéricas, evitar)
    "Llama-3.3-70B-Instruct",
    "Meta-Llama-3.1-405B-Instruct",
    "Meta-Llama-3.1-70B-Instruct",
    "Meta-Llama-3.1-8B-Instruct",
]
_GITHUB_MAX_CHARS = 12000  # reduzido para caber no timeout de 30s do Google Chat

# Palavras que identificam modelos que não devem ser usados para chat/completion.
_NON_CHAT_KEYWORDS = ("text-embedding", "cohere", "embed")


def _extrair_id_modelo(raw: str) -> str:
    """Extrai o nome curto de um ID no formato azureml://registries/.../models/{nome}/versions/{v}."""
    if raw.startswith("azureml://"):
        parts = raw.split("/")
        try:
            idx = parts.index("models")
            return parts[idx + 1]
        except (ValueError, IndexError):
            return raw
    return raw


def _e_non_chat(model_id: str) -> bool:
    low = model_id.lower()
    return any(kw in low for kw in _NON_CHAT_KEYWORDS)


async def _resolver_modelos_github() -> list[str]:
    """Resolve os modelos permitidos pela conta: Claude > Gemini > Llama > GPT."""
    global _cached_model_order

    if _cached_model_order:
        return _cached_model_order

    ids_brutos: list[str] = []
    if GITHUB_TOKEN:
        try:
            async with httpx.AsyncClient(timeout=15) as cli:
                r = await cli.get(
                    "https://models.inference.ai.azure.com/models",
                    headers={"Authorization": f"Bearer {GITHUB_TOKEN}"},
                )
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, list):
                        ids_brutos = [m.get("id", "") or m.get("name", "") for m in data if isinstance(m, dict)]
                    elif isinstance(data, dict):
                        items = data.get("data") or data.get("models") or data.get("value") or []
                        ids_brutos = [m.get("id", "") or m.get("name", "") for m in items if isinstance(m, dict)]
                    ids_brutos = [m for m in ids_brutos if m]
                else:
                    logger.warning("Falha ao listar modelos (status %s).", r.status_code)
        except Exception as e:
            logger.warning("Falha ao listar modelos do endpoint (%s).", e)

    if not ids_brutos:
        _cached_model_order = list(_GITHUB_MODELS_FALLBACK)
        logger.info("Usando lista de modelos fallback.")
        return _cached_model_order

    # Extrai IDs curtos e remove modelos que não suportam chat/completion.
    modelos_validos = [_extrair_id_modelo(raw) for raw in ids_brutos]
    modelos_validos = [m for m in modelos_validos if m and not _e_non_chat(m)]
    logger.info("Modelos disponíveis para chat/completion: %s", ", ".join(modelos_validos))

    if not modelos_validos:
        _cached_model_order = list(_GITHUB_MODELS_FALLBACK)
        return _cached_model_order

    lower_map = {m.lower(): m for m in modelos_validos}

    claude = [lower_map[k] for k in lower_map if "claude" in k]
    gemini = [lower_map[k] for k in lower_map if "gemini" in k]
    llama  = [lower_map[k] for k in lower_map if "llama" in k or "meta" in k]
    gpt    = [lower_map[k] for k in lower_map if "gpt" in k]

    def _ordenar(candidatos: list[str], prefs: list[str]) -> list[str]:
        ordenados: list[str] = []
        usados: set[str] = set()
        for pref in prefs:
            pref_l = pref.lower()
            for c in candidatos:
                if c in usados:
                    continue
                if pref_l in c.lower() or c.lower() in pref_l:
                    ordenados.append(c)
                    usados.add(c)
        for c in candidatos:
            if c not in usados:
                ordenados.append(c)
        return ordenados

    claude = _ordenar(claude, ["claude-sonnet-4-6", "claude-sonnet-4-5", "claude-3-7-sonnet",
                               "claude-sonnet-4", "claude-3-5-sonnet", "claude-haiku-4-5", "claude-3-5-haiku"])
    gemini = _ordenar(gemini, ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash"])
    gpt    = _ordenar(gpt, ["gpt-4.1", "gpt-4o", "gpt-5", "gpt-4.1-mini", "gpt-4o-mini", "gpt-5-mini"])
    llama  = _ordenar(llama,  ["Llama-3.3-70B-Instruct", "Meta-Llama-3.1-405B-Instruct",
                               "Meta-Llama-3.1-70B-Instruct", "Meta-Llama-3.1-8B-Instruct"])

    # Claude → Gemini → GPT → Llama (último recurso)
    _cached_model_order = claude + gemini + gpt + llama
    if not _cached_model_order:
        # Último recurso: qualquer modelo não-GPT disponível
        _cached_model_order = list(modelos_validos)
    return _cached_model_order

async def _chamar_ia(prompt: str) -> str:
    global _last_success_model

    def _is_unknown_model_error(err: Exception) -> bool:
        msg = str(err).lower()
        return "unknown_model" in msg or "unknown model" in msg

    if _copilot_ok and _copilot:
        modelos = await _resolver_modelos_github()
        modelos = [m for m in modelos if m not in _invalid_models]

        if _last_success_model and _last_success_model in modelos:
            modelos = [_last_success_model] + [m for m in modelos if m != _last_success_model]

        if not modelos:
            raise RuntimeError(
                "Nenhum modelo de chat válido disponível no endpoint da conta. "
                "Use /debug-models para verificar os modelos liberados para o GITHUB_TOKEN."
            )

        # Trunca o prompt para não exceder o limite de tokens do endpoint
        prompt_github = prompt[:_GITHUB_MAX_CHARS] if len(prompt) > _GITHUB_MAX_CHARS else prompt
        if len(prompt) > _GITHUB_MAX_CHARS:
            logger.warning("Prompt truncado de %d para %d chars para GitHub Copilot.", len(prompt), _GITHUB_MAX_CHARS)
        for model_name in modelos:
            try:
                response = await _copilot.chat.completions.create(
                    model=model_name,
                    max_tokens=2048,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "Você é Mia, assistente jurídica especializada em direito trabalhista brasileiro, "
                                "com vasta experiência em análise de decisões judiciais. "
                                "Quando solicitado a retornar JSON, responda APENAS com JSON válido, sem markdown, "
                                "sem blocos de código, sem explicações e sem texto adicional antes ou depois do JSON."
                            ),
                        },
                        {"role": "user", "content": prompt_github},
                    ],
                )
                logger.info("IA: GitHub Copilot respondeu com modelo %s.", model_name)
                _last_success_model = model_name
                return response.choices[0].message.content or ""
            except Exception as e:
                err_str = str(e)
                if _is_unknown_model_error(e):
                    _invalid_models.add(model_name)
                # 429 rate limit — troca de modelo imediatamente sem esperar retry
                if "429" in err_str or "rate_limit" in err_str.lower() or "too many requests" in err_str.lower():
                    logger.warning("[PDF] Modelo %s com rate limit (429) — trocando imediatamente.", model_name)
                    _invalid_models.add(model_name)
                else:
                    logger.warning("GitHub Copilot modelo %s falhou (%s). Tentando próximo...", model_name, e)

    raise RuntimeError(
        "Nenhuma IA disponível no endpoint com modelos de chat/completion. "
        "Confirme o GITHUB_TOKEN e os modelos liberados para a conta (verifique /debug-models)."
    )


# ---------------------------------------------------------------------------
# ANÁLISE COM IA
# ---------------------------------------------------------------------------

PROMPT_ANALISE = """Você é Mia, especialista sênior em decisões judiciais trabalhistas brasileiras, com 15 anos de experiência representando empresas em ações trabalhistas.

CONTEXTO FUNDAMENTAL:
- O escritório representa EXCLUSIVAMENTE a empresa (reclamada/ré), NUNCA o trabalhador.
- "Favorável" = favorável À EMPRESA (pedido negado, vínculo não reconhecido, condenação reduzida, recurso provido para a empresa).
- "Desfavorável" = desfavorável À EMPRESA (vínculo reconhecido, condenação imposta, recurso negado, pedido deferido).
- "Parcialmente Favorável" = quando houve condenação mas alguns pedidos foram negados, reduzindo a exposição.
- Entendimentos favoráveis = teses que PROTEGEM a empresa e podem ser replicadas como precedente.
- Entendimentos desfavoráveis = teses que PREJUDICAM a empresa e demandam atenção/recurso.

TAREFA: Analise profundamente a decisão judicial abaixo. Extraia TODOS os dados relevantes com máxima precisão.

Retorne APENAS um JSON válido com os seguintes campos:
{{
  "trt": "TRT-X (ex: TRT-2, TRT-15) ou N/A — extraia do número do processo se necessário",
  "numero_processo": "formato 0000000-00.0000.0.00.0000 exato do documento ou N/A",
  "nome_reclamante": "nome completo do trabalhador/reclamante ou N/A",
  "data_decisao": "DD/MM/AAAA — procure na assinatura final, cabeçalho ou data de publicação ou N/A",
  "tipo_decisao": "Sentença | Acórdão | Decisão Interlocutória | Despacho",
  "resultado_geral": "Favorável | Desfavorável | Parcialmente Favorável — SEMPRE do ponto de vista da EMPRESA",
  "cliente_detectado": "razão social completa do réu/reclamado principal ou N/A",
  "tipo_responsabilidade_detectado": "OL | Nuvem | Terceirização | Subsidiária | Ex Funcionário | Ex-Foodlovers | Marketplace | N/A",
  "juiz_relator": "nome completo com titulação (ex: Juiz do Trabalho Dr. João Silva) ou N/A",
  "vara_turma": "nome completo (ex: 3ª Vara do Trabalho de São Paulo | 2ª Turma do TRT-2) ou N/A",
  "pedidos_deferidos": ["lista de pedidos/verbas que foram concedidos ao reclamante"],
  "pedidos_indeferidos": ["lista de pedidos/verbas que foram negados — vitórias para a empresa"],
  "entendimentos_favoraveis": [
    {{
      "tema": "nome do tema jurídico (ex: Vínculo Empregatício, Horas Extras)",
      "entendimento": "tese exata adotada que beneficia a empresa, citando base legal se houver",
      "uso_como_precedente": "como replicar essa tese em outros casos semelhantes"
    }}
  ],
  "entendimentos_desfavoraveis": [
    {{
      "tema": "nome do tema jurídico",
      "entendimento": "tese adotada que prejudica a empresa, com análise do impacto",
      "estrategia_recursal": "qual argumento usar para recorrer ou mitigar esse entendimento"
    }}
  ],
  "fundamentos_juridicos": "todos os artigos CLT/CC, súmulas TST/STJ/STF, OJs, temas repetitivos citados na decisão",
  "valor_condenacao": "valor líquido em reais (ex: R$ 15.430,00) ou N/A se favorável",
  "deposito_recursal": "valor estimado de depósito recursal (50% da condenação, limitado ao teto legal) ou N/A",
  "prazo_recursal": "prazo para recurso (geralmente 8 dias úteis para RO) ou N/A",
  "resumo_geral": "resumo executivo em 4-6 linhas do ponto de vista da empresa: o que foi decidido, impacto financeiro, risco residual e recomendação imediata",
  "nivel_risco": "BAIXO | MÉDIO | ALTO | CRÍTICO — avaliação do risco financeiro/jurídico para a empresa",
  "recomendacao": "RECORRER | AGUARDAR | CUMPRIR | NEGOCIAR — recomendação estratégica com breve justificativa",
  "observacoes_precedente": "análise detalhada de como usar ou neutralizar esta decisão em outros processos do mesmo cliente ou casos similares"
}}

CLIENTE (EMPRESA RECLAMADA) INFORMADO PELO ADVOGADO: {cliente}
TIPO DE RESPONSABILIDADE INFORMADO PELO ADVOGADO: {tipo}

DECISÃO JUDICIAL COMPLETA:
{texto}

RETORNE APENAS JSON VÁLIDO, sem markdown, sem blocos de código, sem texto adicional."""


async def analisar_decisao(texto: str, cliente_hint: str, tipo_hint: str) -> dict:
    prompt = PROMPT_ANALISE.format(
        cliente=cliente_hint or "Não informado — detecte da decisão",
        tipo=tipo_hint or "Não informado — detecte da decisão",
        texto=texto[:40000],
    )
    raw = await _chamar_ia(prompt)
    return _parse_json(raw)


def _parse_json(raw: str) -> dict:
    try:
        m = re.search(r"\{[\s\S]*\}", raw)
        if m:
            return json.loads(m.group(0))
    except Exception:
        pass
    logger.warning("Falha ao parsear JSON da IA. Raw: %s", raw[:300])
    return {k: "N/A" for k in [
        "trt", "numero_processo", "nome_reclamante", "data_decisao",
        "tipo_decisao", "resultado_geral", "cliente_detectado",
        "tipo_responsabilidade_detectado", "juiz_relator", "vara_turma",
        "entendimentos_favoraveis", "entendimentos_desfavoraveis",
        "fundamentos_juridicos", "valor_condenacao", "resumo_geral",
        "observacoes_precedente",
    ]}


# ---------------------------------------------------------------------------
# FORMATAÇÃO DO RELATÓRIO
# ---------------------------------------------------------------------------

def formatar_relatorio(d: dict, sigla: str) -> str:
    resultado = d.get('resultado_geral', 'N/A')
    nivel_risco = d.get('nivel_risco', '')
    recomendacao = d.get('recomendacao', '')

    # Ícone do resultado
    if 'Favorável' in resultado and 'Parcialmente' not in resultado:
        icone_resultado = '🟢'
    elif 'Parcialmente' in resultado:
        icone_resultado = '🟡'
    else:
        icone_resultado = '🔴'

    # Ícone do risco
    risco_icones = {'BAIXO': '🟢', 'MÉDIO': '🟡', 'ALTO': '🔴', 'CRÍTICO': '🚨'}
    icone_risco = risco_icones.get(nivel_risco.upper() if nivel_risco else '', '⚪')

    r = f"{icone_resultado} *ANÁLISE CONCLUÍDA — aguardando confirmação*\n\n"

    # Bloco identificação
    r += f"📋 *{d.get('tipo_decisao', 'N/A')}* — {icone_resultado} {resultado}\n"
    r += f"🏛️ *TRT:* {d.get('trt', 'N/A')}\n"
    r += f"📄 *Processo:* {d.get('numero_processo', 'N/A')}\n"
    r += f"👤 *Reclamante:* {d.get('nome_reclamante', 'N/A')}\n"
    r += f"🏢 *Cliente:* {d.get('_cliente_final', 'N/A')}\n"
    r += f"⚖️ *Tipo:* {d.get('_tipo_final', 'N/A')}\n"
    r += f"📅 *Data:* {d.get('data_decisao', 'N/A')}\n"
    r += f"👨‍⚖️ *Juiz/Relator:* {d.get('juiz_relator', 'N/A')}\n"
    r += f"🏠 *Vara/Turma:* {d.get('vara_turma', 'N/A')}\n"
    r += f"💰 *Valor:* {d.get('valor_condenacao', 'N/A')}\n"

    if d.get('deposito_recursal') and d.get('deposito_recursal') != 'N/A':
        r += f"🏦 *Depósito Recursal:* {d.get('deposito_recursal')}\n"
    if d.get('prazo_recursal') and d.get('prazo_recursal') != 'N/A':
        r += f"⏰ *Prazo Recursal:* {d.get('prazo_recursal')}\n"

    r += "\n"

    # Resumo executivo
    r += f"📝 *Resumo Executivo:*\n{d.get('resumo_geral', 'N/A')}\n\n"

    # Risco e recomendação
    if nivel_risco:
        r += f"{icone_risco} *Nível de Risco:* {nivel_risco}\n"
    if recomendacao:
        rec_icones = {'RECORRER': '⚡', 'AGUARDAR': '⏳', 'CUMPRIR': '✅', 'NEGOCIAR': '🤝'}
        icone_rec = rec_icones.get(recomendacao.split()[0].upper() if recomendacao else '', '📌')
        r += f"{icone_rec} *Recomendação:* {recomendacao}\n"
    r += "\n"

    # Pedidos indeferidos (vitórias)
    pedidos_ind = d.get('pedidos_indeferidos') or []
    if isinstance(pedidos_ind, list) and pedidos_ind:
        r += "🏆 *Pedidos Indeferidos (vitórias da empresa):*\n"
        for item in pedidos_ind:
            r += f"  • {item}\n"
        r += "\n"

    # Pedidos deferidos
    pedidos_def = d.get('pedidos_deferidos') or []
    if isinstance(pedidos_def, list) and pedidos_def:
        r += "⚠️ *Pedidos Deferidos (contra a empresa):*\n"
        for item in pedidos_def:
            r += f"  • {item}\n"
        r += "\n"

    # Entendimentos favoráveis
    favs = d.get('entendimentos_favoraveis') or []
    if isinstance(favs, list) and favs:
        r += "✅ *Entendimentos Favoráveis (use como precedente):*\n"
        for i, e in enumerate(favs, 1):
            if isinstance(e, dict):
                r += f"  {i}. *{e.get('tema','')}:* {e.get('entendimento','')}\n"
                if e.get('uso_como_precedente'):
                    r += f"     _↳ {e.get('uso_como_precedente')}_\n"
        r += "\n"

    # Entendimentos desfavoráveis
    desfavs = d.get('entendimentos_desfavoraveis') or []
    if isinstance(desfavs, list) and desfavs:
        r += "❌ *Entendimentos Desfavoráveis (atenção/recurso):*\n"
        for i, e in enumerate(desfavs, 1):
            if isinstance(e, dict):
                r += f"  {i}. *{e.get('tema','')}:* {e.get('entendimento','')}\n"
                if e.get('estrategia_recursal'):
                    r += f"     _↳ Estratégia: {e.get('estrategia_recursal')}_\n"
        r += "\n"

    r += f"📚 *Fundamentos Jurídicos:* {d.get('fundamentos_juridicos', 'N/A')}\n\n"
    r += f"📌 *Uso como Precedente:*\n{d.get('observacoes_precedente', 'N/A')}\n"
    r += f"\n_Analisado por {sigla} em {_now_br().strftime('%d/%m/%Y %H:%M')}_"
    return r


def formatar_entendimentos(lista: list) -> str:
    if not isinstance(lista, list) or not lista:
        return ""
    partes = []
    for e in lista:
        if isinstance(e, dict):
            partes.append(f"{e.get('tema','')}: {e.get('entendimento','')}")
        else:
            partes.append(str(e))
    return " | ".join(partes)


def mensagem_confirmacao(advogado: str) -> str:
    return (
        f"⚠️ *{advogado}, revise e confirme:*\n\n"
        f"`/confirmar` — salva na planilha\n"
        f"`/corrigir [instrução]` — corrige e reanalisa\n"
        f"`/cancelar` — descarta\n\n"
        f"_Exemplo: `/corrigir o resultado deve ser Desfavorável e o TRT é TRT-2`_"
    )


# ---------------------------------------------------------------------------
# SESSÕES — confirmar/cancelar
# ---------------------------------------------------------------------------

def _montar_row(analise: dict, sigla: str, hints: dict) -> dict:
    cliente_final = hints["cliente"] or analise.get("cliente_detectado") or "N/A"
    tipo_final    = hints["tipo"] or analise.get("tipo_responsabilidade_detectado") or "N/A"

    trt = analise.get("trt", "N/A")
    if not trt or trt == "N/A":
        trt = extrair_trt_do_processo(analise.get("numero_processo", ""))

    return {
        "DATA DO REGISTRO":           _now_br().strftime("%d/%m/%Y %H:%M"),
        "ADVOGADO":                    sigla,
        "TRT":                         trt,
        "NÚMERO DO PROCESSO":          analise.get("numero_processo", ""),
        "NOME DO RECLAMANTE":          analise.get("nome_reclamante", ""),
        "CLIENTE":                     cliente_final,
        "TIPO DE RESPONSABILIDADE":    tipo_final,
        "TIPO DE DECISÃO":             analise.get("tipo_decisao", ""),
        "RESULTADO DA DECISÃO":        analise.get("resultado_geral", ""),
        "DATA DA DECISÃO":             analise.get("data_decisao", ""),
        "JUIZ/RELATOR":                analise.get("juiz_relator", ""),
        "VARA/TURMA":                  analise.get("vara_turma", ""),
        "ENTENDIMENTOS FAVORÁVEIS":    formatar_entendimentos(analise.get("entendimentos_favoraveis", [])),
        "ENTENDIMENTOS DESFAVORÁVEIS": formatar_entendimentos(analise.get("entendimentos_desfavoraveis", [])),
        "FUNDAMENTOS JURÍDICOS":       analise.get("fundamentos_juridicos", ""),
        "VALOR DA CONDENAÇÃO":         analise.get("valor_condenacao", ""),
        "RESUMO":                      analise.get("resumo_geral", ""),
        "OBSERVAÇÕES":                 analise.get("observacoes_precedente", ""),
        "_cliente_final":              cliente_final,
        "_tipo_final":                 tipo_final,
    }


async def confirmar_sessao(advogado: str, webhook_url: str):
    mensagem, oferecer_email = await confirmar_sessao_data(advogado)
    await send_webhook(webhook_url, mensagem)


async def confirmar_sessao_data(advogado: str) -> tuple[str, bool]:
    chave = _chave_sessao(advogado)
    primeiro = advogado.strip().split()[0]
    sessoes = await carregar_sessoes()
    row = sessoes.get(chave)
    if not row:
        return f"Hmm, {primeiro}, não encontrei nenhuma análise pendente para confirmar. Que tal enviar um novo PDF? 😊", False
    try:
        row_limpo = {k: v for k, v in row.items() if not k.startswith("_")}
        await salvar_decisao(row_limpo)

        # Preserva os dados da decisão para possível geração de e-mail
        dados_email = {k: v for k, v in row.items()}
        del sessoes[chave]

        # Armazena estado aguardando resposta de e-mail
        chave_email = f"email_{chave}"
        sessoes[chave_email] = dados_email
        await salvar_sessoes(sessoes)

        sigla = row.get("ADVOGADO", advogado)
        processo = row.get('NÚMERO DO PROCESSO', 'N/A')
        cliente = row.get('CLIENTE', 'N/A')
        resultado = row.get('RESULTADO DA DECISÃO', 'N/A')
        logger.info("Sessão confirmada para %s", advogado)
        return (
            f"🎉 Perfeito, {primeiro}! Decisão registrada com sucesso na planilha!\n\n"
            f"📄 *Processo:* {processo}\n"
            f"🏢 *Cliente:* {cliente}\n"
            f"📊 *Resultado:* {resultado}\n\n"
            f"_Salvo por {sigla} em {_now_br().strftime('%d/%m/%Y %H:%M')}_\n\n"
            f"Agora, que tal gerar uma sugestão de e-mail para o cliente? ✉️",
            True,
        )
    except Exception as e:
        logger.exception("Erro ao salvar sessão: %s", e)
        return f"Ops, {primeiro}! Tive um probleminha ao salvar na planilha. Erro: `{type(e).__name__}: {str(e)[:150]}`\n\nPode tentar confirmar novamente? 🙏", False


async def cancelar_sessao(advogado: str, webhook_url: str):
    await send_webhook(webhook_url, await cancelar_sessao_data(advogado))


async def cancelar_sessao_data(advogado: str) -> str:
    chave = _chave_sessao(advogado)
    primeiro = advogado.strip().split()[0]
    sessoes = await carregar_sessoes()
    if chave in sessoes:
        del sessoes[chave]
        await salvar_sessoes(sessoes)
        return f"Tudo bem, {primeiro}! Análise descartada, nenhum dado foi salvo. Quando quiser, é só enviar um novo PDF! 😊"
    return f"Hmm, {primeiro}, não encontrei nenhuma análise pendente para cancelar. Tudo certo por aqui! 👍"


async def marcar_aguardando_correcao(advogado: str) -> str:
    chave = _chave_sessao(advogado)
    primeiro = advogado.strip().split()[0]
    sessoes = await carregar_sessoes()
    row = sessoes.get(chave)
    if not row:
        return f"Hmm, {primeiro}, não encontrei nenhuma análise pendente para corrigir. Envie um PDF para começar! 😊"
    row["_aguardando_correcao"] = True
    sessoes[chave] = row
    await salvar_sessoes(sessoes)
    return (
        f"✏️ Claro, {primeiro}! Me diga o que quer corrigir — mencione @Mia Falaw Bot e explique a correção.\n\n"
        f"Exemplos:\n"
        f"• _resultado deve ser Desfavorável_\n"
        f"• _TRT é TRT-2 e vara é 5ª Vara do Trabalho_\n"
        f"• _cliente é Magazine Luiza e tipo é Subsidiária_"
    )


async def esta_aguardando_correcao(advogado: str) -> bool:
    chave = _chave_sessao(advogado)
    sessoes = await carregar_sessoes()
    row = sessoes.get(chave) or {}
    return bool(row.get("_aguardando_correcao"))


async def obter_relatorio_pendente(advogado: str) -> str | None:
    """Retorna o relatório formatado se houver sessão pendente de confirmação.
    Retorna None se não houver sessão, ou se estiver aguardando PDF/correção."""
    chave = _chave_sessao(advogado)
    sessoes = await carregar_sessoes()
    row = sessoes.get(chave)
    if not row:
        return None
    # Ignora sessões que ainda estão em outro estado
    if row.get("_aguardando_correcao"):
        return None
    # Precisa ter pelo menos número de processo para ser uma análise real
    if not row.get("NÚMERO DO PROCESSO") and not row.get("numero_processo"):
        return None
    sigla = resolver_sigla(advogado)
    # Reconstrói o objeto analise a partir da row salva
    analise = {
        "trt":                          row.get("TRT", "N/A"),
        "numero_processo":               row.get("NÚMERO DO PROCESSO", "N/A"),
        "nome_reclamante":               row.get("RECLAMANTE", "N/A"),
        "data_decisao":                  row.get("DATA", "N/A"),
        "tipo_decisao":                  row.get("TIPO", "N/A"),
        "resultado_geral":               row.get("RESULTADO", "N/A"),
        "cliente_detectado":             row.get("CLIENTE", "N/A"),
        "tipo_responsabilidade_detectado": row.get("TIPO RESPONSABILIDADE", "N/A"),
        "juiz_relator":                  row.get("JUIZ/RELATOR", "N/A"),
        "vara_turma":                    row.get("VARA/TURMA", "N/A"),
        "pedidos_deferidos":             row.get("PEDIDOS DEFERIDOS", []),
        "pedidos_indeferidos":           row.get("PEDIDOS INDEFERIDOS", []),
        "entendimentos_favoraveis":      row.get("ENTENDIMENTOS FAVORÁVEIS", []),
        "entendimentos_desfavoraveis":   row.get("ENTENDIMENTOS DESFAVORÁVEIS", []),
        "fundamentos_juridicos":         row.get("FUNDAMENTOS", "N/A"),
        "valor_condenacao":              row.get("VALOR CONDENAÇÃO", "N/A"),
        "deposito_recursal":             row.get("DEPÓSITO RECURSAL", "N/A"),
        "prazo_recursal":                row.get("PRAZO RECURSAL", "N/A"),
        "resumo_geral":                  row.get("RESUMO", "N/A"),
        "nivel_risco":                   row.get("NÍVEL RISCO", "N/A"),
        "recomendacao":                  row.get("RECOMENDAÇÃO", "N/A"),
        "observacoes_precedente":        row.get("OBSERVAÇÕES", "N/A"),
    }
    return formatar_relatorio(analise, sigla)


# ---------------------------------------------------------------------------
# GERAÇÃO DE E-MAIL — /sim e /nao
# ---------------------------------------------------------------------------

PROMPT_EMAIL = """Você é um advogado sênior especializado em direito trabalhista brasileiro, responsável pela comunicação com clientes empresariais.

Redija um e-mail corporativo de reporte de decisão judicial para o cliente, com base nos dados abaixo.
O escritório representa SEMPRE a empresa (reclamada/ré). O cliente é o gestor jurídico ou diretor da empresa.

DADOS DA DECISÃO:
- Tipo de decisão: {tipo_decisao}
- Resultado: {resultado} (do ponto de vista da empresa)
- Número do processo: {numero_processo}
- Reclamante (trabalhador): {reclamante}
- Cliente (empresa representada): {cliente}
- Data da decisão: {data_decisao}
- Vara/Turma: {vara_turma}
- Valor da condenação: {valor_condenacao}
- Resumo da decisão: {resumo}
- Entendimentos favoráveis à empresa: {favoraveis}
- Entendimentos desfavoráveis à empresa: {desfavoraveis}
- Observações/uso como precedente: {observacoes}

ESTRUTURA OBRIGATÓRIA DO E-MAIL (siga esta ordem):

1. SAUDAÇÃO FORMAL
   Prezados Senhores / Prezado(a) [nome do cliente se disponível],

2. APRESENTAÇÃO DO ASSUNTO (1 parágrafo)
   Informe que o escritório vem, por meio deste, comunicar o resultado da decisão judicial proferida no processo acima referenciado.

3. RESULTADO DA DECISÃO (1–2 parágrafos)
   Informe claramente se a decisão foi favorável, desfavorável ou parcialmente favorável à empresa.
   Se favorável: destaque as teses que protegeram a empresa e os pedidos negados ao reclamante.
   Se desfavorável: explique objetivamente o que foi decidido, sem alarmismo, com clareza sobre os impactos.
   Se parcialmente favorável: separe as vitórias das condenações.

4. ASPECTOS RELEVANTES (1 parágrafo)
   Destaque os fundamentos jurídicos principais e os pontos que podem ser úteis como precedente em outros processos.

5. PROVIDÊNCIAS E PRÓXIMOS PASSOS (1 parágrafo)
   Informe claramente o que o escritório fará: recurso, cumprimento, negociação ou monitoramento.
   Se houver condenação: mencione o prazo recursal, o valor do depósito recursal necessário (estimado em 50% da condenação) e custas processuais.
   Se não houver condenação: confirme que não há obrigações financeiras imediatas.

6. ENCERRAMENTO FORMAL
   Coloque-se à disposição para esclarecimentos, com disponibilidade para reunião se necessário.
   Finalize com: "Atenciosamente," seguido de linha em branco para assinatura.

REGRAS DE ESCRITA:
- Português formal e jurídico, sem gírias ou informalidades.
- Parágrafos bem estruturados, sem listas com marcadores ou asteriscos.
- Tom seguro, objetivo e profissional — transmitir que a situação está sendo gerenciada com competência.
- Se a decisão for desfavorável: seja direto mas tranquilizador, ressaltando as medidas que serão tomadas.
- Se a decisão for favorável: celebre discretamente e reforce a qualidade da defesa.
- NÃO use markdown, asteriscos, hashtags ou qualquer formatação especial.
- O e-mail deve ter entre 350 e 500 palavras.

Assunto (já definido, não inclua no corpo): {assunto}

Retorne APENAS o corpo do e-mail, começando pela saudação e terminando em "Atenciosamente,", sem nenhum texto adicional antes ou depois."""


def _montar_assunto_email(row: dict) -> str:
    tipo_decisao = row.get("TIPO DE DECISÃO", "Decisão").upper()
    resultado = row.get("RESULTADO DA DECISÃO", "")
    numero_processo = row.get("NÚMERO DO PROCESSO", "N/A")
    reclamante = row.get("NOME DO RECLAMANTE", "N/A")

    resultado_upper = resultado.upper()
    if "PARCIALMENTE" in resultado_upper:
        resultado_label = "PARCIALMENTE FAVORÁVEL"
    elif "DESFAVORÁVEL" in resultado_upper or "DESFAVORAVEL" in resultado_upper:
        resultado_label = "DESFAVORÁVEL"
    else:
        resultado_label = "FAVORÁVEL"

    return f"{tipo_decisao} – {resultado_label} – PROC Nº {numero_processo} – RECLAMANTE: {reclamante}"


async def gerar_email_sessao(advogado: str, webhook_url: str):
    await send_webhook(webhook_url, await gerar_email_sessao_data(advogado))


async def gerar_email_sessao_data(advogado: str) -> str:
    """Gera o e-mail de reporte ao cliente a partir da sessão confirmada."""
    chave = _chave_sessao(advogado)
    chave_email = f"email_{chave}"
    sessoes = await carregar_sessoes()
    row = sessoes.get(chave_email)

    if not row:
        return (
            f"⚠️ *{advogado}*, não há decisão recente para gerar o e-mail.\n"
            f"Confirme uma decisão com `/confirmar` primeiro."
        )

    try:
        assunto = _montar_assunto_email(row)

        prompt = PROMPT_EMAIL.format(
            tipo_decisao=row.get("TIPO DE DECISÃO", "N/A"),
            resultado=row.get("RESULTADO DA DECISÃO", "N/A"),
            numero_processo=row.get("NÚMERO DO PROCESSO", "N/A"),
            reclamante=row.get("NOME DO RECLAMANTE", "N/A"),
            cliente=row.get("CLIENTE", "N/A"),
            data_decisao=row.get("DATA DA DECISÃO", "N/A"),
            vara_turma=row.get("VARA/TURMA", "N/A"),
            valor_condenacao=row.get("VALOR DA CONDENAÇÃO", "N/A"),
            resumo=row.get("RESUMO", "N/A"),
            favoraveis=row.get("ENTENDIMENTOS FAVORÁVEIS", "N/A") or "Nenhum",
            desfavoraveis=row.get("ENTENDIMENTOS DESFAVORÁVEIS", "N/A") or "Nenhum",
            observacoes=row.get("OBSERVAÇÕES", "N/A"),
            assunto=assunto,
        )

        corpo_email = await _chamar_ia(prompt)

        del sessoes[chave_email]
        await salvar_sessoes(sessoes)

        logger.info("E-mail gerado para %s", advogado)
        return (
            f"📧 *SUGESTÃO DE E-MAIL*\n\n"
            f"*Assunto:* {assunto}\n\n"
            f"{'─' * 40}\n\n"
            f"{corpo_email.strip()}\n\n"
            f"{'─' * 40}\n"
            f"_Sugestão gerada automaticamente. Revise antes de enviar._"
        )

    except Exception as e:
        logger.exception("Erro ao gerar e-mail: %s", e)
        return "⚠️ Erro ao gerar o e-mail. Tente novamente."


async def dispensar_email_sessao(advogado: str, webhook_url: str):
    await send_webhook(webhook_url, await dispensar_email_sessao_data(advogado))


async def dispensar_email_sessao_data(advogado: str) -> str:
    """Descarta a oferta de e-mail da sessão confirmada."""
    chave = _chave_sessao(advogado)
    primeiro = advogado.strip().split()[0]
    chave_email = f"email_{chave}"
    sessoes = await carregar_sessoes()

    if chave_email in sessoes:
        del sessoes[chave_email]
        await salvar_sessoes(sessoes)

    return f"Combinado, {primeiro}! Sem e-mail por enquanto. Qualquer coisa é só chamar! 😊"


# ---------------------------------------------------------------------------
# CORREÇÃO COM IA
# ---------------------------------------------------------------------------

PROMPT_CORRECAO = """Você é especialista em decisões judiciais trabalhistas brasileiras.

CONTEXTO IMPORTANTE:
- Este escritório representa SEMPRE a empresa (reclamada/ré), nunca o trabalhador.
- "Favorável" significa favorável À EMPRESA.
- "Desfavorável" significa desfavorável À EMPRESA.

Abaixo está a análise atual de uma decisão judicial e uma instrução de correção do advogado.
Aplique a correção e retorne a análise COMPLETA corrigida em JSON válido.

ANÁLISE ATUAL:
{analise_atual}

INSTRUÇÃO DE CORREÇÃO DO ADVOGADO:
{instrucao}

Retorne APENAS o JSON completo corrigido com todos os campos, sem markdown, sem explicações.
Mantenha todos os campos que não foram mencionados na correção."""


async def corrigir_sessao(advogado: str, instrucao: str, webhook_url: str):
    ok, mensagem = await corrigir_sessao_data(advogado, instrucao)
    await send_webhook(webhook_url, mensagem)


async def corrigir_sessao_data(advogado: str, instrucao: str) -> tuple[bool, str]:
    chave = _chave_sessao(advogado)
    sessoes = await carregar_sessoes()
    row = sessoes.get(chave)
    if not row:
        return False, f"⚠️ *{advogado}*, não há análise pendente para corrigir."

    try:
        # Monta JSON da análise atual para passar à IA
        analise_atual = {
            "trt": row.get("TRT", ""),
            "numero_processo": row.get("NÚMERO DO PROCESSO", ""),
            "nome_reclamante": row.get("NOME DO RECLAMANTE", ""),
            "data_decisao": row.get("DATA DA DECISÃO", ""),
            "tipo_decisao": row.get("TIPO DE DECISÃO", ""),
            "resultado_geral": row.get("RESULTADO DA DECISÃO", ""),
            "cliente_detectado": row.get("CLIENTE", ""),
            "tipo_responsabilidade_detectado": row.get("TIPO DE RESPONSABILIDADE", ""),
            "juiz_relator": row.get("JUIZ/RELATOR", ""),
            "vara_turma": row.get("VARA/TURMA", ""),
            "entendimentos_favoraveis": row.get("ENTENDIMENTOS FAVORÁVEIS", ""),
            "entendimentos_desfavoraveis": row.get("ENTENDIMENTOS DESFAVORÁVEIS", ""),
            "fundamentos_juridicos": row.get("FUNDAMENTOS JURÍDICOS", ""),
            "valor_condenacao": row.get("VALOR DA CONDENAÇÃO", ""),
            "resumo_geral": row.get("RESUMO", ""),
            "observacoes_precedente": row.get("OBSERVAÇÕES", ""),
        }

        prompt = PROMPT_CORRECAO.format(
            analise_atual=json.dumps(analise_atual, ensure_ascii=False),
            instrucao=instrucao
        )

        raw = await _chamar_ia(prompt)
        analise_corrigida = _parse_json(raw)

        # Preserva cliente/tipo informados pelo advogado (prioridade sobre IA)
        hints = {
            "cliente": row.get("_cliente_hint"),
            "tipo": row.get("_tipo_hint"),
        }

        # Atualiza TRT se necessário
        trt = analise_corrigida.get("trt", "N/A")
        if not trt or trt == "N/A":
            trt = extrair_trt_do_processo(analise_corrigida.get("numero_processo", ""))
        analise_corrigida["trt"] = trt

        cliente_final = hints["cliente"] or analise_corrigida.get("cliente_detectado") or "N/A"
        tipo_final    = hints["tipo"] or analise_corrigida.get("tipo_responsabilidade_detectado") or "N/A"
        analise_corrigida["_cliente_final"] = cliente_final
        analise_corrigida["_tipo_final"]    = tipo_final

        sigla = row.get("ADVOGADO", resolver_sigla(advogado))

        # Atualiza sessão com dados corrigidos
        row_corrigido = _montar_row(analise_corrigida, sigla, hints)
        row_corrigido["_cliente_hint"] = hints["cliente"]
        row_corrigido["_tipo_hint"]    = hints["tipo"]
        row_corrigido["_aguardando_correcao"] = False
        sessoes[chave] = row_corrigido
        await salvar_sessoes(sessoes)

        relatorio  = formatar_relatorio(analise_corrigida, sigla)
        return True, relatorio

    except Exception as e:
        logger.exception("Erro ao corrigir sessão: %s", e)
        return False, "⚠️ Erro ao aplicar correção. Tente novamente."


# ---------------------------------------------------------------------------
# HANDLERS PRINCIPAIS
# ---------------------------------------------------------------------------

async def _analisar_e_aguardar(
    texto_pdf: str,
    advogado: str,
    texto: str,
    webhook_url: str,
    include_confirmacao_text: bool = True,
) -> str:
    sigla  = resolver_sigla(advogado)
    hints  = parse_mensagem(texto)

    if not texto_pdf.strip():
        return "⚠️ Não foi possível extrair texto do PDF."

    analise = await analisar_decisao(texto_pdf, hints["cliente"] or "", hints["tipo"] or "")

    # Atualiza TRT se necessário
    trt = analise.get("trt", "N/A")
    if not trt or trt == "N/A":
        trt = extrair_trt_do_processo(analise.get("numero_processo", ""))
    analise["trt"] = trt

    # Define cliente/tipo finais
    cliente_final = hints["cliente"] or analise.get("cliente_detectado") or "N/A"
    tipo_final    = hints["tipo"] or analise.get("tipo_responsabilidade_detectado") or "N/A"
    analise["_cliente_final"] = cliente_final
    analise["_tipo_final"]    = tipo_final

    # Salva sessão pendente (preserva hints para reuso em correções)
    row = _montar_row(analise, sigla, hints)
    row["_cliente_hint"] = hints["cliente"]
    row["_tipo_hint"]    = hints["tipo"]
    row["_aguardando_correcao"] = False
    chave = _chave_sessao(advogado)
    sessoes = await carregar_sessoes()
    sessoes[chave] = row
    await salvar_sessoes(sessoes)
    logger.info("Sessão pendente criada para %s (chave: %s)", advogado, chave)

    # Retorna relatório + instrução de confirmação
    relatorio = formatar_relatorio(analise, sigla)
    if include_confirmacao_text:
        confirmacao = mensagem_confirmacao(advogado)
        return relatorio + "\n\n" + confirmacao
    return relatorio


async def processar_texto(texto_pdf: str, advogado: str, texto: str, webhook_url: str, include_confirmacao_text: bool = True) -> str:
    return await _analisar_e_aguardar(texto_pdf, advogado, texto, webhook_url, include_confirmacao_text=include_confirmacao_text)


async def processar_texto_chat(
    texto_pdf: str,
    advogado: str,
    cliente: str,
    tipo_responsabilidade: str,
) -> str:
    metadados = ""
    if cliente:
        metadados += f"Cliente: {cliente}\n"
    if tipo_responsabilidade:
        metadados += f"Tipo: {tipo_responsabilidade}"
    return await _analisar_e_aguardar(
        texto_pdf,
        advogado,
        metadados,
        webhook_url="",
        include_confirmacao_text=False,
    )


async def processar_pdf_bytes(pdf_bytes: bytes, advogado: str, texto: str, webhook_url: str) -> str:
    texto_pdf = extrair_texto_pdf(pdf_bytes)
    return await _analisar_e_aguardar(texto_pdf, advogado, texto, webhook_url)


async def processar_pdf(pdf_url: str, advogado: str, texto: str, webhook_url: str) -> str:
    pdf_bytes = await download_pdf(pdf_url)
    texto_pdf = extrair_texto_pdf(pdf_bytes)
    return await _analisar_e_aguardar(texto_pdf, advogado, texto, webhook_url)


# ---------------------------------------------------------------------------
# BUSCA
# ---------------------------------------------------------------------------

PROMPT_BUSCA = """Você é uma advogada trabalhista sênior especializada em defesa empresarial. Sua tarefa é recuperar e analisar precedentes jurídicos relevantes de uma base de dados interna do escritório.

═══════════════════════════════════════════════
PAPEL DO ESCRITÓRIO
═══════════════════════════════════════════════
Este escritório representa EXCLUSIVAMENTE a empresa (reclamada). Portanto:
- Precedente FAVORÁVEL = decisão que protegeu, isentou ou reduziu condenação da empresa.
- Precedente DESFAVORÁVEL = decisão que condenou ou prejudicou a empresa — útil para antecipar riscos e preparar estratégia de defesa.

═══════════════════════════════════════════════
BUSCA SOLICITADA
═══════════════════════════════════════════════
Tipo: {tipo_label}
Tema/Consulta: "{tema}"

═══════════════════════════════════════════════
REGRAS DE IDENTIFICAÇÃO DO TIPO DE BUSCA
═══════════════════════════════════════════════
1. NOME DE CLIENTE/EMPRESA — Se o termo for um nome próprio de empresa (ex: "iFood", "Loft", "Ambev"), filtre prioritariamente pela coluna "CLIENTE" (correspondência parcial, sem distinção de maiúsculas).
2. NÚMERO DE PROCESSO — Se o termo tiver formato de processo (ex: "0001234-56.2023"), filtre pela coluna "NÚMERO DO PROCESSO".
3. NOME DE MAGISTRADO — Se o termo parecer um nome de juiz ou desembargador, filtre pela coluna "JUIZ/RELATOR".
4. TEMA JURÍDICO — Para qualquer outro caso (ex: "horas extras", "dano moral", "terceirização", "vínculo empregatício"), aplique busca semântica nas colunas relevantes:
   - Para FAVORÁVEIS: priorize "ENTENDIMENTOS FAVORÁVEIS" e "OBSERVAÇÕES".
   - Para DESFAVORÁVEIS: priorize "ENTENDIMENTOS DESFAVORÁVEIS" e "OBSERVAÇÕES".
   - Considere também "PEDIDOS INDEFERIDOS" (favoráveis) e "PEDIDOS DEFERIDOS" (desfavoráveis) como fontes secundárias.

═══════════════════════════════════════════════
CRITÉRIOS DE RELEVÂNCIA (aplique na ordem)
═══════════════════════════════════════════════
1. Correspondência direta do tema no texto do entendimento.
2. Mesma matéria jurídica por sinonímia ou expressão equivalente (ex: "jornada" ≈ "horas extras", "dano extrapatrimonial" ≈ "dano moral").
3. Mesmo resultado prático (procedência/improcedência) mesmo que o fundamento seja diferente.
4. Decisões mais recentes têm peso levemente maior.
5. Decisões de TRT ou TST têm peso maior que sentenças de 1ª instância para formação de tese.

Ordene os resultados do mais relevante para o menos relevante.
Inclua no máximo 10 precedentes. Se houver mais, selecione os mais representativos e variados (diferentes TRTs, datas, argumentos).

═══════════════════════════════════════════════
DADOS DA PLANILHA
═══════════════════════════════════════════════
{dados}

═══════════════════════════════════════════════
FORMATO DE RESPOSTA — APENAS JSON VÁLIDO
═══════════════════════════════════════════════
{{
  "tema_buscado": "tema exato informado pelo usuário",
  "tipo": "FAVORÁVEIS ou DESFAVORÁVEIS",
  "total_encontrados": 0,
  "precedentes": [
    {{
      "numero_processo": "número do processo",
      "advogado": "sigla do advogado responsável",
      "cliente": "nome do cliente/empresa",
      "trt": "TRT-X ou TST",
      "data_decisao": "data da decisão",
      "tipo_decisao": "Sentença / Acórdão / Decisão Monocrática",
      "resultado_geral": "resultado resumido (ex: improcedente, parcialmente procedente)",
      "entendimento_relevante": "trecho ou resumo do entendimento diretamente relacionado ao tema buscado — seja específico",
      "fundamento_juridico": "base legal ou súmula citada na decisão (se disponível)",
      "como_usar": "orientação prática e objetiva de como replicar este precedente em novos casos — mencione o argumento central e o contexto ideal de aplicação",
      "relevancia": "ALTA / MÉDIA / BAIXA — justifique brevemente"
    }}
  ],
  "tese_consolidada": "síntese das teses favoráveis/desfavoráveis encontradas, redigida como argumento jurídico coeso que a empresa pode usar em petições",
  "argumentos_principais": ["argumento 1 em uma frase objetiva", "argumento 2", "argumento 3"],
  "alertas": "riscos, contradições entre precedentes ou pontos de atenção que o advogado deve considerar (deixe vazio se não houver)"
}}"""


async def processar_busca(tipo: str, tema: str) -> str:
    if not tema:
        return f"⚠️ Informe o tema. Exemplo: `/{tipo} vínculo empregatício`"
    rows = await buscar_precedentes()
    tema_normalizado = (tema or "").strip().lower()
    if tema_normalizado in {"todos", "todas", "all", "*"}:
        return _formatar_busca_todos(tipo, rows)

    dados_str = json.dumps(rows, ensure_ascii=False)[:25000]
    tipo_label = "FAVORÁVEIS" if tipo == "favoraveis" else "DESFAVORÁVEIS"
    prompt    = PROMPT_BUSCA.format(tipo_label=tipo_label, tema=tema, dados=dados_str)
    raw       = await _chamar_ia(prompt)
    logger.info("Busca raw IA: %s", raw[:500])
    resultado = _parse_json(raw)
    return _formatar_busca(resultado)


def _get(d: dict, *keys, default="N/A"):
    """Tenta múltiplos nomes de chave, retorna o primeiro encontrado."""
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return default


def _formatar_busca_todos(tipo: str, rows: list[dict]) -> str:
    coluna = "ENTENDIMENTOS FAVORÁVEIS" if tipo == "favoraveis" else "ENTENDIMENTOS DESFAVORÁVEIS"
    rotulo = "FAVORÁVEIS" if tipo == "favoraveis" else "DESFAVORÁVEIS"

    filtrados = [r for r in rows if str(r.get(coluna, "")).strip()]
    if not filtrados:
        return f"🔍 *PRECEDENTES {rotulo}*\n\nNenhum precedente encontrado na planilha."

    # Exibe os mais recentes primeiro e limita para evitar mensagens gigantes.
    selecionados = list(reversed(filtrados))[:10]
    r = f"🔍 *PRECEDENTES {rotulo}*\n\n"
    r += f"📌 *Tema:* todos\n"
    r += f"📊 *Encontrados:* {len(filtrados)} (mostrando 10 mais recentes)\n\n"

    for i, row in enumerate(selecionados, 1):
        r += f"{i}. *{row.get('NÚMERO DO PROCESSO', 'N/A')}*\n"
        r += f"   {row.get('CLIENTE', 'N/A')} | {row.get('TRT', 'N/A')} | {row.get('DATA DA DECISÃO', 'N/A')}\n"
        r += f"   _{row.get(coluna, 'N/A')}_\n"
        r += f"   💡 {row.get('OBSERVAÇÕES', 'N/A')}\n\n"

    return r.strip()


def _formatar_busca(d: dict) -> str:
    tipo     = (_get(d, "tipo", default="")).upper()
    icone    = "✅" if "FAVORÁVEIS" in tipo else "⚠️"
    total    = d.get("total_encontrados", 0)

    r  = f"🔍 *PRECEDENTES {tipo}*\n"
    r += f"📌 *Tema:* {_get(d, 'tema_buscado', 'tema')} | {icone} *{total} encontrado(s)*\n"
    r += "─" * 40 + "\n\n"

    for i, p in enumerate(d.get("precedentes") or [], 1):
        processo  = _get(p, "numero_processo", "NÚMERO DO PROCESSO", "numero processo")
        cliente   = _get(p, "cliente", "CLIENTE")
        trt       = _get(p, "trt", "TRT")
        data      = _get(p, "data_decisao", "DATA DA DECISÃO", "data decisao")
        tipo_dec  = _get(p, "tipo_decisao", default="")
        resultado = _get(p, "resultado_geral", default="")
        entend    = _get(p, "entendimento_relevante", "ENTENDIMENTOS FAVORÁVEIS", "ENTENDIMENTOS DESFAVORÁVEIS", "entendimento")
        fund      = _get(p, "fundamento_juridico", default="")
        uso       = _get(p, "como_usar", "observacoes_precedente", "OBSERVAÇÕES")
        relev     = _get(p, "relevancia", default="")

        relevancia_str = f" [{relev}]" if relev and relev != "N/A" else ""
        r += f"*{i}. {processo}*{relevancia_str}\n"
        r += f"   🏢 {cliente} | 🏛️ {trt} | 📅 {data}"
        if tipo_dec and tipo_dec != "N/A":
            r += f" | _{tipo_dec}_"
        r += "\n"
        if resultado and resultado != "N/A":
            r += f"   📋 Resultado: {resultado}\n"
        r += f"   📝 {entend}\n"
        if fund and fund != "N/A":
            r += f"   ⚖️ _Fundamento: {fund}_\n"
        r += f"   💡 *Como usar:* {uso}\n\n"

    r += "─" * 40 + "\n"
    r += f"*📚 Tese Consolidada:*\n{_get(d, 'tese_consolidada')}\n\n"

    argumentos = d.get("argumentos_principais")
    if isinstance(argumentos, list):
        r += "*🎯 Argumentos Principais:*\n"
        for arg in argumentos:
            r += f"• {arg}\n"
    elif argumentos and argumentos != "N/A":
        r += f"*🎯 Argumentos Principais:*\n{argumentos}\n"

    alertas = d.get("alertas", "")
    if alertas and alertas not in ("N/A", "", None):
        r += f"\n⚠️ *Atenção:* _{alertas}_"

    return r


# ---------------------------------------------------------------------------
# AJUDA / LINK
# ---------------------------------------------------------------------------

FORM_LINK = "https://docs.google.com/forms/d/e/1FAIpQLSfrRjaMCnRojpbLVIjWKPKOYew3Mp_PwwaYzogpS9XbOWfzsg/viewform"

def get_ajuda_card() -> dict:
    return {
        "cardId": "ajuda",
        "card": {
            "header": {
                "title": "Decisão FA Bot",
                "subtitle": "Assistente de decisões trabalhistas",
            },
            "sections": [
                {
                    "header": "📎 Registrar uma decisão",
                    "widgets": [
                        {"textParagraph": {"text": "Acesse o formulário, anexe o PDF e envie:"}},
                        {"textParagraph": {"text": f"<a href=\"{FORM_LINK}\">{FORM_LINK}</a>"}},
                        {"decoratedText": {"topLabel": "Após a análise, confirme ou cancele", "text": "<font face=\"monospace\">/confirmar</font> ou <font face=\"monospace\">/cancelar</font>", "startIcon": {"knownIcon": "BOOKMARK"}}},
                    ]
                },
                {
                    "header": "📧 E-mail de reporte ao cliente",
                    "widgets": [
                        {"decoratedText": {"text": "<font face=\"monospace\">/sim</font>", "bottomLabel": "Gera sugestão de e-mail após /confirmar", "startIcon": {"knownIcon": "EMAIL"}}},
                        {"decoratedText": {"text": "<font face=\"monospace\">/nao</font>", "bottomLabel": "Dispensa a sugestão de e-mail", "startIcon": {"knownIcon": "EMAIL"}}},
                    ]
                },
                {
                    "header": "🔍 Buscar precedentes",
                    "widgets": [
                        {"decoratedText": {"text": "<font face=\"monospace\">/favoraveis [tema]</font>", "bottomLabel": "Ex: /favoraveis vínculo empregatício", "startIcon": {"knownIcon": "STAR"}}},
                        {"decoratedText": {"text": "<font face=\"monospace\">/desfavoraveis [tema]</font>", "bottomLabel": "Ex: /desfavoraveis responsabilidade subsidiária", "startIcon": {"knownIcon": "STAR"}}},
                        {"decoratedText": {"text": "<font face=\"monospace\">/link</font>", "bottomLabel": "Link do formulário", "startIcon": {"knownIcon": "STAR"}}},
                        {"decoratedText": {"text": "<font face=\"monospace\">/confirmar</font>", "bottomLabel": "Confirma e salva a análise na planilha", "startIcon": {"knownIcon": "STAR"}}},
                        {"decoratedText": {"text": "<font face=\"monospace\">/cancelar</font>", "bottomLabel": "Descarta a análise pendente", "startIcon": {"knownIcon": "STAR"}}},
                        {"decoratedText": {"text": "<font face=\"monospace\">/ajuda</font>", "bottomLabel": "Exibe esta mensagem", "startIcon": {"knownIcon": "STAR"}}},
                    ]
                }
            ]
        },
        "_fallback_text": (
            f"*Decisão FA Bot*\n\n"
            f"📎 Registrar: {FORM_LINK}\n\n"
            f"Após análise: `/confirmar` para salvar · `/cancelar` para descartar\n"
            f"Após confirmar: `/sim` para e-mail de reporte · `/nao` para dispensar\n\n"
            f"🔍 `/favoraveis [tema]` · `/desfavoraveis [tema]` · `/link` · `/ajuda`"
        )
    }


def get_ajuda() -> str:
    return (
        f"*Decisão FA Bot* — Como usar:\n\n"
        f"📎 *Registrar decisão:* {FORM_LINK}\n\n"
        f"Após a análise:\n"
        f"`/confirmar` — salva na planilha\n"
        f"`/cancelar` — descarta\n\n"
        f"📧 *Após confirmar:*\n"
        f"`/sim` — gera sugestão de e-mail de reporte ao cliente\n"
        f"`/nao` — dispensa o e-mail\n\n"
        f"🔍 *Buscar:*\n"
        f"`/favoraveis [tema]`\n"
        f"`/desfavoraveis [tema]`\n"
        f"`/link` · `/ajuda`"
    )


def get_link() -> str:
    return (
        f"📎 *Link para registrar decisão:*\n\n"
        f"{FORM_LINK}\n\n"
        f"_Anexe o PDF, informe o cliente e tipo (opcionais) e envie._"
    )
