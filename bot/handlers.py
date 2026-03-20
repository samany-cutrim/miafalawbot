"""
Handlers v3 — lógica de negócio.
"""

import io
import json
import logging
import re
from datetime import datetime

import anthropic
import pdfplumber

from bot.sheets import salvar_decisao, buscar_precedentes
from bot.config import ANTHROPIC_API_KEY, GEMINI_API_KEY, COLUNAS

import httpx

logger = logging.getLogger(__name__)

# Claude client
claude = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

# Gemini fallback
if GEMINI_API_KEY:
    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
        _gemini_ok = True
    except Exception:
        _gemini_ok = False
else:
    _gemini_ok = False


# ---------------------------------------------------------------------------
# MAPA DE SIGLAS
# ---------------------------------------------------------------------------

SIGLAS: dict[str, str] = {
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
    # Correspondência exata
    if display_name in SIGLAS:
        return SIGLAS[display_name]
    # Case-insensitive
    lower = display_name.strip().lower()
    for nome, sigla in SIGLAS.items():
        if nome.lower() == lower:
            return sigla
    # Primeiro nome (ex: "Samany" → "SC")
    primeiro = lower.split()[0] if lower.split() else lower
    for nome, sigla in SIGLAS.items():
        if nome.lower().startswith(primeiro):
            return sigla
    # Parcial
    for nome, sigla in SIGLAS.items():
        partes = nome.lower().split()
        if all(p in lower for p in partes[:2]):
            return sigla
    # Fallback: iniciais
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
    """
    Número formato: 0000310-88.2024.5.05.0102
    O TRT é o dígito após '5.' (posição 4 nos segmentos separados por ponto)
    Ex: 2024.5.05.0102 → TRT 5
    """
    if not numero or numero == "N/A":
        return "N/A"
    try:
        # Remove espaços e busca padrão NNNNNN-NN.AAAA.J.TT.OOOO
        m = re.search(r'\d{7}-\d{2}\.\d{4}\.(\d)\.(\d{2})\.\d{4}', numero)
        if m:
            justica = m.group(1)  # deve ser 5 (Justiça do Trabalho)
            tribunal = m.group(2).lstrip("0")  # ex: "05" → "5"
            if justica == "5":
                return f"TRT-{tribunal}"
        # Fallback: busca qualquer .5.XX.
        m2 = re.search(r'\.5\.(\d{2})\.', numero)
        if m2:
            tribunal = m2.group(1).lstrip("0")
            return f"TRT-{tribunal}"
    except Exception:
        pass
    return "N/A"


# ---------------------------------------------------------------------------
# CHAMADA À IA (Claude + fallback Gemini)
# ---------------------------------------------------------------------------

async def _chamar_ia(prompt: str) -> str:
    # Tenta Claude
    if claude:
        try:
            response = await claude.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=4096,
                messages=[{"role": "user", "content": prompt}],
            )
            logger.info("IA: Claude respondeu.")
            return response.content[0].text
        except Exception as e:
            logger.warning("Claude falhou (%s). Tentando Gemini...", e)

    # Fallback Gemini
    if _gemini_ok:
        try:
            import google.generativeai as genai
            model = genai.GenerativeModel("gemini-1.5-flash")
            response = model.generate_content(prompt)
            logger.info("IA: Gemini respondeu (fallback).")
            return response.text
        except Exception as e:
            raise RuntimeError(f"Ambas as IAs falharam. Último erro: {e}")

    raise RuntimeError("Nenhuma IA disponível. Configure ANTHROPIC_API_KEY ou GEMINI_API_KEY.")


# ---------------------------------------------------------------------------
# ANÁLISE COM IA
# ---------------------------------------------------------------------------

