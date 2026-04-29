"""
Handlers v3 — com confirmação humana antes de salvar na planilha.
Sessões armazenadas em memória por advogado (chave = nome do advogado).
"""

import asyncio
import io
import json
import logging
import re
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone, timedelta
import pdfplumber

_TZ_BRASILIA = timezone(timedelta(hours=-3))

def _now_br() -> datetime:
    return datetime.now(_TZ_BRASILIA)

from bot.sheets import (
    salvar_decisao, buscar_precedentes,
    carregar_sessoes, salvar_sessoes,
    get_sessao, set_sessao, del_sessao, patch_sessao, move_sessao,
)
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
_GITHUB_MAX_CHARS = 28000  # aumentado; truncação inteligente (início+fim) evita perder dados

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
        # Truncagem inteligente: pega primeiro bloco + último bloco para não perder dados de assinatura/data
        if len(prompt) > _GITHUB_MAX_CHARS:
            metade = _GITHUB_MAX_CHARS // 2
            prompt_github = prompt[:metade] + "\n\n[...trecho intermediário omitido...]\n\n" + prompt[-metade:]
            logger.warning("Prompt truncado de %d para %d chars para GitHub Copilot.", len(prompt), _GITHUB_MAX_CHARS)
        else:
            prompt_github = prompt
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
    "valor_causa": "valor da causa em reais (ex: R$ 15.430,00) se constar expressamente no documento, senão N/A",
  "valor_condenacao": "valor líquido em reais (ex: R$ 15.430,00) ou N/A se favorável",
    "deposito_recursal": "N/A — o sistema calculará a recomendação final conforme a tabela vigente do TST",
    "prazo_recursal": "prazo para recurso se constar ou for claramente inferível, senão N/A",
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
    # Tentativa 1: JSON perfeito
    try:
        m = re.search(r"\{[\s\S]*\}", raw)
        if m:
            return json.loads(m.group(0))
    except Exception:
        pass
    # Tentativa 2: JSON truncado — tenta reparar
    try:
        from json_repair import repair_json
        text = raw.strip()
        # Pega do primeiro { até o fim (mesmo que incompleto)
        start = text.find("{")
        if start != -1:
            partial = text[start:]
            repaired = repair_json(partial, return_objects=True)
            if isinstance(repaired, dict) and repaired:
                logger.info("[JSON] Reparado com json-repair OK")
                return repaired
    except Exception as e:
        logger.warning("[JSON] json-repair falhou: %s", e)
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

    r = f"<b>{icone_resultado} ANÁLISE CONCLUÍDA — aguardando confirmação</b><br><br>"

    r += "<b>Identificação</b><br>"
    r += f"📋 <b>{d.get('tipo_decisao', 'N/A')}</b> — {icone_resultado} {resultado}<br>"
    r += f"🏛️ <b>TRT:</b> {d.get('trt', 'N/A')}<br>"
    r += f"📄 <b>Processo:</b> {d.get('numero_processo', 'N/A')}<br>"
    r += f"👤 <b>Reclamante:</b> {d.get('nome_reclamante', 'N/A')}<br>"
    r += f"🏢 <b>Cliente:</b> {d.get('_cliente_final', 'N/A')}<br>"
    r += f"⚖️ <b>Tipo:</b> {d.get('_tipo_final', 'N/A')}<br>"
    r += f"📅 <b>Data:</b> {d.get('data_decisao', 'N/A')}<br>"
    r += f"👨‍⚖️ <b>Juiz/Relator:</b> {d.get('juiz_relator', 'N/A')}<br>"
    r += f"🏠 <b>Vara/Turma:</b> {d.get('vara_turma', 'N/A')}<br>"
    r += f"💰 <b>Valor:</b> {d.get('valor_condenacao', 'N/A')}<br>"

    if d.get('deposito_recursal') and d.get('deposito_recursal') != 'N/A':
        r += f"🏦 <b>Depósito Recursal:</b> {d.get('deposito_recursal')}<br>"
    if d.get('prazo_recursal') and d.get('prazo_recursal') != 'N/A':
        r += f"⏰ <b>Prazo Recursal:</b> {d.get('prazo_recursal')}<br>"

    r += "<br><b>Resumo Executivo</b><br>"
    r += f"{d.get('resumo_geral', 'N/A')}<br><br>"

    if nivel_risco:
        r += f"{icone_risco} <b>Nível de Risco:</b> {nivel_risco}<br>"
    if recomendacao:
        rec_icones = {'RECORRER': '⚡', 'AGUARDAR': '⏳', 'CUMPRIR': '✅', 'NEGOCIAR': '🤝'}
        icone_rec = rec_icones.get(recomendacao.split()[0].upper() if recomendacao else '', '📌')
        r += f"{icone_rec} <b>Recomendação:</b> {recomendacao}<br>"
    r += "<br>"

    # Pedidos indeferidos (vitórias)
    pedidos_ind = d.get('pedidos_indeferidos') or []
    if isinstance(pedidos_ind, list) and pedidos_ind:
        r += "<b>🏆 Pedidos Indeferidos (vitórias da empresa)</b><br>"
        for item in pedidos_ind:
            r += f"• {item}<br>"
        r += "<br>"

    # Pedidos deferidos
    pedidos_def = d.get('pedidos_deferidos') or []
    if isinstance(pedidos_def, list) and pedidos_def:
        r += "<b>⚠️ Pedidos Deferidos (contra a empresa)</b><br>"
        for item in pedidos_def:
            r += f"• {item}<br>"
        r += "<br>"

    # Entendimentos favoráveis
    favs = d.get('entendimentos_favoraveis') or []
    if isinstance(favs, list) and favs:
        r += "<b>✅ Entendimentos Favoráveis</b><br>"
        for i, e in enumerate(favs, 1):
            if isinstance(e, dict):
                r += f"{i}. <b>{e.get('tema','')}:</b> {e.get('entendimento','')}<br>"
                if e.get('uso_como_precedente'):
                    r += f"<i>↳ {e.get('uso_como_precedente')}</i><br>"
        r += "<br>"

    # Entendimentos desfavoráveis
    desfavs = d.get('entendimentos_desfavoraveis') or []
    if isinstance(desfavs, list) and desfavs:
        r += "<b>❌ Entendimentos Desfavoráveis</b><br>"
        for i, e in enumerate(desfavs, 1):
            if isinstance(e, dict):
                r += f"{i}. <b>{e.get('tema','')}:</b> {e.get('entendimento','')}<br>"
                if e.get('estrategia_recursal'):
                    r += f"<i>↳ Estratégia: {e.get('estrategia_recursal')}</i><br>"
        r += "<br>"

    r += f"<b>📚 Fundamentos Jurídicos:</b> {d.get('fundamentos_juridicos', 'N/A')}<br><br>"
    r += f"<b>📌 Uso como Precedente:</b><br>{d.get('observacoes_precedente', 'N/A')}<br><br>"
    r += f"<i>Analisado por {sigla} em {_now_br().strftime('%d/%m/%Y %H:%M')}</i>"
    return r


