/**
 * DecisionFA Bot v3 — Google Apps Script
 *
 * Como funciona:
 *   1. Um trigger de tempo (a cada 1 min) chama verificarMensagens()
 *   2. O script lê as últimas mensagens do espaço usando a Chat API
 *      com OAuth do próprio usuário (sem precisar de bot aprovado)
 *   3. Se encontrar mensagem nova com PDF, chama o servidor no Render
 *   4. O servidor analisa e responde via Incoming Webhook no mesmo espaço
 *
 * CONFIGURAÇÃO (preencha as constantes abaixo):
 *   SPACE_NAME    → nome do espaço (formato: spaces/XXXXXXXX)
 *   RENDER_URL    → URL do seu serviço no Render
 *   WEBHOOK_URL   → Incoming Webhook criado no espaço do Chat
 */

// ============================================================
// ⚙️  CONFIGURAÇÕES — edite aqui
// ============================================================
var SPACE_NAME  = "spaces/XXXXXXXX";          // <- substitua pelo ID do espaço
var RENDER_URL  = "https://SEU-APP.onrender.com"; // <- URL do Render
var WEBHOOK_URL = "https://chat.googleapis.com/v1/spaces/XXXXXXXX/messages?key=...&token=..."; // <- webhook do espaço
// ============================================================

var ULTIMA_LEITURA_KEY = "ultimaLeitura";

// ---------------------------------------------------------------------------
// TRIGGER PRINCIPAL — chame este a cada 1 minuto
// ---------------------------------------------------------------------------
function verificarMensagens() {
  var props = PropertiesService.getScriptProperties();
  var ultimaLeitura = props.getProperty(ULTIMA_LEITURA_KEY);

  // Na primeira execução, marca agora e sai (evita processar histórico)
  if (!ultimaLeitura) {
    props.setProperty(ULTIMA_LEITURA_KEY, new Date().toISOString());
    return;
  }

  var mensagens = listarMensagensNovas(ultimaLeitura);
  if (!mensagens || mensagens.length === 0) return;

  // Atualiza timestamp antes de processar (evita reprocessar em caso de erro)
  props.setProperty(ULTIMA_LEITURA_KEY, new Date().toISOString());

  mensagens.forEach(function(msg) {
    processarMensagem(msg);
  });
}

// ---------------------------------------------------------------------------
// LISTA MENSAGENS NOVAS DO ESPAÇO
// ---------------------------------------------------------------------------
function listarMensagensNovas(desde) {
  var token = ScriptApp.getOAuthToken();
  var url = "https://chat.googleapis.com/v1/" + SPACE_NAME + "/messages"
    + "?orderBy=createTime asc"
    + "&filter=createTime > \"" + desde + "\"";

  var resp = UrlFetchApp.fetch(url, {
    headers: { "Authorization": "Bearer " + token },
    muteHttpExceptions: true
  });

  if (resp.getResponseCode() !== 200) {
    Logger.log("Erro ao listar mensagens: " + resp.getContentText());
    return [];
  }

  var data = JSON.parse(resp.getContentText());
  return data.messages || [];
}

// ---------------------------------------------------------------------------
// PROCESSA UMA MENSAGEM
// ---------------------------------------------------------------------------
function processarMensagem(msg) {
  var texto = (msg.text || msg.argumentText || "").replace(/<[^>]+>/g, "").trim();
  var sender = msg.sender || {};
  var advogado = sender.displayName || sender.name || "Advogado";

  // Ignora mensagens do próprio bot (evita loop)
  if (sender.type === "BOT") return;

  var attachments = msg.attachment || msg.attachments || [];
  if (!Array.isArray(attachments)) attachments = [attachments];

  // --- Verifica se tem PDF anexado ---
  var pdf = encontrarPdf(attachments);
  if (pdf) {
    var pdfUrl = obterUrlPdf(pdf);
    if (!pdfUrl) {
      chamarWebhook("⚠️ Não consegui acessar o PDF de " + advogado + ". Tente reenviar.");
      return;
    }
    chamarRender("/processar", {
      pdf_url: pdfUrl,
      advogado: advogado,
      texto: texto,
      webhook_url: WEBHOOK_URL
    });
    return;
  }

  // --- Comandos de busca ---
  var tl = texto.toLowerCase();

  if (tl.indexOf("/favoraveis") === 0 || tl.indexOf("favoraveis") === 0) {
    var tema = texto.replace(/^\\/?(favoraveis|favoráveis)\\s*/i, "").trim();
    chamarRender("/buscar", { tipo: "favoraveis", tema: tema, webhook_url: WEBHOOK_URL });
    return;
  }

  if (tl.indexOf("/desfavoraveis") === 0 || tl.indexOf("desfavoraveis") === 0) {
    var tema2 = texto.replace(/^\\/?(desfavoraveis|desfavoráveis)\\s*/i, "").trim();
    chamarRender("/buscar", { tipo: "desfavoraveis", tema: tema2, webhook_url: WEBHOOK_URL });
    return;
  }

  if (tl.indexOf("/ajuda") !== -1 || tl === "ajuda") {
    chamarRender("/ajuda", { webhook_url: WEBHOOK_URL });
    return;
  }
}