PROMPT_ANALISE = """Você é especialista em decisões judiciais trabalhistas brasileiras.

CONTEXTO IMPORTANTE:
- Este escritório representa SEMPRE a empresa (reclamada/ré), nunca o trabalhador.
- "Favorável" significa favorável À EMPRESA (ex: pedido negado, vínculo não reconhecido, condenação reduzida).
- "Desfavorável" significa desfavorável À EMPRESA (ex: vínculo reconhecido, condenação imposta, recurso negado).
- Os entendimentos favoráveis são teses que BENEFICIAM a empresa e podem ser usados como precedente.
- Os entendimentos desfavoráveis são teses que PREJUDICAM a empresa e devem ser monitorados.

Analise a decisão abaixo e retorne APENAS um JSON válido com os campos:
{{
  "trt": "TRT-X ou N/A",
  "numero_processo": "0000000-00.0000.0.00.0000 ou N/A",
  "nome_reclamante": "nome completo do reclamante/trabalhador ou N/A",
  "data_decisao": "DD/MM/AAAA — busque no final do documento na assinatura ou cabeçalho ou N/A",
  "tipo_decisao": "Sentença ou Acórdão",
  "resultado_geral": "Favorável ou Desfavorável ou Parcialmente Favorável — SEMPRE do ponto de vista da EMPRESA",
  "cliente_detectado": "nome do réu/reclamado principal (a empresa) ou N/A",
  "tipo_responsabilidade_detectado": "OL ou Nuvem ou Terceirização ou Subsidiária ou Ex Funcionário ou Ex-Foodlovers ou Marketplace ou N/A",
  "juiz_relator": "nome completo do juiz singular ou relator do acórdão que proferiu/assinou a decisão ou N/A",
  "vara_turma": "ex: 2ª Vara do Trabalho de São Paulo, 3ª Turma do TST — extraia do cabeçalho ou rodapé ou N/A",
  "entendimentos_favoraveis": [{{"tema": "tema jurídico", "entendimento": "tese favorável à empresa"}}],
  "entendimentos_desfavoraveis": [{{"tema": "tema jurídico", "entendimento": "tese desfavorável à empresa"}}],
  "fundamentos_juridicos": "artigos, súmulas e precedentes citados na decisão",
  "valor_condenacao": "R$ 0,00 ou N/A — se favorável à empresa coloque N/A",
  "resumo_geral": "resumo em 3-5 linhas do ponto de vista da empresa — o que foi decidido e como impacta a empresa",
  "observacoes_precedente": "como esta decisão pode ser usada como precedente em outros casos pela empresa"
}}

CLIENTE (EMPRESA RECLAMADA) INFORMADO PELO ADVOGADO: {cliente}
TIPO DE RESPONSABILIDADE INFORMADO PELO ADVOGADO: {tipo}

DECISÃO:
{texto}

RETORNE APENAS JSON VÁLIDO, sem markdown, sem explicações."""


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
    r = "✅ *DECISÃO REGISTRADA*\n\n"
    r += f"📋 *{d.get('tipo_decisao', 'N/A')}* — {d.get('resultado_geral', 'N/A')}\n"
    r += f"🏛️ *TRT:* {d.get('trt', 'N/A')}\n"
    r += f"📄 *Processo:* {d.get('numero_processo', 'N/A')}\n"
    r += f"👤 *Reclamante:* {d.get('nome_reclamante', 'N/A')}\n"
    r += f"🏢 *Cliente:* {d.get('_cliente_final', 'N/A')}\n"
    r += f"⚖️ *Tipo:* {d.get('_tipo_final', 'N/A')}\n"
    r += f"📅 *Data:* {d.get('data_decisao', 'N/A')}\n"
    r += f"👨‍⚖️ *Juiz/Relator:* {d.get('juiz_relator', 'N/A')}\n"
    r += f"🏠 *Vara/Turma:* {d.get('vara_turma', 'N/A')}\n"
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
# HANDLERS PRINCIPAIS
# ---------------------------------------------------------------------------