TETO_RO_2025 = Decimal("13813.83")
TETO_RR_2025 = Decimal("27627.66")


def _parse_brl_value(texto: str | None) -> Decimal | None:
    if not texto:
        return None
    m = re.search(r"R\$\s*([\d\.]+,\d{2})", str(texto))
    if not m:
        return None
    try:
        return Decimal(m.group(1).replace('.', '').replace(',', '.'))
    except (InvalidOperation, AttributeError):
        return None


def _format_brl_value(valor: Decimal) -> str:
    s = f"{valor:.2f}"
    inteiro, frac = s.split('.')
    grupos = []
    while inteiro:
        grupos.append(inteiro[-3:])
        inteiro = inteiro[:-3]
    return f"R$ {'.'.join(reversed(grupos))},{frac}"


def _extrair_valor_causa(texto: str) -> Decimal | None:
    m = re.search(r"valor\s+da\s+causa[^\n\r:]*[:\s]+R\$\s*([\d\.]+,\d{2})", texto, re.IGNORECASE)
    if not m:
        return None
    try:
        return Decimal(m.group(1).replace('.', '').replace(',', '.'))
    except InvalidOperation:
        return None


def _definir_regras_recursais(analise: dict, texto_pdf: str):
    texto_base = " ".join([
        str(analise.get("tipo_decisao", "")),
        str(analise.get("fundamentos_juridicos", "")),
        texto_pdf[:5000],
    ]).lower()

    valor_causa = _extrair_valor_causa(texto_pdf)
    valor_condenacao = _parse_brl_value(analise.get("valor_condenacao"))
    trt = str(analise.get("trt", "") or "")

    eh_rr_ou_tst = (
        "recurso de revista" in texto_base
        or "embargos" in texto_base
        or trt.upper() == "TST"
        or (analise.get("tipo_decisao") == "Acórdão" and trt.upper().startswith("TRT-"))
    )

    if eh_rr_ou_tst:
        analise["deposito_recursal"] = (
            "Verificar se já há depósito recursal no processo e se há necessidade de preparo recursal complementar "
            f"(teto TST vigente para RR/Embargos/Ação Rescisória: {_format_brl_value(TETO_RR_2025)})."
        )
        analise["prazo_recursal"] = analise.get("prazo_recursal") or "8 dias úteis"
        if valor_causa is not None:
            analise["valor_causa"] = _format_brl_value(valor_causa)
        return

    teto = TETO_RO_2025
    base = None
    base_label = None
    if valor_causa is not None and valor_causa < teto:
        base = valor_causa
        base_label = "valor da causa"
    elif valor_condenacao is not None and valor_condenacao < teto:
        base = valor_condenacao
        base_label = "valor da condenação"
    else:
        base = teto
        base_label = "teto vigente do TST"

    analise["deposito_recursal"] = f"{_format_brl_value(base)} — recomendado com base no {base_label}."
    analise["prazo_recursal"] = analise.get("prazo_recursal") or "8 dias úteis"
    if valor_causa is not None:
        analise["valor_causa"] = _format_brl_value(valor_causa)


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
    # Atomicamente move sessão pendente → sessão de e-mail (sem race condition)
    row = await move_sessao(chave, f"email_{chave}")
    if not row:
        return f"Hmm, {primeiro}, não encontrei nenhuma análise pendente para confirmar. Que tal enviar um novo PDF? 😊", False
    
    row_limpo = {k: v for k, v in row.items() if not k.startswith("_")}
    
    try:
        await salvar_decisao(row_limpo)
        sucesso_sheets = True
        logger.info("Decisão salva na Sheets para %s", advogado)
    except PermissionError as e:
        sucesso_sheets = False
        logger.warning("PermissionError ao salvar na Sheets (API não habilitada?): %s", e)
    except Exception as e:
        sucesso_sheets = False
        logger.exception("Erro ao salvar na Sheets: %s", e)

    sigla = row.get("ADVOGADO", advogado)
    processo = row.get('NÚMERO DO PROCESSO', 'N/A')
    cliente = row.get('CLIENTE', 'N/A')
    resultado = row.get('RESULTADO DA DECISÃO', 'N/A')
    logger.info("Sessão confirmada para %s (Sheets: %s)", advogado, sucesso_sheets)
    
    if sucesso_sheets:
        return (
            f"🎉 Perfeito, {primeiro}! Decisão registrada com sucesso na planilha!\n\n"
            f"📄 *Processo:* {processo}\n"
            f"🏢 *Cliente:* {cliente}\n"
            f"📊 *Resultado:* {resultado}\n\n"
            f"_Salvo por {sigla} em {_now_br().strftime('%d/%m/%Y %H:%M')}_\n\n"
            f"Agora, que tal gerar uma sugestão de e-mail para o cliente? ✉️",
            True,
        )
    else:
        return (
            f"⚠️ Análise registrada (em memória), mas não pude salvar na planilha.\n\n"
            f"📄 *Processo:* {processo}\n"
            f"🏢 *Cliente:* {cliente}\n"
            f"📊 *Resultado:* {resultado}\n\n"
            f"_Motivo: Sheets API não habilitada no projeto. Peça ao admin para habilitar em: https://console.developers.google.com/apis/api/sheets.googleapis.com/overview?project=98905599157_\n\n"
            f"Quer gerar e-mail mesmo assim? ✉️",
            True,
        )


