"""
Handlers principais do DecisionFA Bot v2.

Fluxo único (sem multi-step):
  Advogado posta mensagem com PDF + (opcionalmente) Cliente e Tipo no texto.
  Bot processa tudo e registra na planilha automaticamente.

Formato esperado na mensagem (qualquer ordem, tudo opcional no texto):
  Cliente: iFood
  Tipo: OL
  [PDF anexado]

A IA extrai da decisão: TRT, processo, reclamante, data, resultado, resumo, etc.
Cliente e Tipo informados no texto têm prioridade sobre o que a IA extraiu.
"""

import io
import json
import logging
import re
from datetime import datetime

import anthropic
import pdfplumber
import google.generativeai as genai

from bot.chat import send_message, download_file
from bot.sheets import salvar_decisao, buscar_precedentes
from bot.config import ANTHROPIC_API_KEY, COLUNAS

logger = logging.getLogger(__name__)
claude = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

# Gemini como fallback
_GEMINI_KEY = __import__('os').environ.get("GEMINI_API_KEY", "")
if _GEMINI_KEY:
    genai.configure(api_key=_GEMINI_KEY)


async def _chamar_ia(prompt: str) -> str:
    """
    Chama Claude primeiro. Se falhar (erro de API, timeout, etc.),
    tenta Gemini como fallback.
    """
    # Tenta Claude
    try:
        response = await claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        logger.info("IA: Claude respondeu.")
        return response.content[0].text
    except Exception as e:
        logger.warning("Claude falhou (%s). Tentando Gemini...", e)

    # Fallback: Gemini
    if not _GEMINI_KEY:
        raise RuntimeError("Claude falhou e GEMINI_API_KEY não está configurada.")
    try:
        model = genai.GenerativeModel("gemini-1.5-flash")
        response = model.generate_content(prompt)
        logger.info("IA: Gemini respondeu (fallback).")
        return response.text
    except Exception as e:
        raise RuntimeError(f"Ambas as IAs falharam. Último erro: {e}")

# ---------------------------------------------------------------------------
# MAPA DE SIGLAS — chave: nome completo exato do Google Chat (displayName)
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
    """
    Retorna a sigla do advogado a partir do displayName do Google Chat.
    Tenta correspondência exata primeiro, depois parcial (primeiro + último nome).
    Se não encontrar, gera sigla automática pelas iniciais e loga aviso.
    """
    # 1. Correspondência exata
    if display_name in SIGLAS:
        return SIGLAS[display_name]

    # 2. Correspondência case-insensitive
    lower = display_name.strip().lower()
    for nome, sigla in SIGLAS.items():
        if nome.lower() == lower:
            return sigla

    # 3. Correspondência parcial — verifica se alguma chave está contida no nome
    for nome, sigla in SIGLAS.items():
        partes = nome.lower().split()
        if all(p in lower for p in partes[:2]):  # primeiro e segundo nome batem
            return sigla

    # 4. Fallback: gera sigla pelas iniciais
    partes = display_name.strip().split()
    sigla_auto = "".join(p[0].upper() for p in partes if p)
    logger.warning(
        "Advogado '%s' não encontrado no mapa de siglas. "
        "Sigla automática gerada: '%s'. Adicione ao mapa SIGLAS em handlers.py.",
        display_name, sigla_auto
    )
    return sigla_auto

TIPOS_VALIDOS = ["OL", "Nuvem", "Terceirização", "Subsidiária", "Ex Funcionário", "Ex-Foodlovers", "Marketplace"]


# ---------------------------------------------------------------------------
# PARSER DA MENSAGEM DO ADVOGADO
# ---------------------------------------------------------------------------

def parse_mensagem(text: str) -> dict:
    """
    Extrai Cliente e Tipo do texto da mensagem.
    Aceita formatos flexíveis:
      "Cliente: iFood"  /  "cliente ifood"  /  "tipo: OL"  /  "OL"
    """
    result = {"cliente": None, "tipo": None}

    # Cliente
    m = re.search(r"cliente[:\s]+([^\n,/]+)", text, re.IGNORECASE)
    if m:
        result["cliente"] = m.group(1).strip()

    # Tipo — tenta extrair explicitamente ou detecta nome de tipo no texto
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

(Se o advogado informou cliente e/ou tipo, use esses valores nos campos correspondentes.
Os campos _detectado são apenas fallback para quando não foram informados.)

DECISÃO:
{texto}