// ---------------------------------------------------------------------------
// HELPERS
// ---------------------------------------------------------------------------

function encontrarPdf(attachments) {
  for (var i = 0; i < attachments.length; i++) {
    var a = attachments[i];
    if (
      a.contentType === "application/pdf" ||
      a.mimeType === "application/pdf" ||
      (a.name && a.name.toLowerCase().indexOf(".pdf") !== -1)
    ) {
      return a;
    }
  }
  return null;
}

function obterUrlPdf(attachment) {
  // attachmentDataRef tem o resource name — precisamos do conteúdo
  var ref = attachment.attachmentDataRef;
  if (ref && ref.resourceName) {
    // Baixa via Chat API Media e converte para URL temporária no Drive
    return baixarAnexoParaDrive(ref.resourceName);
  }
  // Fallback: URL direta (quando disponível)
  return attachment.downloadUri || attachment.downloadUrl || attachment.source || null;
}

function baixarAnexoParaDrive(resourceName) {
  /**
   * Baixa o anexo do Chat via API de Media, salva temporariamente no Drive
   * e retorna uma URL pública temporária para o Render baixar.
   */
  var token = ScriptApp.getOAuthToken();
  var mediaUrl = "https://chat.googleapis.com/v1/" + resourceName + "?alt=media";

  var resp = UrlFetchApp.fetch(mediaUrl, {
    headers: { "Authorization": "Bearer " + token },
    muteHttpExceptions: true
  });

  if (resp.getResponseCode() !== 200) {
    Logger.log("Erro ao baixar anexo: " + resp.getResponseCode() + " " + resp.getContentText());
    return null;
  }

  // Salva no Drive temporariamente
  var blob = resp.getBlob().setName("decisao_temp_" + Date.now() + ".pdf");
  var file = DriveApp.createFile(blob);

  // Torna público por 1h para o Render conseguir baixar
  file.setSharing(DriveApp.Access.ANYONE_WITH_LINK, DriveApp.Permission.VIEW);

  // URL de download direto do Drive
  var fileId = file.getId();

  // Agenda exclusão após 10 minutos
  agendarExclusao(fileId);

  return "https://drive.google.com/uc?export=download&id=" + fileId;
}

function agendarExclusao(fileId) {
  // Salva ID para limpar depois
  var props = PropertiesService.getScriptProperties();
  var lista = JSON.parse(props.getProperty("tempFiles") || "[]");
  lista.push({ id: fileId, expira: Date.now() + 10 * 60 * 1000 });
  props.setProperty("tempFiles", JSON.stringify(lista));
}

function limparArquivosTemp() {
  // Execute este via trigger a cada hora
  var props = PropertiesService.getScriptProperties();
  var lista = JSON.parse(props.getProperty("tempFiles") || "[]");
  var agora = Date.now();
  var restantes = [];
  lista.forEach(function(f) {
    if (f.expira < agora) {
      try { DriveApp.getFileById(f.id).setTrashed(true); } catch(e) {}
    } else {
      restantes.push(f);
    }
  });
  props.setProperty("tempFiles", JSON.stringify(restantes));
}

function chamarRender(endpoint, payload) {
  var url = RENDER_URL + endpoint;
  UrlFetchApp.fetch(url, {
    method: "post",
    contentType: "application/json",
    payload: JSON.stringify(payload),
    muteHttpExceptions: true
  });
}

function chamarWebhook(texto) {
  UrlFetchApp.fetch(WEBHOOK_URL, {
    method: "post",
    contentType: "application/json",
    payload: JSON.stringify({ text: texto }),
    muteHttpExceptions: true
  });
}

// ---------------------------------------------------------------------------
// INSTALAÇÃO DOS TRIGGERS (execute uma vez manualmente)
// ---------------------------------------------------------------------------
function instalarTriggers() {
  // Remove triggers antigos
  ScriptApp.getProjectTriggers().forEach(function(t) {
    ScriptApp.deleteTrigger(t);
  });

  // Verifica mensagens a cada 1 minuto
  ScriptApp.newTrigger("verificarMensagens")
    .timeBased()
    .everyMinutes(1)
    .create();

  // Limpa arquivos temporários a cada hora
  ScriptApp.newTrigger("limparArquivosTemp")
    .timeBased()
    .everyHours(1)
    .create();

  Logger.log("✅ Triggers instalados com sucesso!");
}

// ---------------------------------------------------------------------------
// TESTE MANUAL — execute no editor para testar sem esperar o trigger
// ---------------------------------------------------------------------------
function testarConexao() {
  var resp = UrlFetchApp.fetch(RENDER_URL + "/health", { muteHttpExceptions: true });
  Logger.log("Render status: " + resp.getResponseCode() + " — " + resp.getContentText());
}
