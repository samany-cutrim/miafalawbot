"""
Handlers v3 — lógica de negócio idêntica ao v2.
Diferença: download do PDF usa a URL já autenticada enviada pelo Apps Script.
"""

import io
import json
import logging
import re
from datetime import datetime

import anthropic
import pdfplumber

from bot.sheets import salvar_decisao, buscar_precedentes
from bot.config import ANTHROPIC_API_KEY, COLUNAS

import httpx

logger = logging.getLogger(__name__)
claude = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)


# ---------------------------------------------------------------------------
# MAPA DE SIGLAS
# ---------------------------------------------------------------------------

SIGLAS: dict[str, str] = {
    "Samany Cutrim":                      "SC",
    "Letícia Silva":                      "LSS",
    "Pollyanna Rodrigues Godoy Dias":     "PGD",
    "Tatiana G. Ferraz Andrade":          "TGFA",
    "Natany Valentim Gonçalves":          "NVG",
    "Fernando Attilio Trevisan Júnior":   "FAT",
    "Camilla Mele Martinez":              "CMM",
    "Beatriz Agar Domingues da Silva":    "BADS",
    "Lilian Missora Matsumoto":           "LMM",
    "Indyara Tomé de Brito":              "ITB",
}

def resolver_sigla(display_name: str) -> str:
    if display_name in SIGLAS:
        return SIGLAS[display_name]
    lower = display_name.strip().lower()
    for nome, sigla in SIGLAS.items():
        if nome.lower() == lower:
            return sigla
    for nome, sigla in SIGLAS.items():
        partes = nome.lower().split()
        if all(p in lower for p in partes[:2]):
            return sigla
    partes = display_name.strip().split()
    sigla_auto = "".join(p[0].upper() for p in partes if p)
    logger.warning("Advogado '%s' não encontrado no mapa. Sigla: '%s'", display_name, sigla_auto)
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
# DOWNLOAD DO PDF
# ---------------------------------------------------------------------------

async def download_pdf(url: str) -> bytes:
    """
    Baixa o PDF usando a URL enviada pelo Apps Script.
    O Apps Script já inclui o token OAuth na URL ou nos headers — 
    aqui usamos a URL direta (o Apps Script gera uma URL temporária autenticada).
    """
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.content


# ---------------------------------------------------------------------------
# EXTRAÇÃO DE TEXTO
# ---------------------------------------------------------------------------

def extrair_texto_pdf(pdf_bytes: bytes) -> str:
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            return "\n".join(p.extract_text() or "" for p in pdf.pages)
    except Exception as e:
        logger.exception("Erro ao extrair PDF: %s", e)
        return ""


# ---------------------------------------------------------------------------
# ANÁLISE COM CLAUDE
# ---------------------------------------------------------------------------

PROMPT_ANALISE = """Você é especialista em decisões judiciais trabalhistas brasileiras.

Analise a decisão abaixo e retorne APENAS um JSON válido com os campos:
{{
  "trt": "TRT-X ou N/A",
  "numero_processo": "0000000-00.0000.0.00.0000 ou N/A",
  "nome_reclamante": "nome ou N/A",
  "data_decisao": "DD/MM/AAAA ou N/A",
  "tipo_decisao": "Sentença ou Acórdão",
  "resultado_geral": "Favorável ou Desfavorável ou Parcialmente Favorável",
  "cliente_detectado": "nome do réu/reclamado principal ou N/A",
  "tipo_responsabilidade_detectado": "OL ou Nuvem ou Terceirização ou Subsidiária ou Ex Funcionário ou Ex-Foodlovers ou Marketplace ou N/A",
  "entendimentos_favoraveis": [{{"tema": "", "entendimento": ""}}],
  "entendimentos_desfavoraveis": [{{"tema": "", "entendimento": ""}}],
  "fundamentos_juridicos": "principais fundamentos citados",
  "valor_condenacao": "R$ 0,00 ou N/A",
  "resumo_geral": "resumo em 3-5 linhas",
  "observacoes_precedente": "relevância como precedente"
}}

CLIENTE INFORMADO PELO ADVOGADO: {cliente}
TIPO INFORMADO PELO ADVOGADO: {tipo}

DECISÃO:
{texto}

RETORNE APENAS JSON VÁLIDO, sem markdown, sem explicações."""


async def analisar_decisao(texto: str, cliente_hint: str, tipo_hint: str) -> dict:
    prompt = PROMPT_ANALISE.format(
        cliente=cliente_hint or "Não informado — detecte da decisão",
        tipo=tipo_hint or "Não informado — detecte da decisão",
        texto=texto[:40000],
    )
    response = await claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    return _parse_json(response.content[0].text)


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
        "tipo_responsabilidade_detectado", "entendimentos_favoraveis",
        "entendimentos_desfavoraveis", "fundamentos_juridicos",
        "valor_condenacao", "resumo_geral", "observacoes_precedente",
    ]}


