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
import pdfplumber

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
# Ordem fixa: Claude primeiro, Gemini em segundo. Nunca GPT.
# Limite de input: ~8000 tokens ≈ 24000 chars
# IDs reais do endpoint models.inference.ai.azure.com (GitHub Models)
# Baseados na documentação GitHub Copilot supported models.
# Ordem: Claude Sonnet > Haiku > Gemini. Nunca GPT.
_GITHUB_MODELS = [
    "claude-sonnet-4-5",
    "claude-sonnet-4",
    "claude-haiku-4-5",
    "claude-3-7-sonnet",
    "claude-3-5-sonnet",
    "claude-3-5-haiku",
    "gemini-2-5-pro",
    "gemini-2.5-pro",
    "gemini-2-5-flash",
    "gemini-2.5-flash",
    "gemini-2-0-flash",
    "gemini-2.0-flash",
]
_GITHUB_MAX_CHARS = 20000  # margem segura abaixo de 8000 tokens


async def _resolver_modelos_github() -> list[str]:
    """Resolve os modelos permitidos pela conta, priorizando Claude e Gemini.
    Nunca inclui GPT.
    """
    global _cached_model_order

    if _cached_model_order:
        return _cached_model_order

    modelos_disponiveis: list[str] = []
    if GITHUB_TOKEN:
        try:
            # Usa httpx direto para evitar erro de parsing Pydantic do SDK openai
            async with httpx.AsyncClient(timeout=15) as cli:
                r = await cli.get(
                    "https://models.inference.ai.azure.com/models",
                    headers={"Authorization": f"Bearer {GITHUB_TOKEN}"},
                )
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, list):
                        modelos_disponiveis = [m.get("id", "") or m.get("name", "") for m in data if isinstance(m, dict)]
                    elif isinstance(data, dict):
                        items = data.get("data") or data.get("models") or data.get("value") or []
                        modelos_disponiveis = [m.get("id", "") or m.get("name", "") for m in items if isinstance(m, dict)]
                    modelos_disponiveis = [m for m in modelos_disponiveis if m]
                    logger.info("Modelos disponíveis no endpoint: %s", ", ".join(modelos_disponiveis))
                else:
                    logger.warning("Falha ao listar modelos (status %s).", r.status_code)
        except Exception as e:
            logger.warning("Falha ao listar modelos do endpoint (%s).", e)

    # Se a listagem falhar, usa fallback estático configurado.
    if not modelos_disponiveis:
        _cached_model_order = list(_GITHUB_MODELS)
        return _cached_model_order

    lower_map = {m.lower(): m for m in modelos_disponiveis}

    claude = [lower_map[k] for k in lower_map if "claude" in k]
    gemini = [lower_map[k] for k in lower_map if "gemini" in k]

    # Ordena por preferência quando existir correspondência parcial.
    def _ordenar_preferencia(candidatos: list[str], preferencias: list[str]) -> list[str]:
        ordenados: list[str] = []
        usados = set()
        for pref in preferencias:
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

    claude = _ordenar_preferencia(claude, [
        "claude-sonnet-4-5",
        "claude-sonnet-4",
        "claude-haiku-4-5",
        "claude-3-7-sonnet",
        "claude-3-5-sonnet",
        "claude-3-5-haiku",
    ])
    gemini = _ordenar_preferencia(gemini, [
        "gemini-2-5-pro",
        "gemini-2.5-pro",
        "gemini-2-5-flash",
        "gemini-2.5-flash",
        "gemini-2-0-flash",
        "gemini-2.0-flash",
    ])

    _cached_model_order = claude + gemini
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
                "Nenhum modelo Claude/Gemini válido disponível no endpoint da conta. "
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
                    max_tokens=4096,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "Você é um assistente especializado em análise jurídica. "
                                "Responda APENAS com JSON válido, sem markdown, sem blocos de código, "
                                "sem explicações e sem texto adicional antes ou depois do JSON."
                            ),
                        },
                        {"role": "user", "content": prompt_github},
                    ],
                )
                logger.info("IA: GitHub Copilot respondeu com modelo %s.", model_name)
                _last_success_model = model_name
                return response.choices[0].message.content or ""
            except Exception as e:
                if _is_unknown_model_error(e):
                    _invalid_models.add(model_name)
                logger.warning("GitHub Copilot modelo %s falhou (%s). Tentando próximo...", model_name, e)

    raise RuntimeError(
        "Nenhuma IA disponível no endpoint com modelos Claude/Gemini. "
        "Confirme o GITHUB_TOKEN e os modelos liberados para a conta (verifique /debug-models)."
    )


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
    mensagem, oferecer_email = await confirmar_sessao_data(advogado)
    await send_webhook(webhook_url, mensagem)
    if oferecer_email:
        await send_webhook(
            webhook_url,
            (
                f"📧 *{advogado}, deseja uma sugestão de e-mail de reporte ao cliente?*\n\n"
                f"`/sim` — gerar sugestão de e-mail\n"
                f"`/nao` — dispensar"
            ),
        )


