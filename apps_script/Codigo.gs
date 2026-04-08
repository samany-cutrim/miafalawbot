/**
 * Decisões FA Bot v3 — Google Forms + Comandos no Chat
 * Form ID: 1LM9J782lRb8-qO-8rJCyqJdBxWYiPx1lKF-DIT7zAm8
 */

var RENDER_URL  = "https://decisoesfabot.onrender.com";
var WEBHOOK_URL = "https://chat.googleapis.com/v1/spaces/AAQAZKvRf_I/messages?key=AIzaSyDdI0hCZtE6vySjMm-WEfRq3CPzqKqqsHI&token=Do__09wO3amdpFlT5Zs4aLkyIrhZ5ZaUBUFZNqOdxuE";
var FORM_ID     = "1LM9J782lRb8-qO-8rJCyqJdBxWYiPx1lKF-DIT7zAm8";
var SPACE_NAME  = "spaces/AAQAZKvRf_I";
var ULTIMA_LEITURA_KEY = "ultimaLeitura";

// ---------------------------------------------------------------------------
// TRIGGER 1: envio do formulário
// ---------------------------------------------------------------------------

function onFormSubmit(e) {
  try {
    var respostas = e.response.getItemResponses();
    var pdfFileId = null;
    var cliente   = "";
    var tipo      = "";
    var advogado  = "Advogado";

    try {
      var email = e.response.getRespondentEmail() || "";
      if (email) {
        advogado = email.split("@")[0];
        advogado = advogado.charAt(0).toUpperCase() + advogado.slice(1).toLowerCase();
      }
    } catch(err) {}

    for (var i = 0; i < respostas.length; i++) {
      var item     = respostas[i];
      var titulo   = item.getItem().getTitle().toLowerCase();
      var resposta = item.getResponse();

      if (titulo.indexOf("pdf") !== -1 || titulo.indexOf("decisão") !== -1 ||
          titulo.indexOf("decisao") !== -1 || titulo.indexOf("arquivo") !== -1) {
        if (resposta) pdfFileId = Array.isArray(resposta) ? resposta[0] : resposta;
      } else if (titulo.indexOf("nome") !== -1 || titulo.indexOf("advogado") !== -1) {
        if (resposta) advogado = resposta;
      } else if (titulo.indexOf("cliente") !== -1) {
        cliente = resposta || "";
      } else if (titulo.indexOf("tipo") !== -1) {
        tipo = resposta || "";
        if (tipo === "(detectar automaticamente)") tipo = "";
      }
    }

    Logger.log("Form: advogado=" + advogado + " cliente=" + cliente + " tipo=" + tipo + " fileId=" + pdfFileId);

    if (!pdfFileId) {
      chamarWebhook("Formulario recebido de *" + advogado + "* mas sem PDF. Por favor reenvie.");
      return;
    }

    var textoPdf = extrairTextoDoPdf(pdfFileId);
    if (!textoPdf || textoPdf.trim() === "") {
      chamarWebhook("Nao foi possivel extrair texto do PDF de *" + advogado + "*. Verifique se o PDF nao esta escaneado.");
      return;
    }

    var textoMsg = "";
    if (cliente) textoMsg += "Cliente: " + cliente + "\n";
    if (tipo)    textoMsg += "Tipo: " + tipo;

    chamarWebhook("Analisando decisao de *" + advogado + "*... Aguarde.");

    var resp = UrlFetchApp.fetch(RENDER_URL + "/processar-texto", {
      method: "post",
      contentType: "application/json",
      payload: JSON.stringify({
        texto_pdf:   textoPdf,
        advogado:    advogado,
        texto:       textoMsg,
        webhook_url: WEBHOOK_URL
      }),
      muteHttpExceptions: true
    });
    Logger.log("Render: " + resp.getResponseCode() + " " + resp.getContentText());

  } catch(err) {
    Logger.log("Erro onFormSubmit: " + err);
    chamarWebhook("Erro ao processar o formulario. Tente novamente.");
  }
}


// ---------------------------------------------------------------------------
// TRIGGER 2: comandos no Chat (polling a cada 1 min)
// ---------------------------------------------------------------------------