async def _montar_e_salvar(analise: dict, sigla: str, hints: dict) -> str:
    cliente_final = hints["cliente"] or analise.get("cliente_detectado") or "N/A"
    tipo_final    = hints["tipo"] or analise.get("tipo_responsabilidade_detectado") or "N/A"
    analise["_cliente_final"] = cliente_final
    analise["_tipo_final"]    = tipo_final

    # TRT: prioriza extração do número do processo se IA retornou N/A
    trt = analise.get("trt", "N/A")
    if not trt or trt == "N/A":
        trt = extrair_trt_do_processo(analise.get("numero_processo", ""))
    analise["trt"] = trt

    row = {
        "DATA DO REGISTRO":         datetime.now().strftime("%d/%m/%Y %H:%M"),
        "ADVOGADO":                  sigla,
        "TRT":                       trt,
        "NÚMERO DO PROCESSO":        analise.get("numero_processo", ""),
        "NOME DO RECLAMANTE":        analise.get("nome_reclamante", ""),
        "CLIENTE":                   cliente_final,
        "TIPO DE RESPONSABILIDADE":  tipo_final,
        "TIPO DE DECISÃO":           analise.get("tipo_decisao", ""),
        "RESULTADO DA DECISÃO":      analise.get("resultado_geral", ""),
        "DATA DA DECISÃO":           analise.get("data_decisao", ""),
        "JUIZ/RELATOR":              analise.get("juiz_relator", ""),
        "VARA/TURMA":                analise.get("vara_turma", ""),
        "ENTENDIMENTOS FAVORÁVEIS":  formatar_entendimentos(analise.get("entendimentos_favoraveis", [])),
        "ENTENDIMENTOS DESFAVORÁVEIS": formatar_entendimentos(analise.get("entendimentos_desfavoraveis", [])),
        "FUNDAMENTOS JURÍDICOS":     analise.get("fundamentos_juridicos", ""),
        "VALOR DA CONDENAÇÃO":       analise.get("valor_condenacao", ""),
        "RESUMO":                    analise.get("resumo_geral", ""),
        "OBSERVAÇÕES":               analise.get("observacoes_precedente", ""),
    }

    await salvar_decisao(row)
    return formatar_relatorio(analise, sigla)


async def processar_texto(texto_pdf: str, advogado: str, texto: str) -> str:
    sigla  = resolver_sigla(advogado)
    hints  = parse_mensagem(texto)
    if not texto_pdf.strip():
        return "⚠️ Não foi possível extrair texto do PDF."
    analise = await analisar_decisao(texto_pdf, hints["cliente"] or "", hints["tipo"] or "")
    return await _montar_e_salvar(analise, sigla, hints)


async def processar_pdf_bytes(pdf_bytes: bytes, advogado: str, texto: str) -> str:
    sigla      = resolver_sigla(advogado)
    hints      = parse_mensagem(texto)
    texto_pdf  = extrair_texto_pdf(pdf_bytes)
    if not texto_pdf.strip():
        return "⚠️ Não foi possível extrair texto do PDF."
    analise = await analisar_decisao(texto_pdf, hints["cliente"] or "", hints["tipo"] or "")
    return await _montar_e_salvar(analise, sigla, hints)


async def processar_pdf(pdf_url: str, advogado: str, texto: str) -> str:
    sigla      = resolver_sigla(advogado)
    hints      = parse_mensagem(texto)
    pdf_bytes  = await download_pdf(pdf_url)
    texto_pdf  = extrair_texto_pdf(pdf_bytes)
    if not texto_pdf.strip():
        return "⚠️ Não foi possível extrair texto do PDF."
    analise = await analisar_decisao(texto_pdf, hints["cliente"] or "", hints["tipo"] or "")
    return await _montar_e_salvar(analise, sigla, hints)


# ---------------------------------------------------------------------------
# BUSCA
# ---------------------------------------------------------------------------