async def cancelar_sessao(advogado: str, webhook_url: str):
    await send_webhook(webhook_url, await cancelar_sessao_data(advogado))


async def cancelar_sessao_data(advogado: str) -> str:
    chave = _chave_sessao(advogado)
    primeiro = advogado.strip().split()[0]
    existing = await get_sessao(chave)
    if existing is not None:
        await del_sessao(chave)
        return f"Tudo bem, {primeiro}! Análise descartada, nenhum dado foi salvo. Quando quiser, é só enviar um novo PDF! 😊"
    return f"Hmm, {primeiro}, não encontrei nenhuma análise pendente para cancelar. Tudo certo por aqui! 👍"


async def marcar_aguardando_correcao(advogado: str) -> str:
    chave = _chave_sessao(advogado)
    primeiro = advogado.strip().split()[0]
    row = await get_sessao(chave)
    if not row:
        return f"Hmm, {primeiro}, não encontrei nenhuma análise pendente para corrigir. Envie um PDF para começar! 😊"
    await patch_sessao(chave, {"_aguardando_correcao": True})
    return (
        f"✏️ Claro, {primeiro}! Me diga o que quer corrigir — mencione @Mia Falaw Bot e explique a correção.\n\n"
        f"Exemplos:\n"
        f"• _resultado deve ser Desfavorável_\n"
        f"• _TRT é TRT-2 e vara é 5ª Vara do Trabalho_\n"
        f"• _cliente é Magazine Luiza e tipo é Subsidiária_"
    )