RETORNE APENAS JSON VÁLIDO, sem markdown, sem explicações."""


async def analisar_decisao(texto: str, cliente_hint: str, tipo_hint: str) -> dict:
    texto_truncado = texto[:40000]
    prompt = PROMPT_ANALISE.format(
        cliente=cliente_hint or "Não informado — detecte da decisão",
        tipo=tipo_hint or "Não informado — detecte da decisão",
        texto=texto_truncado,
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
        "tipo_responsabilidade_detectado", "entendimentos_favoraveis",
        "entendimentos_desfavoraveis", "fundamentos_juridicos",
        "valor_condenacao", "resumo_geral", "observacoes_precedente",
    ]}


# ---------------------------------------------------------------------------
# FORMATAÇÃO DO RELATÓRIO
# ---------------------------------------------------------------------------

def formatar_relatorio(d: dict, advogado: str) -> str:
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
    r += f"\n_Registrado por {advogado} em {datetime.now().strftime('%d/%m/%Y %H:%M')}_"
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
# HANDLER PRINCIPAL — chamado para cada mensagem com PDF
# ---------------------------------------------------------------------------

async def handle_pdf_postagem(space_name: str, text: str, attachment: dict, advogado: str):
    """
    Processa postagem de decisão:
    1. Extrai cliente/tipo do texto
    2. Baixa e lê o PDF
    3. Chama Claude para análise
    4. Salva na planilha (coluna ADVOGADO = sigla)
    5. Confirma no chat
    """
    # Resolve sigla antes de tudo
    sigla = resolver_sigla(advogado)

    # 1. Parse do texto
    hints = parse_mensagem(text)
    cliente_hint = hints["cliente"]
    tipo_hint = hints["tipo"]

    await send_message(space_name, "⏳ *Analisando decisão...*\nAguarde alguns instantes.")

    # 2. Download do PDF
    url = (
        attachment.get("downloadUri")
        or attachment.get("downloadUrl")
        or attachment.get("source")
        or ""
    )
    if not url:
        await send_message(space_name, "⚠️ Não consegui acessar o PDF. Tente reenviar.")
        return

    try:
        pdf_bytes = await download_file(url)
    except Exception as e:
        logger.exception("Erro ao baixar PDF: %s", e)
        await send_message(space_name, "⚠️ Erro ao baixar o PDF. Tente reenviar.")
        return

    # 3. Extração de texto
    texto = extrair_texto_pdf(pdf_bytes)
    if not texto.strip():
        await send_message(space_name, "⚠️ Não foi possível extrair texto do PDF (pode ser escaneado).")
        return

    # 4. Análise com Claude
    try:
        analise = await analisar_decisao(texto, cliente_hint or "", tipo_hint or "")
    except Exception as e:
        logger.exception("Erro na análise: %s", e)
        await send_message(space_name, "⚠️ Erro na análise com IA. Tente novamente.")
        return

    # Cliente e tipo finais: prioriza o que o advogado informou no texto
    cliente_final = cliente_hint or analise.get("cliente_detectado") or "N/A"
    tipo_final = tipo_hint or analise.get("tipo_responsabilidade_detectado") or "N/A"
    analise["_cliente_final"] = cliente_final
    analise["_tipo_final"] = tipo_final

    # 5. Monta linha para planilha
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

    # 6. Salva na planilha
    try:
        await salvar_decisao(row)
    except Exception as e:
        logger.exception("Erro ao salvar planilha: %s", e)
        await send_message(space_name, "⚠️ Análise OK, mas erro ao salvar na planilha. Contate o admin.")
        return

    # 7. Confirma no chat com relatório
    relatorio = formatar_relatorio(analise, sigla)
    await send_message(space_name, relatorio)


# ---------------------------------------------------------------------------
# BUSCA DE PRECEDENTES
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


async def handle_busca(space_name: str, tipo: str, tema: str):
    if not tema:
        await send_message(space_name, f"⚠️ Informe o tema. Exemplo:\n`/{tipo} vínculo empregatício`")
        return

    await send_message(space_name, f"🔍 Buscando precedentes *{'FAVORÁVEIS' if tipo == 'favoraveis' else 'DESFAVORÁVEIS'}* sobre _{tema}_...")

    try:
        rows = await buscar_precedentes()
    except Exception as e:
        logger.exception("Erro ao buscar planilha: %s", e)
        await send_message(space_name, "⚠️ Erro ao acessar a planilha.")
        return

    dados_str = json.dumps(rows, ensure_ascii=False)[:25000]
    tipo_label = "FAVORÁVEIS" if tipo == "favoraveis" else "DESFAVORÁVEIS"

    prompt = PROMPT_BUSCA.format(tipo_label=tipo_label, tema=tema, dados=dados_str)

    try:
        raw = await _chamar_ia(prompt)
        resultado = _parse_json(raw)
    except Exception as e:
        logger.exception("Erro na busca IA: %s", e)
        await send_message(space_name, "⚠️ Erro ao processar busca.")
        return

    await send_message(space_name, _formatar_busca(resultado))


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

AJUDA = (
    "*Decisão FA Bot* — Como usar:\n\n"
    "📎 *Postar decisão:*\n"
    "Envie o PDF da decisão no grupo.\n"
    "Opcionalmente inclua na mensagem:\n"
    "  `Cliente: iFood`\n"
    "  `Tipo: OL` _(OL, Nuvem, Terceirização, Subsidiária, Ex Funcionário, Ex-Foodlovers, Marketplace)_\n"
    "A IA extrai e registra tudo automaticamente na planilha.\n\n"
    "🔍 *Buscar precedentes:*\n"
    "`/favoraveis [tema]`\n"
    "`/desfavoraveis [tema]`\n\n"
    "/ajuda — Esta mensagem"
)


async def send_ajuda(space_name: str):
    await send_message(space_name, AJUDA)