async def confirmar_sessao_data(advogado: str) -> tuple[str, bool]:
    chave = _chave_sessao(advogado)
    sessoes = await carregar_sessoes()
    row = sessoes.get(chave)
    if not row:
        return f"⚠️ *{advogado}*, não há análise pendente para confirmar.", False
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
        logger.info("Sessão confirmada para %s", advogado)
        return (
            f"✅ *Decisão registrada com sucesso!*\n"
            f"📄 *Processo:* {row.get('NÚMERO DO PROCESSO', 'N/A')}\n"
            f"🏢 *Cliente:* {row.get('CLIENTE', 'N/A')}\n"
            f"📊 *Resultado:* {row.get('RESULTADO DA DECISÃO', 'N/A')}\n"
            f"_Salvo por {sigla} em {datetime.now().strftime('%d/%m/%Y %H:%M')}_",
            True,
        )
    except Exception as e:
        logger.exception("Erro ao salvar sessão: %s", e)
        return "⚠️ Erro ao salvar na planilha. Tente confirmar novamente.", False


async def cancelar_sessao(advogado: str, webhook_url: str):
    await send_webhook(webhook_url, await cancelar_sessao_data(advogado))


async def cancelar_sessao_data(advogado: str) -> str:
    chave = _chave_sessao(advogado)
    sessoes = await carregar_sessoes()
    if chave in sessoes:
        del sessoes[chave]
        await salvar_sessoes(sessoes)
        return f"❌ *{advogado}*, análise descartada. Nenhum registro foi salvo."
    return f"⚠️ *{advogado}*, não há análise pendente para cancelar."


async def marcar_aguardando_correcao(advogado: str) -> str:
    chave = _chave_sessao(advogado)
    sessoes = await carregar_sessoes()
    row = sessoes.get(chave)
    if not row:
        return f"⚠️ *{advogado}*, não há análise pendente para corrigir."
    row["_aguardando_correcao"] = True
    sessoes[chave] = row
    await salvar_sessoes(sessoes)
    return (
        f"✏️ *{advogado}*, me diga no chat o que você quer corrigir na análise.\n"
        f"Exemplo: `resultado deve ser Desfavorável e TRT é TRT-2`"
    )


async def esta_aguardando_correcao(advogado: str) -> bool:
    chave = _chave_sessao(advogado)
    sessoes = await carregar_sessoes()
    row = sessoes.get(chave) or {}
    return bool(row.get("_aguardando_correcao"))


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
    chave_email = f"email_{chave}"
    sessoes = await carregar_sessoes()

    if chave_email in sessoes:
        del sessoes[chave_email]
        await salvar_sessoes(sessoes)

    return f"👍 *{advogado}*, tudo certo! E-mail dispensado."


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