async def esta_aguardando_correcao(advogado: str) -> bool:
    chave = _chave_sessao(advogado)
    row = await get_sessao(chave) or {}
    return bool(row.get("_aguardando_correcao"))


async def obter_relatorio_pendente(advogado: str) -> str | None:
    """Retorna o relatório formatado se houver sessão pendente de confirmação.
    Retorna None se não houver sessão, ou se estiver aguardando PDF/correção."""
    chave = _chave_sessao(advogado)
    row = await get_sessao(chave)
    if not row:
        return None
    if row.get("_aguardando_correcao"):
        return None
    if not row.get("NÚMERO DO PROCESSO") and not row.get("numero_processo"):
        return None
    sigla = resolver_sigla(advogado)

    # Prefere a análise completa salva diretamente (garante todos os campos)
    analise = row.get("_analise_raw")
    if analise:
        return formatar_relatorio(analise, sigla)

    # Fallback para sessões antigas: reconstrói com as chaves corretas da planilha
    analise = {
        "trt":                             row.get("TRT", "N/A"),
        "numero_processo":                  row.get("NÚMERO DO PROCESSO", "N/A"),
        "nome_reclamante":                  row.get("NOME DO RECLAMANTE", "N/A"),
        "data_decisao":                     row.get("DATA DA DECISÃO", "N/A"),
        "tipo_decisao":                     row.get("TIPO DE DECISÃO", "N/A"),
        "resultado_geral":                  row.get("RESULTADO DA DECISÃO", "N/A"),
        "cliente_detectado":                row.get("CLIENTE", "N/A"),
        "tipo_responsabilidade_detectado":  row.get("TIPO DE RESPONSABILIDADE", "N/A"),
        "juiz_relator":                     row.get("JUIZ/RELATOR", "N/A"),
        "vara_turma":                       row.get("VARA/TURMA", "N/A"),
        "pedidos_deferidos":                row.get("pedidos_deferidos", []),
        "pedidos_indeferidos":              row.get("pedidos_indeferidos", []),
        "entendimentos_favoraveis":         row.get("ENTENDIMENTOS FAVORÁVEIS", []),
        "entendimentos_desfavoraveis":      row.get("ENTENDIMENTOS DESFAVORÁVEIS", []),
        "fundamentos_juridicos":            row.get("FUNDAMENTOS JURÍDICOS", "N/A"),
        "valor_condenacao":                 row.get("VALOR DA CONDENAÇÃO", "N/A"),
        "deposito_recursal":                row.get("deposito_recursal", "N/A"),
        "prazo_recursal":                   row.get("prazo_recursal", "N/A"),
        "resumo_geral":                     row.get("RESUMO", "N/A"),
        "nivel_risco":                      row.get("nivel_risco", "N/A"),
        "recomendacao":                     row.get("recomendacao", "N/A"),
        "observacoes_precedente":           row.get("OBSERVAÇÕES", "N/A"),
        "_cliente_final":                   row.get("_cliente_final") or row.get("CLIENTE", "N/A"),
        "_tipo_final":                      row.get("_tipo_final") or row.get("TIPO DE RESPONSABILIDADE", "N/A"),
    }
    return formatar_relatorio(analise, sigla)


# ---------------------------------------------------------------------------
# GERAÇÃO DE E-MAIL — /sim e /nao
# ---------------------------------------------------------------------------

