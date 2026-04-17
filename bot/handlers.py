"""
Handlers v3 — com confirmação humana antes de salvar na planilha.
Sessões armazenadas em memória por advogado (chave = nome do advogado).
"""

import asyncio
import io
import json
import logging
import re
from datetime import datetime
import anthropic
import pdfplumber

from bot.sheets import salvar_decisao, buscar_precedentes, carregar_sessoes, salvar_sessoes
from bot.config import ANTHROPIC_API_KEY, GEMINI_API_KEY, GITHUB_TOKEN, COLUNAS
from bot.webhook import send_webhook

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
        logger.info("Gemini inicializado com sucesso.")
    except Exception as e:
        logger.error("Falha ao inicializar Gemini: %s", e)
        _gemini_ok = False
else:
    logger.warning("GEMINI_API_KEY não configurada.")
    _gemini_ok = False

# GitHub Models fallback (Claude gratuito via GitHub Copilot)
_github_ok = bool(GITHUB_TOKEN)

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

# Modelos Claude em ordem de preferência (do mais leve/barato ao mais pesado)
_CLAUDE_MODELS = [
    "claude-3-haiku-20240307",       # mais barato
    "claude-3-5-haiku-20241022",     # barato + capaz
    "claude-3-5-sonnet-20241022",    # equilibrado
    "claude-3-opus-20240229",        # mais poderoso
]

# Modelos Gemini disponíveis (API v1beta atual)
_GEMINI_MODELS = [
    "gemini-2.0-flash-lite",         # mais leve
    "gemini-2.0-flash",              # rápido
    "gemini-2.5-flash",              # flash mais recente
    "gemini-2.5-pro",                # mais poderoso
]

# Modelos Claude disponíveis via GitHub Models (gratuitos)
_GITHUB_MODELS = [
    "claude-3-5-sonnet",
    "claude-3-5-haiku",
    "claude-3-7-sonnet",
]