# ---------------------------------------------------------------------------
# FORMATAÇÃO
# ---------------------------------------------------------------------------

def formatar_relatorio(d: dict, sigla: str) -> str:
    r = "✅ *DECISÃO REGISTRADA*\n\n"
    r += f"📋 *{d.get('tipo_decisao', 'N/A')}* — {d.get('resultado_geral', 'N/A')}\n"
    r += f"🏛️ *TRT:* {d.get('trt', 'N/A')}\n"
    r += f"📄 *Processo:* {d.get('numero_processo', 'N/A')}\n"
    r += f"👤 *Reclamante:* {d.get('nome_reclamante', 'N/A')}\n"
    r += f"🏢 *Cliente:* {d.get('_cliente_final', 'N/A')}\n"
    r += f"⚖️ *Tipo:* {d.get('_tipo_final', 'N/A')}\n"
    r += f"📅 *Data:* {d.get('data_decisao', 'N/A')}\n"
    r += f"💰 *Valor:* {d.get('valor_condenacao', 'N/A')}\n\n"
    r += f"📝 *Resumo:*\n{d.get('resumo_geral', 'N/A')}\n\n"

    favs = d.get("entendimentos_favoraveis") or []
    if isinstance(favs, list) and favs:
        r += "✅ *Favoráveis:*\n"
        for i, e in enumerate(favs, 1):
            if isinstance(e, dict):
                r += f"  {i}. *{e.get('tema','')}:* {e.get('entendimento','')}\n"
        r += "\n"

    desfavs = d.get("entendimentos_desfavoraveis") or []
    if isinstance(desfavs, list) and desfavs:
        r += "❌ *Desfavoráveis:*\n"
        for i, e in enumerate(desfavs, 1):
            if isinstance(e, dict):
                r += f"  {i}. *{e.get('tema','')}:* {e.get('entendimento','')}\n"
        r += "\n"

    r += f"📚 *Fundamentos:* {d.get('fundamentos_juridicos', 'N/A')}\n"
    r += f"📌 *Observações:* {d.get('observacoes_precedente', 'N/A')}\n"
    r += f"\n_Registrado por {sigla} em {datetime.now().strftime('%d/%m/%Y %H:%M')}_"
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


# ---------------------------------------------------------------------------
# HANDLER PRINCIPAL
# ---------------------------------------------------------------------------

async def processar_pdf_bytes(pdf_bytes: bytes, advogado: str, texto: str) -> str:
    sigla = resolver_sigla(advogado)
    hints = parse_mensagem(texto)
    texto_pdf = extrair_texto_pdf(pdf_bytes)
    if not texto_pdf.strip():
        return "Nao foi possivel extrair texto do PDF."
    analise = await analisar_decisao(texto_pdf, hints["cliente"] or "", hints["tipo"] or "")
    cliente_final = hints["cliente"] or analise.get("cliente_detectado") or "N/A"
    tipo_final = hints["tipo"] or analise.get("tipo_responsabilidade_detectado") or "N/A"
    analise["_cliente_final"] = cliente_final
    analise["_tipo_final"] = tipo_final
    row = {
        "DATA DO REGISTRO": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "ADVOGADO": sigla,
        "TRT": analise.get("trt", ""),
        "NUMERO DO PROCESSO": analise.get("numero_processo", ""),
        "NOME DO RECLAMANTE": analise.get("nome_reclamante", ""),
        "CLIENTE": cliente_final,
        "TIPO DE RESPONSABILIDADE": tipo_final,
        "TIPO DE DECISAO": analise.get("tipo_decisao", ""),
        "RESULTADO DA DECISAO": analise.get("resultado_geral", ""),
        "DATA DA DECISAO": analise.get("data_decisao", ""),
        "ENTENDIMENTOS FAVORAVEIS": formatar_entendimentos(analise.get("entendimentos_favoraveis", [])),
        "ENTENDIMENTOS DESFAVORAVEIS": formatar_entendimentos(analise.get("entendimentos_desfavoraveis", [])),
        "FUNDAMENTOS JURIDICOS": analise.get("fundamentos_juridicos", ""),
        "VALOR DA CONDENACAO": analise.get("valor_condenacao", ""),
        "RESUMO": analise.get("resumo_geral", ""),
        "OBSERVACOES": analise.get("observacoes_precedente", ""),
    }
    await salvar_decisao(row)
    return formatar_relatorio(analise, sigla)