PROMPT_EMAIL = """Você é um advogado sênior especializado em direito trabalhista brasileiro, responsável pela comunicação com clientes empresariais.

Redija um e-mail corporativo estruturado seguindo EXATAMENTE o formato dos templates profissionais da firma.

DADOS DA DECISÃO:
- Tipo de decisão: {tipo_decisao}
- Resultado: {resultado} (do ponto de vista da empresa)
- Número do processo: {numero_processo}
- Reclamante (trabalhador): {reclamante}
- Cliente (empresa representada): {cliente}
- Data da decisão: {data_decisao}
- Vara/Turma: {vara_turma}
- Valor da condenação: {valor_condenacao}
- Custas processuais: {custas}
- Recomendação estratégica: {recomendacao}
- Resumo da decisão: {resumo}
- Entendimentos favoráveis à empresa: {favoraveis}
- Entendimentos desfavoráveis à empresa: {desfavoraveis}

ESTRUTURA OBRIGATÓRIA (exatamente nesta ordem):

1. SAUDAÇÃO
Olá, Pessoal.

Tudo bem?

2. METADATA (cada linha em negrito, seguindo padrão: *Label:* Valor)
*Processo:* {numero_processo}
*Reclamante:* {reclamante}
*Cliente:* {cliente}
*Vara/Turma:* {vara_turma}

3. DISPOSITIVO
*Dispositivo:*
[Parágrafo resumindo a decisão: favorável/desfavorável/parcial para a empresa]

4. FAVORÁVEIS
*Favoráveis (para a empresa):*
[Numerada lista 1-5+ com formato: 1. [Pedido/Tese]: descrição objetiva]

5. DESFAVORÁVEIS (se houver)
*Desfavoráveis (para a empresa):*
[Numerada lista com mesmo formato acima]

6. ENCERRAMENTO
Atenciosamente,

FORMATO DE LISTAS:
- Use números (1, 2, 3...) seguido de ponto e espaço
- NÃO use a palavra "Tópico"
- Cada item deve citar o pedido/tese decidido e a conclusão correspondente
- Um item por linha

REGRAS:
- Use *asteriscos* para negrito: *texto* = negrito
- Cada seção em linha separada
- Português formal, profissional
- Descreva apenas os fundamentos principais
- Se favorável: celebre discretamente a qualidade da defesa
- Se desfavorável: seja objetivo e tranquilizador
- Se desfavorável à empresa: inclua obrigatoriamente menção ao valor da condenação e às custas (ou "não identificadas" se ausentes) e recomende a interposição de recurso com justificativa breve

Retorne APENAS o corpo do e-mail neste formato exato, começando em "Olá, Pessoal." e terminando em "Atenciosamente,"."""


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