function verificarComandos() {
  var props = PropertiesService.getScriptProperties();
  var ultimaLeitura = props.getProperty(ULTIMA_LEITURA_KEY);

  if (!ultimaLeitura) {
    props.setProperty(ULTIMA_LEITURA_KEY, new Date().toISOString());
    return;
  }

  var mensagens = listarMensagensNovas(ultimaLeitura);
  if (!mensagens || mensagens.length === 0) return;

  props.setProperty(ULTIMA_LEITURA_KEY, new Date().toISOString());
  mensagens.forEach(function(msg) { processarComando(msg); });
}

function listarMensagensNovas(desde) {
  var token = ScriptApp.getOAuthToken();
  var filtro = 'createTime > "' + desde + '"';
  var url = "https://chat.googleapis.com/v1/" + SPACE_NAME + "/messages"
    + "?orderBy=createTime+asc&filter=" + encodeURIComponent(filtro);
  var resp = UrlFetchApp.fetch(url, {
    headers: { "Authorization": "Bearer " + token },
    muteHttpExceptions: true
  });
  if (resp.getResponseCode() !== 200) return [];
  return JSON.parse(resp.getContentText()).messages || [];
}

function processarComando(msg) {
  var rawText = msg.text || msg.argumentText || "";
  var texto = rawText.replace(/<[^>]+>/g, "").trim();
  var sender = msg.sender || {};
  if (sender.type === "BOT") return;

  var tl = texto.toLowerCase();

  if (tl.indexOf("/favoraveis") === 0 || tl.indexOf("favoraveis") === 0) {
    var tema = extrairTema(texto);
    chamarRender("/buscar", { tipo: "favoraveis", tema: tema, webhook_url: WEBHOOK_URL });
    return;
  }
  if (tl.indexOf("/desfavoraveis") === 0 || tl.indexOf("desfavoraveis") === 0) {
    var tema2 = extrairTema(texto);
    chamarRender("/buscar", { tipo: "desfavoraveis", tema: tema2, webhook_url: WEBHOOK_URL });
    return;
  }
  if (tl === "/ajuda" || tl === "ajuda") {
    chamarRender("/ajuda", { webhook_url: WEBHOOK_URL });
    return;
  }
  if (tl === "/link" || tl === "link") {
    chamarRender("/link", { webhook_url: WEBHOOK_URL });
    return;
  }
  if (tl === "/confirmar" || tl === "confirmar") {
    var advogado = (msg.sender || {}).displayName || "Advogado";
    chamarRender("/confirmar", { advogado: advogado, webhook_url: WEBHOOK_URL });
    return;
  }
  if (tl === "/cancelar" || tl === "cancelar") {
    var advogado2 = (msg.sender || {}).displayName || "Advogado";
    chamarRender("/cancelar", { advogado: advogado2, webhook_url: WEBHOOK_URL });
    return;
  }
  if (tl.indexOf("/corrigir") === 0 || tl.indexOf("corrigir") === 0) {
    var advogado3 = (msg.sender || {}).displayName || "Advogado";
    var instrucao = texto.replace(/^\/?(corrigir)\s*/i, "").trim();
    if (!instrucao) {
      chamarWebhook("⚠️ Informe o que corrigir. Exemplo:\n`/corrigir o resultado deve ser Desfavorável`");
      return;
    }
    chamarRender("/corrigir", { advogado: advogado3, instrucao: instrucao, webhook_url: WEBHOOK_URL });
    return;
  }
}

function extrairTema(texto) {
  var partes = texto.split(" ");
  partes.shift();
  return partes.join(" ").trim();
}


// ---------------------------------------------------------------------------
// EXTRAÇÃO DE TEXTO DO PDF VIA DRIVE
// ---------------------------------------------------------------------------