PROMPT_BUSCA = """Você é especialista em direito trabalhista brasileiro.

CONTEXTO: Este escritório representa SEMPRE a empresa (reclamada). Precedentes favoráveis são decisões que beneficiaram a empresa. Precedentes desfavoráveis são decisões que prejudicaram a empresa.

Busque nos dados abaixo precedentes {tipo_label} À EMPRESA sobre o tema: "{tema}"

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
  "tese_consolidada": "tese consolidada do ponto de vista da empresa para usar em defesa",
  "argumentos_principais": "principais argumentos que a empresa pode usar baseado nestes precedentes"
}}"""


async def processar_busca(tipo: str, tema: str) -> str:
    if not tema:
        return f"⚠️ Informe o tema. Exemplo: `/{tipo} vínculo empregatício`"
    rows      = await buscar_precedentes()
    dados_str = json.dumps(rows, ensure_ascii=False)[:25000]
    tipo_label = "FAVORÁVEIS" if tipo == "favoraveis" else "DESFAVORÁVEIS"
    prompt    = PROMPT_BUSCA.format(tipo_label=tipo_label, tema=tema, dados=dados_str)
    raw       = await _chamar_ia(prompt)
    resultado = _parse_json(raw)
    return _formatar_busca(resultado)


def _formatar_busca(d: dict) -> str:
    tipo = (d.get("tipo") or "").upper()
    r  = f"🔍 *PRECEDENTES {tipo}*\n\n"
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
                        {"decoratedText": {"topLabel": "Cliente (opcional)", "text": "<font face=\"monospace\">Cliente: iFood</font>", "startIcon": {"knownIcon": "PERSON"}}},
                        {"decoratedText": {"topLabel": "Tipo (opcional)", "text": "<font face=\"monospace\">Tipo: OL</font>", "bottomLabel": "OL · Nuvem · Terceirização · Subsidiária · Ex Funcionário · Ex-Foodlovers · Marketplace", "startIcon": {"knownIcon": "BOOKMARK"}}},
                    ]
                },
                {
                    "header": "🔍 Buscar precedentes",
                    "widgets": [
                        {"decoratedText": {"text": "<font face=\"monospace\">/favoraveis [tema]</font>", "bottomLabel": "Ex: /favoraveis vínculo empregatício", "startIcon": {"knownIcon": "STAR"}}},
                        {"decoratedText": {"text": "<font face=\"monospace\">/desfavoraveis [tema]</font>", "bottomLabel": "Ex: /desfavoraveis responsabilidade subsidiária", "startIcon": {"knownIcon": "STAR"}}},
                        {"decoratedText": {"text": "<font face=\"monospace\">/link</font>", "bottomLabel": "Envia o link do formulário", "startIcon": {"knownIcon": "STAR"}}},
                        {"decoratedText": {"text": "<font face=\"monospace\">/ajuda</font>", "bottomLabel": "Exibe esta mensagem", "startIcon": {"knownIcon": "STAR"}}},
                    ]
                }
            ]
        },
        "_fallback_text": (
            f"*Decisão FA Bot*\n\n"
            f"📎 Registrar decisão: {FORM_LINK}\n\n"
            f"🔍 `/favoraveis [tema]` · `/desfavoraveis [tema]` · `/link` · `/ajuda`"
        )
    }


def get_ajuda() -> str:
    return (
        f"*Decisão FA Bot* — Como usar:\n\n"
        f"📎 *Registrar decisão:*\n{FORM_LINK}\n\n"
        f"🔍 *Buscar precedentes:*\n"
        "`/favoraveis vínculo empregatício`\n"
        "`/desfavoraveis responsabilidade subsidiária`\n"
        "`/link` — Link do formulário\n"
        "`/ajuda` — Esta mensagem"
    )


def get_link() -> str:
    return (
        f"📎 *Link para registrar decisão:*\n\n"
        f"{FORM_LINK}\n\n"
        f"_Anexe o PDF, informe o cliente e tipo (opcionais) e envie._"
    )