def _formatar_email_html(corpo_email: str, assunto: str, row: dict) -> str:
    """
    Formata o corpo do e-mail com HTML estruturado.
    Processa formato com *asteriscos* para negrito e estrutura de listas numeradas.
    Segue padrão dos templates profissionais da firma.
    """
    html = "<b>📧 SUGESTÃO DE E-MAIL</b><br><br>"
    
    # Metadata do caso
    html += "<b>Processo:</b> " + row.get("NÚMERO DO PROCESSO", "N/A") + "<br>"
    html += "<b>Reclamante:</b> " + row.get("NOME DO RECLAMANTE", "N/A") + "<br>"
    html += "<b>Cliente:</b> " + row.get("CLIENTE", "N/A") + "<br>"
    html += "<b>Vara/Turma:</b> " + row.get("VARA/TURMA", "N/A") + "<br>"
    html += "<b>Data da decisão:</b> " + row.get("DATA DA DECISÃO", "N/A") + "<br>"
    
    valor = row.get("VALOR DA CONDENAÇÃO", "N/A")
    if valor and valor != "N/A" and valor.strip():
        html += "<b>Valor:</b> " + valor + "<br>"
    
    html += "<br><b>───────────────────────────────────────────</b><br><br>"
    
    # Normaliza rótulos para manter padrão interno esperado.
    corpo_email = re.sub(r'(?im)^\*?resumo:\*?\s*$', '*Dispositivo:*', corpo_email)

    # Processa linhas do corpo do email
    lines = corpo_email.split('\n')
    for i, line in enumerate(lines):
        stripped = line.strip()
        
        # Pula linhas vazias mantendo espaçamento
        if not stripped:
            if i > 0 and i < len(lines) - 1:  # Não adiciona br no final
                html += "<br>"
            continue
        
        # Seções em negrito com asteriscos: *Seção:* → <b>Seção:</b>
        if stripped.startswith('*') and stripped.endswith('*'):
            # Remove asteriscos
            titulo = stripped.replace('*', '').strip()
            html += f'<b>{titulo}</b><br>'
        elif re.match(r'^\*[^*]+:\*', stripped):
            # Formato: *Label:* valor → <b>Label:</b> valor
            # Substitui *texto:* por <b>texto:</b>
            formatted = stripped
            # Encontra e substitui *...:* por <b>...</b>
            formatted = re.sub(r'\*([^*]+):\*', r'<b>\1:</b>', formatted)
            # Se houver mais asteriscos (conteúdo em negrito dentro), substitui
            formatted = re.sub(r'\*([^*]+)\*', r'<b>\1</b>', formatted)
            html += f'{formatted}<br>'
        elif re.match(r'^\d+\.\s', stripped):
            # Itens numerados: remove "Tópico:" quando vier da IA e mantém pedido/tese direto.
            # Substitui *...:* por <b>...</b> e mantém numeração
            formatted = re.sub(r'^(\d+\.\s*)\*?[Tt][óo]pico\*?:\s*', r'\1', stripped)
            formatted = re.sub(r'\*([^*]+):\*', r'<b>\1:</b>', formatted)
            # Se houver mais asteriscos, substitui por bold
            formatted = re.sub(r'\*([^*]+)\*', r'<b>\1</b>', formatted)
            html += f'{formatted}<br>'
        else:
            # Parágrafo normal com possível negrito
            formatted = stripped
            # Substitui *texto* por <b>texto</b>
            formatted = re.sub(r'\*([^*]+)\*', r'<b>\1</b>', formatted)
            html += f'{formatted}<br>'

    # Garante bloco estratégico mínimo para decisões desfavoráveis.
    resultado = (row.get("RESULTADO DA DECISÃO") or "").lower()
    if "desfavor" in resultado:
        custas = row.get("CUSTAS") or "Não identificadas na decisão"
        recomendacao = (
            row.get("recomendacao")
            or (row.get("_analise_raw") or {}).get("recomendacao")
            or "Recorrer"
        )
        html += "<br><b>Recomendação estratégica:</b> Diante do resultado desfavorável à empresa, recomenda-se a interposição do recurso cabível.<br>"
        html += f"<b>Valor da condenação:</b> {row.get('VALOR DA CONDENAÇÃO', 'N/A') or 'N/A'}<br>"
        html += f"<b>Custas processuais:</b> {custas}<br>"
        html += f"<b>Diretriz recursal:</b> {recomendacao}<br>"
    
    html += "<br><b>───────────────────────────────────────────</b><br><br>"
    html += f"<b>Assunto:</b><br><i>{assunto}</i><br><br>"
    html += "<i>Sugestão gerada automaticamente. Revise antes de enviar.</i>"
    
    return html


async def gerar_email_sessao_data(advogado: str) -> str:
    """Gera o e-mail de reporte ao cliente a partir da sessão confirmada."""
    chave = _chave_sessao(advogado)
    chave_email = f"email_{chave}"
    row = await get_sessao(chave_email)

    if not row:
        return (
            f"⚠️ {advogado}, não há decisão recente para gerar o e-mail.<br>"
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
            custas=row.get("CUSTAS", "Não identificadas na decisão") or "Não identificadas na decisão",
            recomendacao=(
                row.get("recomendacao")
                or (row.get("_analise_raw") or {}).get("recomendacao")
                or "Recorrer"
            ),
            resumo=row.get("RESUMO", "N/A"),
            favoraveis=row.get("ENTENDIMENTOS FAVORÁVEIS", "N/A") or "Nenhum",
            desfavoraveis=row.get("ENTENDIMENTOS DESFAVORÁVEIS", "N/A") or "Nenhum",
            observacoes=row.get("OBSERVAÇÕES", "N/A"),
            assunto=assunto,
        )

        corpo_email = await _chamar_ia(prompt)
        
        # Formata o e-mail com HTML estruturado
        email_html = _formatar_email_html(corpo_email, assunto, row)

        await del_sessao(chave_email)

        logger.info("E-mail gerado para %s", advogado)
        return email_html

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
    await del_sessao(chave_email)
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
    row = await get_sessao(chave)
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
        row_corrigido["_analise_raw"]  = analise_corrigida
        await set_sessao(chave, row_corrigido)

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
    _definir_regras_recursais(analise, texto_pdf)

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
    row["_analise_raw"]  = analise  # análise completa para reconstruir relatório fiel
    chave = _chave_sessao(advogado)
    await set_sessao(chave, row)
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