function extrairTextoDoPdf(fileId) {
  try {
    var file = DriveApp.getFileById(fileId);
    Logger.log("PDF: " + file.getName() + " | " + file.getSize() + " bytes");

    var resource = {
      title: "temp_decisao_" + Date.now(),
      mimeType: "application/vnd.google-apps.document"
    };
    var docFile = Drive.Files.insert(resource, file.getBlob(), { convert: true });
    var doc = DocumentApp.openById(docFile.id);
    var texto = doc.getBody().getText();
    Logger.log("Texto: " + texto.length + " chars");

    DriveApp.getFileById(docFile.id).setTrashed(true);
    return texto;
  } catch(err) {
    Logger.log("Erro extrairTextoDoPdf: " + err);
    return null;
  }
}


// ---------------------------------------------------------------------------
// HELPERS
// ---------------------------------------------------------------------------

function chamarWebhook(texto) {
  UrlFetchApp.fetch(WEBHOOK_URL, {
    method: "post",
    contentType: "application/json",
    payload: JSON.stringify({ text: texto }),
    muteHttpExceptions: true
  });
}

function chamarRender(endpoint, payload) {
  try {
    UrlFetchApp.fetch(RENDER_URL + endpoint, {
      method: "post",
      contentType: "application/json",
      payload: JSON.stringify(payload),
      muteHttpExceptions: true
    });
  } catch(e) { Logger.log("Erro chamarRender " + endpoint + ": " + e); }
}

function keepAlive() {
  UrlFetchApp.fetch(RENDER_URL + "/health", { muteHttpExceptions: true });
}


// ---------------------------------------------------------------------------
// INSTALAÇÃO
// ---------------------------------------------------------------------------

function instalarTriggers() {
  ScriptApp.getProjectTriggers().forEach(function(t) { ScriptApp.deleteTrigger(t); });

  // Trigger do formulário
  var form = FormApp.openById(FORM_ID);
  ScriptApp.newTrigger("onFormSubmit")
    .forForm(form)
    .onFormSubmit()
    .create();

  // Polling de comandos no Chat
  ScriptApp.newTrigger("verificarComandos")
    .timeBased()
    .everyMinutes(1)
    .create();

  // Keep-alive
  ScriptApp.newTrigger("keepAlive")
    .timeBased()
    .everyMinutes(10)
    .create();

  Logger.log("Triggers instalados com sucesso!");
}


// ---------------------------------------------------------------------------
// TESTES
// ---------------------------------------------------------------------------

function testarConexao() {
  var resp = UrlFetchApp.fetch(RENDER_URL + "/health", { muteHttpExceptions: true });
  Logger.log("Render: " + resp.getResponseCode() + " -- " + resp.getContentText());
}

function testarWebhook() {
  chamarWebhook("Teste de conexao do DecisionFA Bot - funcionando!");
}

function postLinkNoChat() {
  var form = FormApp.openById(FORM_ID);
  chamarWebhook(
    "*Decisão FA Bot* — Para registrar uma decisão acesse:\n\n" +
    form.getPublishedUrl() + "\n\n" +
    "_Anexe o PDF, informe o cliente e tipo (opcionais) e envie._"
  );
}

function autorizarTudo() {
  var arquivos = DriveApp.getFiles();
  Logger.log("Drive OK");
  UrlFetchApp.fetch(RENDER_URL + "/health", { muteHttpExceptions: true });
  Logger.log("URL OK");
  var token = ScriptApp.getOAuthToken();
  Logger.log("Token OK: " + token.substring(0, 20) + "...");
}

function debugSender() {
  var token = ScriptApp.getOAuthToken();
  var filtro = 'createTime > "2026-03-20T22:00:00Z"';
  var url = "https://chat.googleapis.com/v1/" + SPACE_NAME + "/messages"
    + "?orderBy=createTime+desc&filter=" + encodeURIComponent(filtro);
  var resp = UrlFetchApp.fetch(url, {
    headers: { "Authorization": "Bearer " + token },
    muteHttpExceptions: true
  });
  var msgs = JSON.parse(resp.getContentText()).messages || [];
  msgs.forEach(function(msg) {
    var sender = msg.sender || {};
    var texto = (msg.text || "").substring(0, 50);
    Logger.log("sender: " + JSON.stringify(sender) + " | texto: " + texto);
  });
}