async def processar_texto(texto_pdf: str, advogado: str, texto: str) -> str:
    """Processa texto já extraído do PDF pelo Apps Script via Google Drive."""
    sigla = resolver_sigla(advogado)
    hints = parse_mensagem(texto)

    if not texto_pdf.strip():
        return "⚠️ Não foi possível extrair texto do PDF."

    analise = await analisar_decisao(texto_pdf, hints["cliente"] or "", hints["tipo"] or "")

    cliente_final = hints["cliente"] or analise.get("cliente_detectado") or "N/A"
    tipo_final = hints["tipo"] or analise.get("tipo_responsabilidade_detectado") or "N/A"
    analise["_cliente_final"] = cliente_final
    analise["_tipo_final"] = tipo_final

    row = {
        "DATA DO REGISTRO": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "ADVOGADO": sigla,
        "TRT": analise.get("trt", ""),
        "NÚMERO DO PROCESSO": analise.get("numero_processo", ""),
        "NOME DO RECLAMANTE": analise.get("nome_reclamante", ""),
        "CLIENTE": cliente_final,
        "TIPO DE RESPONSABILIDADE": tipo_final,
        "TIPO DE DECISÃO": analise.get("tipo_decisao", ""),
        "RESULTADO DA DECISÃO": analise.get("resultado_geral", ""),
        "DATA DA DECISÃO": analise.get("data_decisao", ""),
        "ENTENDIMENTOS FAVORÁVEIS": formatar_entendimentos(analise.get("entendimentos_favoraveis", [])),
        "ENTENDIMENTOS DESFAVORÁVEIS": formatar_entendimentos(analise.get("entendimentos_desfavoraveis", [])),
        "FUNDAMENTOS JURÍDICOS": analise.get("fundamentos_juridicos", ""),
        "VALOR DA CONDENAÇÃO": analise.get("valor_condenacao", ""),
        "RESUMO": analise.get("resumo_geral", ""),
        "OBSERVAÇÕES": analise.get("observacoes_precedente", ""),
    }

    await salvar_decisao(row)
    return formatar_relatorio(analise, sigla)


async def processar_pdf(pdf_url: str, advogado: str, texto: str) -> str:
    sigla = resolver_sigla(advogado)
    hints = parse_mensagem(texto)

    pdf_bytes = await download_pdf(pdf_url)
    texto_pdf = extrair_texto_pdf(pdf_bytes)

    if not texto_pdf.strip():
        return "⚠️ Não foi possível extrair texto do PDF (pode ser escaneado ou protegido)."

    analise = await analisar_decisao(texto_pdf, hints["cliente"] or "", hints["tipo"] or "")

    cliente_final = hints["cliente"] or analise.get("cliente_detectado") or "N/A"
    tipo_final = hints["tipo"] or analise.get("tipo_responsabilidade_detectado") or "N/A"
    analise["_cliente_final"] = cliente_final
    analise["_tipo_final"] = tipo_final

    row = {
        "DATA DO REGISTRO": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "ADVOGADO": sigla,
        "TRT": analise.get("trt", ""),
        "NÚMERO DO PROCESSO": analise.get("numero_processo", ""),
        "NOME DO RECLAMANTE": analise.get("nome_reclamante", ""),
        "CLIENTE": cliente_final,
        "TIPO DE RESPONSABILIDADE": tipo_final,
        "TIPO DE DECISÃO": analise.get("tipo_decisao", ""),
        "RESULTADO DA DECISÃO": analise.get("resultado_geral", ""),
        "DATA DA DECISÃO": analise.get("data_decisao", ""),
        "ENTENDIMENTOS FAVORÁVEIS": formatar_entendimentos(analise.get("entendimentos_favoraveis", [])),
        "ENTENDIMENTOS DESFAVORÁVEIS": formatar_entendimentos(analise.get("entendimentos_desfavoraveis", [])),
        "FUNDAMENTOS JURÍDICOS": analise.get("fundamentos_juridicos", ""),
        "VALOR DA CONDENAÇÃO": analise.get("valor_condenacao", ""),
        "RESUMO": analise.get("resumo_geral", ""),
        "OBSERVAÇÕES": analise.get("observacoes_precedente", ""),
    }

    await salvar_decisao(row)
    return formatar_relatorio(analise, sigla)


# ---------------------------------------------------------------------------
# BUSCA
# ---------------------------------------------------------------------------