async def _chamar_ia(prompt: str) -> str:
    if claude:
        for model_name in _CLAUDE_MODELS:
            try:
                response = await claude.messages.create(
                    model=model_name,
                    max_tokens=4096,
                    messages=[{"role": "user", "content": prompt}],
                )
                logger.info("IA: Claude respondeu com modelo %s.", model_name)
                return response.content[0].text
            except Exception as e:
                logger.warning("Claude modelo %s falhou (%s). Tentando próximo...", model_name, e)

    if _gemini_ok:
        import google.generativeai as genai
        for model_name in _GEMINI_MODELS:
            try:
                model = genai.GenerativeModel(model_name)
                response = model.generate_content(prompt)
                logger.info("IA: Gemini respondeu com modelo %s.", model_name)
                return response.text
            except Exception as e:
                err_str = str(e)
                if "429" in err_str and "retry_delay" in err_str:
                    import re as _re
                    m = _re.search(r'seconds:\s*(\d+)', err_str)
                    wait = int(m.group(1)) + 2 if m else 35
                    logger.warning("Gemini modelo %s rate limit, aguardando %ss...", model_name, wait)
                    await asyncio.sleep(wait)
                    try:
                        response = model.generate_content(prompt)
                        logger.info("IA: Gemini respondeu após retry com modelo %s.", model_name)
                        return response.text
                    except Exception as e2:
                        logger.warning("Gemini modelo %s falhou após retry (%s). Tentando próximo...", model_name, e2)
                else:
                    logger.warning("Gemini modelo %s falhou (%s). Tentando próximo...", model_name, e)

    if _github_ok:
        for model_name in _GITHUB_MODELS:
            try:
                async with httpx.AsyncClient(timeout=60) as client:
                    resp = await client.post(
                        "https://models.inference.ai.azure.com/chat/completions",
                        headers={
                            "Authorization": f"Bearer {GITHUB_TOKEN}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": model_name,
                            "max_tokens": 4096,
                            "messages": [{"role": "user", "content": prompt}],
                        },
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    logger.info("IA: GitHub Models respondeu com modelo %s.", model_name)
                    return data["choices"][0]["message"]["content"]
            except Exception as e:
                logger.warning("GitHub Models modelo %s falhou (%s). Tentando próximo...", model_name, e)

    raise RuntimeError("Nenhuma IA disponível. Configure ANTHROPIC_API_KEY, GEMINI_API_KEY ou GITHUB_TOKEN.")


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
  "juiz_relator": "nome completo do juiz singular ou relator do acórdão ou N/A",
  "vara_turma": "ex: 2ª Vara do Trabalho de São Paulo, 3ª Turma do TST ou N/A",
  "entendimentos_favoraveis": [{{"tema": "tema jurídico", "entendimento": "tese favorável à empresa"}}],
  "entendimentos_desfavoraveis": [{{"tema": "tema jurídico", "entendimento": "tese desfavorável à empresa"}}],
  "fundamentos_juridicos": "artigos, súmulas e precedentes citados na decisão",
  "valor_condenacao": "R$ 0,00 ou N/A — se favorável à empresa coloque N/A",
  "resumo_geral": "resumo em 3-5 linhas do ponto de vista da empresa",
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
    r = "✅ *ANÁLISE CONCLUÍDA — aguardando confirmação*\n\n"
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
        r += "✅ *Favoráveis (para a empresa):*\n"
        for i, e in enumerate(favs, 1):
            if isinstance(e, dict):
                r += f"  {i}. *{e.get('tema','')}:* {e.get('entendimento','')}\n"
        r += "\n"

    desfavs = d.get("entendimentos_desfavoraveis") or []
    if isinstance(desfavs, list) and desfavs:
        r += "❌ *Desfavoráveis (para a empresa):*\n"
        for i, e in enumerate(desfavs, 1):
            if isinstance(e, dict):
                r += f"  {i}. *{e.get('tema','')}:* {e.get('entendimento','')}\n"
        r += "\n"

    r += f"📚 *Fundamentos:* {d.get('fundamentos_juridicos', 'N/A')}\n"
    r += f"📌 *Observações:* {d.get('observacoes_precedente', 'N/A')}\n"
    r += f"\n_Analisado por {sigla} em {datetime.now().strftime('%d/%m/%Y %H:%M')}_"
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
        "DATA DO REGISTRO":           datetime.now().strftime("%d/%m/%Y %H:%M"),
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
    chave = _chave_sessao(advogado)
    sessoes = await carregar_sessoes()
    row = sessoes.get(chave)
    if not row:
        await send_webhook(webhook_url, f"⚠️ *{advogado}*, não há análise pendente para confirmar.")
        return
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
        await send_webhook(
            webhook_url,
            f"✅ *Decisão registrada com sucesso!*\n"
            f"📄 *Processo:* {row.get('NÚMERO DO PROCESSO', 'N/A')}\n"
            f"🏢 *Cliente:* {row.get('CLIENTE', 'N/A')}\n"
            f"📊 *Resultado:* {row.get('RESULTADO DA DECISÃO', 'N/A')}\n"
            f"_Salvo por {sigla} em {datetime.now().strftime('%d/%m/%Y %H:%M')}_"
        )
        logger.info("Sessão confirmada para %s", advogado)

        # Pergunta se deseja sugestão de e-mail
        await send_webhook(
            webhook_url,
            f"📧 *{advogado}, deseja uma sugestão de e-mail de reporte ao cliente?*\n\n"
            f"`/sim` — gerar sugestão de e-mail\n"
            f"`/nao` — dispensar"
        )

    except Exception as e:
        logger.exception("Erro ao salvar sessão: %s", e)
        await send_webhook(webhook_url, "⚠️ Erro ao salvar na planilha. Tente `/confirmar` novamente.")


async def cancelar_sessao(advogado: str, webhook_url: str):
    chave = _chave_sessao(advogado)
    sessoes = await carregar_sessoes()
    if chave in sessoes:
        del sessoes[chave]
        await salvar_sessoes(sessoes)
        await send_webhook(webhook_url, f"❌ *{advogado}*, análise descartada. Nenhum registro foi salvo.")
    else:
        await send_webhook(webhook_url, f"⚠️ *{advogado}*, não há análise pendente para cancelar.")


# ---------------------------------------------------------------------------
# GERAÇÃO DE E-MAIL — /sim e /nao
# ---------------------------------------------------------------------------

PROMPT_EMAIL = """Você é especialista em comunicação jurídica trabalhista brasileira.

Redija um e-mail profissional de reporte de decisão judicial para o cliente, com base nos dados abaixo.
O escritório representa SEMPRE a empresa (reclamada). A comunicação deve ser clara, objetiva e tranquilizadora (ou realista quando desfavorável).

DADOS DA DECISÃO:
- Tipo de decisão: {tipo_decisao}
- Resultado: {resultado} (do ponto de vista da empresa)
- Número do processo: {numero_processo}
- Reclamante: {reclamante}
- Cliente (empresa): {cliente}
- Data da decisão: {data_decisao}
- Vara/Turma: {vara_turma}
- Valor da condenação: {valor_condenacao}
- Resumo: {resumo}
- Entendimentos favoráveis: {favoraveis}
- Entendimentos desfavoráveis: {desfavoraveis}
- Observações/Precedente: {observacoes}

INSTRUÇÕES PARA O E-MAIL:
1. Assunto já definido (não altere): {assunto}
2. Cumprimente o cliente formalmente.
3. Informe o resultado da decisão de forma clara.
4. Destaque os principais pontos da decisão.
5. Explique as estratégias que o escritório adotará a partir desta decisão.
6. Indique os próximos passos processuais.
7. Informe o valor da condenação (se houver), as custas processuais estimadas, e o valor do depósito recursal necessário (ou mencione que o juízo já está garantido, se aplicável). Caso não haja condenação (decisão favorável), informe isso claramente.
8. Finalize de forma profissional, colocando-se à disposição para dúvidas.

Retorne APENAS o corpo do e-mail (sem o assunto), em texto corrido, formatado em português formal, sem markdown, sem asteriscos.
O e-mail deve ter no máximo 400 palavras."""


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
    """Handler para /sim — gera o e-mail de reporte ao cliente."""
    chave = _chave_sessao(advogado)
    chave_email = f"email_{chave}"
    sessoes = await carregar_sessoes()
    row = sessoes.get(chave_email)

    if not row:
        await send_webhook(
            webhook_url,
            f"⚠️ *{advogado}*, não há decisão recente para gerar o e-mail.\n"
            f"Confirme uma decisão com `/confirmar` primeiro."
        )
        return

    await send_webhook(webhook_url, "✉️ *Gerando sugestão de e-mail...* Aguarde.")

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

        mensagem = (
            f"📧 *SUGESTÃO DE E-MAIL*\n\n"
            f"*Assunto:* {assunto}\n\n"
            f"{'─' * 40}\n\n"
            f"{corpo_email.strip()}\n\n"
            f"{'─' * 40}\n"
            f"_Sugestão gerada automaticamente. Revise antes de enviar._"
        )
        await send_webhook(webhook_url, mensagem)
        logger.info("E-mail gerado para %s", advogado)

    except Exception as e:
        logger.exception("Erro ao gerar e-mail: %s", e)
        await send_webhook(webhook_url, "⚠️ Erro ao gerar o e-mail. Tente `/sim` novamente.")


async def dispensar_email_sessao(advogado: str, webhook_url: str):
    """Handler para /nao — descarta a oferta de e-mail."""
    chave = _chave_sessao(advogado)
    chave_email = f"email_{chave}"
    sessoes = await carregar_sessoes()

    if chave_email in sessoes:
        del sessoes[chave_email]
        await salvar_sessoes(sessoes)

    await send_webhook(webhook_url, f"👍 *{advogado}*, tudo certo! E-mail dispensado.")


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
    chave = _chave_sessao(advogado)
    sessoes = await carregar_sessoes()
    row = sessoes.get(chave)
    if not row:
        await send_webhook(webhook_url, f"⚠️ *{advogado}*, não há análise pendente para corrigir.")
        return

    await send_webhook(webhook_url, f"✏️ *Aplicando correção...* Aguarde.")

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
        sessoes[chave] = row_corrigido
        await salvar_sessoes(sessoes)

        relatorio  = formatar_relatorio(analise_corrigida, sigla)
        confirmacao = mensagem_confirmacao(advogado)
        await send_webhook(webhook_url, relatorio + "\n\n" + confirmacao)

    except Exception as e:
        logger.exception("Erro ao corrigir sessão: %s", e)
        await send_webhook(webhook_url, "⚠️ Erro ao aplicar correção. Tente novamente.")


# ---------------------------------------------------------------------------
# HANDLERS PRINCIPAIS
# ---------------------------------------------------------------------------

async def _analisar_e_aguardar(texto_pdf: str, advogado: str, texto: str, webhook_url: str) -> str:
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
    chave = _chave_sessao(advogado)
    sessoes = await carregar_sessoes()
    sessoes[chave] = row
    await salvar_sessoes(sessoes)
    logger.info("Sessão pendente criada para %s (chave: %s)", advogado, chave)

    # Retorna relatório + instrução de confirmação
    relatorio = formatar_relatorio(analise, sigla)
    confirmacao = mensagem_confirmacao(advogado)
    return relatorio + "\n\n" + confirmacao


async def processar_texto(texto_pdf: str, advogado: str, texto: str, webhook_url: str) -> str:
    return await _analisar_e_aguardar(texto_pdf, advogado, texto, webhook_url)


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