function processarComando(msg) {
  var rawText = msg.text || msg.argumentText || "";
  var texto = rawText.replace(/<[^>]+>/g, "").trim();
  var sender = msg.sender || {};
  if (sender.type === "BOT") return;

  // Busca o displayName pelo ID do usuário
  var advogado = obterNomeUsuario(sender.name);

  var tl = texto.toLowerCase();

  if (tl.indexOf("/favoraveis") === 0 || tl.indexOf("favoraveis") === 0) {
    chamarRender("/buscar", { tipo: "favoraveis", tema: extrairTema(texto), webhook_url: WEBHOOK_URL });
    return;
  }
  if (tl.indexOf("/desfavoraveis") === 0 || tl.indexOf("desfavoraveis") === 0) {
    chamarRender("/buscar", { tipo: "desfavoraveis", tema: extrairTema(texto), webhook_url: WEBHOOK_URL });
    return;
  }
  if (tl === "/ajuda" || tl === "ajuda") {
    chamarRender("/ajuda", { webhook_url: WEBHOOK_URL });
    return;
  }
  if (tl === "/link" || tl === "link") {
    chamarRender("/link", { webhook_url: WEBHOOK_URL });
    return;
  }
  if (tl === "/confirmar" || tl === "confirmar") {
    chamarRender("/confirmar", { advogado: advogado, webhook_url: WEBHOOK_URL });
    return;
  }
  if (tl === "/cancelar" || tl === "cancelar") {
    chamarRender("/cancelar", { advogado: advogado, webhook_url: WEBHOOK_URL });
    return;
  }
  if (tl.indexOf("/corrigir") === 0 || tl.indexOf("corrigir") === 0) {
    var instrucao = extrairTema(texto);
    if (!instrucao) {
      chamarWebhook("Informe o que corrigir. Exemplo:\n/corrigir o resultado deve ser Desfavorável");
      return;
    }
    chamarRender("/corrigir", { advogado: advogado, instrucao: instrucao, webhook_url: WEBHOOK_URL });
    return;
  }
}

function obterNomeUsuario(userResourceName) {
  // Tenta buscar o perfil do usuário via People API
  if (!userResourceName) return "Advogado";
  
  // Usa cache para não buscar sempre
  var props = PropertiesService.getScriptProperties();
  var cacheKey = "user_" + userResourceName.replace("/", "_");
  var cached = props.getProperty(cacheKey);
  if (cached) return cached;

  try {
    var userId = userResourceName.replace("users/", "");
    var token = ScriptApp.getOAuthToken();
    var resp = UrlFetchApp.fetch(
      "https://people.googleapis.com/v1/people/" + userId + "?personFields=names,emailAddresses",
      { headers: { "Authorization": "Bearer " + token }, muteHttpExceptions: true }
    );
    if (resp.getResponseCode() === 200) {
      var data = JSON.parse(resp.getContentText());
      var names = data.names || [];
      if (names.length > 0) {
        var nome = names[0].displayName || names[0].givenName || "Advogado";
        props.setProperty(cacheKey, nome);
        return nome;
      }
      // Fallback: email
      var emails = data.emailAddresses || [];
      if (emails.length > 0) {
        var email = emails[0].value || "";
        var nome2 = email.split("@")[0];
        props.setProperty(cacheKey, nome2);
        return nome2;
      }
    }
    Logger.log("People API: " + resp.getResponseCode() + " " + resp.getContentText().substring(0, 200));
  } catch(e) {
    Logger.log("Erro obterNomeUsuario: " + e);
  }
  return "Advogado";
}

function verSessoesPendentes() {
  var resp = UrlFetchApp.fetch(RENDER_URL + "/debug-sessoes", { muteHttpExceptions: true });
  Logger.log("Sessoes: " + resp.getContentText());
}

function debugNomeUsuario() {
  var token = ScriptApp.getOAuthToken();
  var userId = "101462235680880137463"; // ID que apareceu no log anterior
  var resp = UrlFetchApp.fetch(
    "https://people.googleapis.com/v1/people/" + userId + "?personFields=names,emailAddresses",
    { headers: { "Authorization": "Bearer " + token }, muteHttpExceptions: true }
  );
  Logger.log("Status: " + resp.getResponseCode());
  Logger.log("Resposta: " + resp.getContentText());
}
