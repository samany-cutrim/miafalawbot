/**
 * Mia Falaw Bot - Apps Script (modo sem admin)
 *
 * Fluxo:
 * 1) Trigger de formulario envia decisao para o backend.
 * 2) Trigger time-based faz polling de mensagens no Chat e processa comandos.
 */

var RENDER_URL = "https://SEU-APP.onrender.com";
var WEBHOOK_URL = "https://chat.googleapis.com/v1/spaces/SEU_SPACE/messages?key=SUA_KEY&token=SEU_TOKEN";
var FORM_ID = "SEU_FORM_ID";
var SPACE_NAME = "spaces/SEU_SPACE";
var ULTIMA_LEITURA_KEY = "ultimaLeitura";


// ---------------------------------------------------------------------------
// TRIGGER 1: envio do formulario
// ---------------------------------------------------------------------------

function onFormSubmit(e) {
  try {
    var respostas = e.response.getItemResponses();
    var pdfFileId = null;
    var cliente = "";
    var tipo = "";
    var advogado = "Advogado";

    try {
      var email = e.response.getRespondentEmail() || "";
      if (email) {
        advogado = email.split("@")[0];
        advogado = advogado.charAt(0).toUpperCase() + advogado.slice(1).toLowerCase();
      }
    } catch (err) {}

    for (var i = 0; i < respostas.length; i++) {
      var item = respostas[i];
      var titulo = item.getItem().getTitle().toLowerCase();
      var resposta = item.getResponse();

      if (titulo.indexOf("pdf") !== -1 || titulo.indexOf("decis") !== -1 || titulo.indexOf("arquivo") !== -1) {
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

    if (!pdfFileId) {
      chamarWebhook("Formulario recebido de *" + advogado + "* sem PDF. Reenvie o formulario.");
      return;
    }

    var textoPdf = extrairTextoDoPdf(pdfFileId);
    if (!textoPdf || textoPdf.trim() === "") {
      chamarWebhook("Nao foi possivel extrair texto do PDF de *" + advogado + "*. Verifique se o PDF nao esta escaneado.");
      return;
    }

    var metadados = "";
    if (cliente) metadados += "Cliente: " + cliente + "\n";
    if (tipo) metadados += "Tipo: " + tipo;

    chamarWebhook("Analisando decisao de *" + advogado + "*... Aguarde.");

    UrlFetchApp.fetch(RENDER_URL + "/processar-texto", {
      method: "post",
      contentType: "application/json",
      payload: JSON.stringify({
        texto_pdf: textoPdf,
        advogado: advogado,
        texto: metadados,
        webhook_url: WEBHOOK_URL
      }),
      muteHttpExceptions: true
    });
  } catch (err) {
    Logger.log("Erro onFormSubmit: " + err);
    chamarWebhook("Erro ao processar o formulario. Tente novamente.");
  }
}


// ---------------------------------------------------------------------------
// TRIGGER 2: polling de comandos no Google Chat
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
  mensagens.forEach(function(msg) {
    processarComando(msg);
  });
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

  if (resp.getResponseCode() !== 200) {
    Logger.log("Erro listarMensagensNovas: " + resp.getResponseCode() + " " + resp.getContentText());
    return [];
  }

  return JSON.parse(resp.getContentText()).messages || [];
}

function processarComando(msg) {
  var sender = msg.sender || {};
  if (sender.type === "BOT") return;

  var rawText = msg.argumentText || msg.text || "";
  var texto = rawText.replace(/<[^>]+>/g, "").trim();
  if (!texto) return;

  var tl = texto.toLowerCase();
  var advogado = sender.displayName || "Advogado";

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
  if (tl.indexOf("/sim") === 0 || tl === "sim") {
    chamarRender("/sim", { advogado: advogado, webhook_url: WEBHOOK_URL });
    return;
  }
  if (tl.indexOf("/nao") === 0 || tl === "nao") {
    chamarRender("/nao", { advogado: advogado, webhook_url: WEBHOOK_URL });
    return;
  }
  if (tl.indexOf("/corrigir") === 0 || tl.indexOf("corrigir") === 0) {
    var instrucao = texto.replace(/^\/?corrigir\s*/i, "").trim();
    if (!instrucao) {
      chamarWebhook("Informe o que corrigir. Exemplo: /corrigir o resultado deve ser Desfavoravel");
      return;
    }
    chamarRender("/corrigir", { advogado: advogado, instrucao: instrucao, webhook_url: WEBHOOK_URL });
  }
}

function extrairTema(texto) {
  var partes = texto.split(" ");
  partes.shift();
  return partes.join(" ").trim();
}


// ---------------------------------------------------------------------------
// EXTRAIR TEXTO DO PDF VIA DRIVE
// ---------------------------------------------------------------------------

function extrairTextoDoPdf(fileId) {
  try {
    var file = DriveApp.getFileById(fileId);

    var resource = {
      title: "temp_decisao_" + Date.now(),
      mimeType: "application/vnd.google-apps.document"
    };

    var docFile = Drive.Files.insert(resource, file.getBlob(), { convert: true });
    var doc = DocumentApp.openById(docFile.id);
    var texto = doc.getBody().getText();

    DriveApp.getFileById(docFile.id).setTrashed(true);
    return texto;
  } catch (err) {
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
  } catch (e) {
    Logger.log("Erro chamarRender " + endpoint + ": " + e);
  }
}

function keepAlive() {
  UrlFetchApp.fetch(RENDER_URL + "/health", { muteHttpExceptions: true });
}


// ---------------------------------------------------------------------------
// INSTALACAO
// ---------------------------------------------------------------------------

function instalarTriggers() {
  ScriptApp.getProjectTriggers().forEach(function(t) { ScriptApp.deleteTrigger(t); });

  var form = FormApp.openById(FORM_ID);
  ScriptApp.newTrigger("onFormSubmit").forForm(form).onFormSubmit().create();
  ScriptApp.newTrigger("verificarComandos").timeBased().everyMinutes(1).create();
  ScriptApp.newTrigger("keepAlive").timeBased().everyMinutes(10).create();

  Logger.log("Triggers instalados com sucesso.");
}


// ---------------------------------------------------------------------------
// TESTES
// ---------------------------------------------------------------------------

function testarConexao() {
  var resp = UrlFetchApp.fetch(RENDER_URL + "/health", { muteHttpExceptions: true });
  Logger.log("Render: " + resp.getResponseCode() + " -- " + resp.getContentText());
}

function testarWebhook() {
  chamarWebhook("Teste de conexao do Mia Falaw Bot - funcionando.");
}

function postLinkNoChat() {
  var form = FormApp.openById(FORM_ID);
  chamarWebhook(
    "*Mia Falaw Bot* - Para registrar uma decisao acesse:\n\n" +
    form.getPublishedUrl() + "\n\n" +
    "_Anexe o PDF, informe cliente/tipo e envie._"
  );
}