PROMPT_BUSCA = """Você é especialista em direito trabalhista brasileiro.

Busque nos dados abaixo precedentes {tipo_label} sobre o tema: "{tema}"

Dados da planilha:
{dados}

Retorne APENAS JSON válido:
{{
  "tema_buscado": str,
  "tipo": str,
  "total_encontrados": int,
  "precedentes": [
    {{
      "numero_processo": str,
      "advogado": str,
      "cliente": str,
      "trt": str,
      "data_decisao": str,
      "tipo_decisao": str,
      "entendimento_relevante": str,
      "como_usar": str
    }}
  ],
  "tese_consolidada": str,
  "argumentos_principais": str
}}"""


async def processar_busca(tipo: str, tema: str) -> str:
    if not tema:
        return f"⚠️ Informe o tema. Exemplo: `/{tipo} vínculo empregatício`"

    rows = await buscar_precedentes()
    dados_str = json.dumps(rows, ensure_ascii=False)[:25000]
    tipo_label = "FAVORÁVEIS" if tipo == "favoraveis" else "DESFAVORÁVEIS"

    prompt = PROMPT_BUSCA.format(tipo_label=tipo_label, tema=tema, dados=dados_str)
    response = await claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    resultado = _parse_json(response.content[0].text)
    return _formatar_busca(resultado)


def _formatar_busca(d: dict) -> str:
    tipo = (d.get("tipo") or "").upper()
    r = f"🔍 *PRECEDENTES {tipo}*\n\n"
    r += f"📌 *Tema:* {d.get('tema_buscado', 'N/A')}\n"
    r += f"📊 *Encontrados:* {d.get('total_encontrados', 0)}\n\n"
    for i, p in enumerate(d.get("precedentes") or [], 1):
        r += f"{i}. *{p.get('numero_processo','N/A')}*\n"
        r += f"   {p.get('cliente','N/A')} | {p.get('trt','N/A')} | {p.get('data_decisao','N/A')}\n"
        r += f"   _{p.get('entendimento_relevante','N/A')}_\n"
        r += f"   💡 {p.get('como_usar','N/A')}\n\n"
    r += f"*Tese consolidada:*\n{d.get('tese_consolidada','N/A')}\n\n"
    r += f"*Argumentos:*\n{d.get('argumentos_principais','N/A')}"
    return r


# ---------------------------------------------------------------------------
# AJUDA
# ---------------------------------------------------------------------------

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
                    "header": "📎 Postar uma decisão",
                    "widgets": [
                        {"textParagraph": {"text": "Envie o <b>PDF da decisão</b> no grupo. O bot analisa e registra automaticamente."}},
                        {"decoratedText": {"topLabel": "Cliente (opcional)", "text": "<font face=\"monospace\">Cliente: iFood</font>", "startIcon": {"knownIcon": "PERSON"}}},
                        {"decoratedText": {"topLabel": "Tipo (opcional)", "text": "<font face=\"monospace\">Tipo: OL</font>", "bottomLabel": "OL · Nuvem · Terceirização · Subsidiária · Ex Funcionário · Ex-Foodlovers · Marketplace", "startIcon": {"knownIcon": "BOOKMARK"}}},
                    ]
                },
                {
                    "header": "🔍 Buscar precedentes",
                    "widgets": [
                        {"decoratedText": {"text": "<font face=\"monospace\">/favoraveis [tema]</font>", "bottomLabel": "Ex: /favoraveis vínculo empregatício", "startIcon": {"knownIcon": "STAR"}}},
                        {"decoratedText": {"text": "<font face=\"monospace\">/desfavoraveis [tema]</font>", "bottomLabel": "Ex: /desfavoraveis responsabilidade subsidiária", "startIcon": {"knownIcon": "STAR"}}},
                        {"decoratedText": {"text": "<font face=\"monospace\">/ajuda</font>", "bottomLabel": "Exibe esta mensagem", "startIcon": {"knownIcon": "STAR"}}},
                    ]
                }
            ]
        },
        "_fallback_text": (
            "*Decisão FA Bot*\n\n"
            "📎 Envie o PDF no grupo. Opcional: `Cliente: iFood` e `Tipo: OL`\n\n"
            "🔍 `/favoraveis [tema]` · `/desfavoraveis [tema]` · `/ajuda`"
        )
    }


def get_ajuda() -> str:
    return (
        "*Decisão FA Bot* — Como usar:\n\n"
        "📎 *Postar uma decisão:*\n"
        "Envie o PDF da decisão no grupo.\n"
        "Opcionalmente inclua na mensagem:\n"
        "  `Cliente: iFood`\n"
        "  `Tipo: OL` _(OL, Nuvem, Terceirização, Subsidiária, Ex Funcionário, Ex-Foodlovers, Marketplace)_\n\n"
        "🔍 *Buscar precedentes:*\n"
        "`/favoraveis vínculo empregatício`\n"
        "`/desfavoraveis responsabilidade subsidiária`\n"
        "`/ajuda` — Esta mensagem"
    )