async def processar_texto(texto_pdf: str, advogado: str, texto: str, webhook_url: str) -> str:
    return await _analisar_e_aguardar(texto_pdf, advogado, texto, webhook_url)


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

PROMPT_BUSCA = """Você é especialista em direito trabalhista brasileiro.

CONTEXTO: Este escritório representa SEMPRE a empresa (reclamada). Precedentes favoráveis são decisões que beneficiaram a empresa. Precedentes desfavoráveis são decisões que prejudicaram a empresa.

Busque nos dados abaixo precedentes {tipo_label} À EMPRESA relacionados a: "{tema}"

INSTRUÇÕES:
- O termo de busca pode ser um TEMA JURÍDICO (ex: "horas extras", "vínculo empregatício") OU um NOME DE CLIENTE/EMPRESA (ex: "iFood", "Loft").
- Se o termo parecer um nome de empresa ou cliente, filtre pela coluna "CLIENTE" (busca parcial, sem distinção de maiúsculas/minúsculas).
- Se o termo parecer um tema jurídico, analise as colunas "ENTENDIMENTOS FAVORÁVEIS" e "ENTENDIMENTOS DESFAVORÁVEIS".
- Para busca de FAVORÁVEIS, foque na coluna "ENTENDIMENTOS FAVORÁVEIS".
- Para busca de DESFAVORÁVEIS, foque na coluna "ENTENDIMENTOS DESFAVORÁVEIS".
- Use busca semântica: encontre registros relacionados ao termo, mesmo sem correspondência exata de palavras.
- Se não encontrar nenhum precedente relacionado, retorne total_encontrados = 0 e precedentes = [].

Dados da planilha (lista de registros em JSON):
{dados}

Retorne APENAS JSON válido com esta estrutura exata:
{{
  "tema_buscado": "tema informado pelo usuário",
  "tipo": "FAVORÁVEIS ou DESFAVORÁVEIS",
  "total_encontrados": 0,
  "precedentes": [
    {{
      "numero_processo": "número do processo",
      "advogado": "sigla do advogado",
      "cliente": "nome do cliente",
      "trt": "TRT-X ou TST",
      "data_decisao": "data da decisão",
      "tipo_decisao": "Sentença ou Acórdão",
      "entendimento_relevante": "trecho do entendimento relacionado ao tema buscado",
      "como_usar": "como este precedente pode ser usado pela empresa em outros casos"
    }}
  ],
  "tese_consolidada": "tese consolidada do ponto de vista da empresa para usar em defesa",
  "argumentos_principais": "principais argumentos que a empresa pode usar baseado nestes precedentes"
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
    tipo = (_get(d, "tipo", default="")).upper()
    r  = f"🔍 *PRECEDENTES {tipo}*\n\n"
    r += f"📌 *Tema:* {_get(d, 'tema_buscado', 'tema')}\n"
    r += f"📊 *Encontrados:* {d.get('total_encontrados', 0)}\n\n"
    for i, p in enumerate(d.get("precedentes") or [], 1):
        processo = _get(p, "numero_processo", "NÚMERO DO PROCESSO", "numero processo")
        cliente  = _get(p, "cliente", "CLIENTE")
        trt      = _get(p, "trt", "TRT")
        data     = _get(p, "data_decisao", "DATA DA DECISÃO", "data decisao")
        entend   = _get(p, "entendimento_relevante", "ENTENDIMENTOS FAVORÁVEIS", "ENTENDIMENTOS DESFAVORÁVEIS", "entendimento")
        uso      = _get(p, "como_usar", "observacoes_precedente", "OBSERVAÇÕES")
        r += f"{i}. *{processo}*\n"
        r += f"   {cliente} | {trt} | {data}\n"
        r += f"   _{entend}_\n"
        r += f"   💡 {uso}\n\n"
    r += f"*Tese consolidada:*\n{_get(d, 'tese_consolidada')}\n\n"
    r += f"*Argumentos:*\n{_get(d, 'argumentos_principais')}"
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
