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
// CHAT APP NATIVO (com modal/dialog)
// ---------------------------------------------------------------------------

function onAddToSpace(e) {
  return respostaCardChatAppMenu();
}


function onMessage(e) {
  var text = ((e.message && e.message.text) || "").trim().toLowerCase();
  if (text === "/menu" || text === "menu" || text === "/ajuda" || text === "ajuda") {
    return respostaCardChatAppMenu();
  }
  if (text === "/busca" || text === "busca") {
    return respostaCardChatAppBusca();
  }
  return respostaCardChatAppMenu();
}


function onCardClick(e) {
  var invoked = getInvokedFunction(e);
  var userName = (e.user && e.user.displayName) ? e.user.displayName : "Advogado";

  if (invoked === "open_decision_dialog") {
    return respostaDialogNovaDecisao();
  }

  if (invoked === "submit_decision_dialog") {
    var cliente = getFormInputValue(e, "cliente");
    var tipo = getFormInputValue(e, "tipo_responsabilidade");
    var decisao = getFormInputValue(e, "decisao");

    if (!decisao) {
      return {
        text: "Informe o conteudo da decisao no modal para continuar."
      };
    }

    var metadados = "";
    if (cliente) metadados += "Cliente: " + cliente + "\n";
    if (tipo) metadados += "Tipo: " + tipo;

    var resp = chamarRenderSync("/processar-texto-sync", {
      texto_pdf: decisao,
      advogado: userName,
      texto: metadados
    });

    if (!resp || resp.status !== "ok" || !resp.resultado) {
      return {
        text: "Erro ao processar a decisao. Tente novamente."
      };
    }

    var resultadoAnalise = String(resp.resultado);
    var resultadoHtml = resultadoAnalise
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/\n/g, "<br>");

    return {
      cardsV2: [
        {
          cardId: "analise_iniciada",
          card: {
            header: getBaseCardHeader("Analise iniciada"),
            sections: [
              {
                widgets: [
                  {
                    textParagraph: {
                      text: resultadoHtml
                    }
                  },
                  {
                    buttonList: {
                      buttons: [
                        {
                          text: "Confirmar",
                          onClick: {
                            action: {
                              "function": "confirm_decision"
                            }
                          }
                        },
                        {
                          text: "Cancelar",
                          onClick: {
                            action: {
                              "function": "cancel_decision"
                            }
                          }
                        },
                        {
                          text: "Corrigir",
                          onClick: {
                            action: {
                              "function": "open_correction_dialog"
                            }
                          }
                        }
                      ]
                    }
                  }
                ]
              }
            ]
          }
        }
      ]
    };
  }

  if (invoked === "buscar_favoraveis") {
    chamarRender("/buscar", {
      tipo: "favoraveis",
      tema: "todos",
      webhook_url: WEBHOOK_URL
    });
    return { text: "Buscando precedentes favoraveis na planilha..." };
  }

  if (invoked === "open_busca_card") {
    return respostaCardChatAppBusca();
  }

  if (invoked === "buscar_desfavoraveis") {
    chamarRender("/buscar", {
      tipo: "desfavoraveis",
      tema: "todos",
      webhook_url: WEBHOOK_URL
    });
    return { text: "Buscando precedentes desfavoraveis na planilha..." };
  }

  if (invoked === "confirm_decision") {
    chamarRender("/confirmar", {
      advogado: userName,
      webhook_url: WEBHOOK_URL
    });
    return respostaCardChatAppEmail();
  }

  if (invoked === "cancel_decision") {
    chamarRender("/cancelar", {
      advogado: userName,
      webhook_url: WEBHOOK_URL
    });
    return { text: "Cancelamento enviado." };
  }

  if (invoked === "open_correction_dialog") {
    return respostaDialogCorrecao();
  }

  if (invoked === "submit_correction_dialog") {
    var instrucao = getFormInputValue(e, "instrucao_correcao");
    if (!instrucao) {
      return { text: "Informe o que corrigir para continuar." };
    }
    chamarRender("/corrigir", {
      advogado: userName,
      instrucao: instrucao,
      webhook_url: WEBHOOK_URL
    });
    return { text: "Correcao enviada para reanalise." };
  }

  if (invoked === "email_yes") {
    chamarRender("/sim", {
      advogado: userName,
      webhook_url: WEBHOOK_URL
    });
    return { text: "Gerando sugestao de e-mail..." };
  }

  if (invoked === "email_no") {
    chamarRender("/nao", {
      advogado: userName,
      webhook_url: WEBHOOK_URL
    });
    return { text: "Perfeito, e-mail dispensado." };
  }

  return { text: "Acao nao reconhecida." };
}


function getInvokedFunction(e) {
  try {
    if (e.common && e.common.invokedFunction) return e.common.invokedFunction;
    if (e.action && e.action.actionMethodName) return e.action.actionMethodName;
  } catch (err) {
    Logger.log("Erro getInvokedFunction: " + err);
  }
  return "";
}


function getFormInputValue(e, key) {
  try {
    var inputs = (e.common && e.common.formInputs) ? e.common.formInputs : {};
    var field = inputs[key] || {};
    var stringInputs = field.stringInputs || {};
    var values = stringInputs.value || [];
    return values.length ? String(values[0]).trim() : "";
  } catch (err) {
    Logger.log("Erro getFormInputValue(" + key + "): " + err);
    return "";
  }
}


function respostaCardChatAppMenu() {
  return {
    cardsV2: [
      {
        cardId: "chatapp_menu",
        card: {
          header: getBaseCardHeader("Menu principal"),
          sections: [
            {
              widgets: [
                {
                  textParagraph: {
                    text: "Clique em <b>Enviar decisao</b> para abrir o modal no Google Chat."
                  }
                },
                {
                  buttonList: {
                    buttons: [
                      {
                        text: "Enviar decisao",
                        onClick: {
                          action: {
                            "function": "open_decision_dialog"
                          }
                        }
                      },
                      {
                        text: "Busca de precedentes",
                        onClick: {
                          action: {
                            "function": "open_busca_card"
                          }
                        }
                      }
                    ]
                  }
                },
                {
                  decoratedText: {
                    text: "<font face=\"monospace\">/favoraveis [tema]</font> · <font face=\"monospace\">/desfavoraveis [tema]</font>",
                    startIcon: { knownIcon: "STAR" }
                  }
                }
              ]
            }
          ]
        }
      }
    ]
  };
}


function respostaCardChatAppBusca() {
  return {
    cardsV2: [
      {
        cardId: "chatapp_busca",
        card: {
          header: getBaseCardHeader("Busca de precedentes"),
          sections: [
            {
              widgets: [
                {
                  textParagraph: {
                    text: "Clique no tipo para buscar direto na planilha. Para tema especifico, use /favoraveis [tema] ou /desfavoraveis [tema]."
                  }
                },
                {
                  buttonList: {
                    buttons: [
                      {
                        text: "Favoraveis",
                        onClick: {
                          action: {
                            "function": "buscar_favoraveis"
                          }
                        }
                      },
                      {
                        text: "Desfavoraveis",
                        onClick: {
                          action: {
                            "function": "buscar_desfavoraveis"
                          }
                        }
                      }
                    ]
                  }
                }
              ]
            }
          ]
        }
      }
    ]
  };
}


function respostaCardChatAppEmail() {
  return {
    cardsV2: [
      {
        cardId: "chatapp_email",
        card: {
          header: getBaseCardHeader("Sugestao de e-mail"),
          sections: [
            {
              widgets: [
                {
                  textParagraph: {
                    text: "Deseja gerar sugestao de e-mail para o cliente?"
                  }
                },
                {
                  buttonList: {
                    buttons: [
                      {
                        text: "Sim",
                        onClick: {
                          action: {
                            "function": "email_yes"
                          }
                        }
                      },
                      {
                        text: "Nao",
                        onClick: {
                          action: {
                            "function": "email_no"
                          }
                        }
                      }
                    ]
                  }
                }
              ]
            }
          ]
        }
      }
    ]
  };
}


function respostaDialogCorrecao() {
  return {
    actionResponse: {
      type: "DIALOG",
      dialogAction: {
        dialog: {
          body: {
            sections: [
              {
                header: "Corrigir analise",
                widgets: [
                  {
                    textInput: {
                      name: "instrucao_correcao",
                      label: "O que deve ser corrigido?",
                      type: "MULTIPLE_LINE"
                    }
                  },
                  {
                    buttonList: {
                      buttons: [
                        {
                          text: "Enviar correcao",
                          onClick: {
                            action: {
                              "function": "submit_correction_dialog"
                            }
                          }
                        }
                      ]
                    }
                  }
                ]
              }
            ]
          }
        }
      }
    }
  };
}


function respostaDialogNovaDecisao() {
  var tipos = [
    "OL",
    "Nuvem",
    "Terceirizacao",
    "Subsidiaria",
    "Ex Funcionario",
    "Ex-Foodlovers",
    "Marketplace"
  ];

  return {
    actionResponse: {
      type: "DIALOG",
      dialogAction: {
        dialog: {
          body: {
            sections: [
              {
                header: "Nova decisao",
                widgets: [
                  {
                    textInput: {
                      name: "cliente",
                      label: "Cliente (opcional)",
                      type: "SINGLE_LINE"
                    }
                  },
                  {
                    selectionInput: {
                      name: "tipo_responsabilidade",
                      label: "Tipo de Responsabilidade",
                      type: "DROPDOWN",
                      items: tipos.map(function(t) {
                        return { text: t, value: t };
                      })
                    }
                  },
                  {
                    textInput: {
                      name: "decisao",
                      label: "Cole o texto da decisao",
                      type: "MULTIPLE_LINE"
                    }
                  },
                  {
                    buttonList: {
                      buttons: [
                        {
                          text: "Enviar decisao",
                          onClick: {
                            action: {
                              "function": "submit_decision_dialog"
                            }
                          }
                        }
                      ]
                    }
                  }
                ]
              }
            ]
          }
        }
      }
    }
  };
}


function getFormUrl() {
  try {
    return FormApp.openById(FORM_ID).getPublishedUrl();
  } catch (e) {
    Logger.log("Erro getFormUrl: " + e);
    return "";
  }
}


function getBaseCardHeader(subtitle) {
  return {
    title: "Mia Falaw Bot",
    subtitle: subtitle || "Fluxo sem admin"
  };
}


function getBotaoAbrirFormulario(texto) {
  var formUrl = getFormUrl();
  if (!formUrl) {
    return null;
  }
  return {
    text: texto || "Abrir formulario",
    onClick: {
      openLink: {
        url: formUrl
      }
    }
  };
}


function getBotaoBusca(textoBotao, comandoExemplo) {
  // Sem Chat App admin, o webhook so permite abrir links; usamos atalho visual.
  var base = "https://chat.google.com/?q=";
  return {
    text: textoBotao,
    onClick: {
      openLink: {
        url: base + encodeURIComponent(comandoExemplo)
      }
    }
  };
}


function enviarCardMenuPrincipal() {
  var botaoFormulario = getBotaoAbrirFormulario("Enviar decisao");
  var botoes = [];
  if (botaoFormulario) botoes.push(botaoFormulario);

  var card = {
    cardId: "menu_principal",
    card: {
      header: getBaseCardHeader("Menu principal"),
      sections: [
        {
          widgets: [
            {
              textParagraph: {
                text: "<b>Fluxo com botoes (modo Apps Script):</b><br>1) Clique em <b>Enviar decisao</b><br>2) Preencha o formulario (modal simulado)<br>3) Use os botoes/comandos de acao no chat"
              }
            },
            {
              buttonList: {
                buttons: botoes
              }
            },
            {
              textParagraph: {
                text: "<b>Busca de precedentes:</b>"
              }
            },
            {
              buttonList: {
                buttons: [
                  getBotaoBusca("Favoraveis", "/favoraveis vinculo empregaticio"),
                  getBotaoBusca("Desfavoraveis", "/desfavoraveis responsabilidade subsidiaria")
                ]
              }
            },
            {
              decoratedText: {
                topLabel: "Acoes rapidas",
                text: "<font face=\"monospace\">/confirmar</font> · <font face=\"monospace\">/cancelar</font> · <font face=\"monospace\">/corrigir ...</font><br><font face=\"monospace\">/favoraveis [tema]</font> · <font face=\"monospace\">/desfavoraveis [tema]</font>",
                startIcon: { knownIcon: "DESCRIPTION" }
              }
            }
          ]
        }
      ]
    },
    _fallback_text: "Use /menu para abrir os botoes e enviar decisao."
  };

  chamarWebhookCard(card);
}


function enviarCardBuscaPrecedentes() {
  var card = {
    cardId: "busca_precedentes",
    card: {
      header: getBaseCardHeader("Busca de precedentes"),
      sections: [
        {
          widgets: [
            {
              textParagraph: {
                text: "Escolha o tipo e depois informe o tema no chat. Exemplo: horas extras"
              }
            },
            {
              buttonList: {
                buttons: [
                  getBotaoBusca("Favoraveis", "/favoraveis horas extras"),
                  getBotaoBusca("Desfavoraveis", "/desfavoraveis horas extras")
                ]
              }
            },
            {
              decoratedText: {
                text: "<font face=\"monospace\">/favoraveis [tema]</font>",
                bottomLabel: "Busca precedentes favoraveis para a empresa",
                startIcon: { knownIcon: "STAR" }
              }
            },
            {
              decoratedText: {
                text: "<font face=\"monospace\">/desfavoraveis [tema]</font>",
                bottomLabel: "Busca precedentes desfavoraveis para a empresa",
                startIcon: { knownIcon: "STAR" }
              }
            }
          ]
        }
      ]
    },
    _fallback_text: "Busca: /favoraveis [tema] ou /desfavoraveis [tema]"
  };

  chamarWebhookCard(card);
}


function enviarCardAcoesPosAnalise() {
  var card = {
    cardId: "acoes_pos_analise",
    card: {
      header: getBaseCardHeader("Acoes da analise"),
      sections: [
        {
          widgets: [
            {
              textParagraph: {
                text: "Escolha uma acao apos revisar a analise:"
              }
            },
            {
              decoratedText: {
                text: "<font face=\"monospace\">/confirmar</font>",
                bottomLabel: "Salva na planilha",
                startIcon: { knownIcon: "CHECK_CIRCLE" }
              }
            },
            {
              decoratedText: {
                text: "<font face=\"monospace\">/cancelar</font>",
                bottomLabel: "Descarta a analise",
                startIcon: { knownIcon: "CANCEL" }
              }
            },
            {
              decoratedText: {
                text: "<font face=\"monospace\">/corrigir [instrucao]</font>",
                bottomLabel: "Reanalisa com ajuste",
                startIcon: { knownIcon: "EDIT" }
              }
            }
          ]
        }
      ]
    },
    _fallback_text: "Acoes: /confirmar, /cancelar, /corrigir [instrucao]"
  };

  chamarWebhookCard(card);
}


function enviarCardEmail() {
  var card = {
    cardId: "acoes_email",
    card: {
      header: getBaseCardHeader("Sugestao de e-mail"),
      sections: [
        {
          widgets: [
            {
              textParagraph: {
                text: "Deseja gerar sugestao de e-mail para o cliente?"
              }
            },
            {
              decoratedText: {
                text: "<font face=\"monospace\">/sim</font>",
                bottomLabel: "Gerar sugestao",
                startIcon: { knownIcon: "EMAIL" }
              }
            },
            {
              decoratedText: {
                text: "<font face=\"monospace\">/nao</font>",
                bottomLabel: "Dispensar",
                startIcon: { knownIcon: "EMAIL" }
              }
            }
          ]
        }
      ]
    },
    _fallback_text: "E-mail: /sim ou /nao"
  };

  chamarWebhookCard(card);
}


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

    // No fluxo com formulario, aguarda a analise chegar antes de qualquer acao.
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
  if (tl === "/busca" || tl === "busca") {
    chamarWebhook("Use os botoes de busca no card do Chat App ou os comandos /favoraveis [tema] e /desfavoraveis [tema].");
    return;
  }
  if (tl === "/menu" || tl === "menu") {
    chamarWebhook("Use o menu do Chat App para abrir os botoes interativos.");
    return;
  }
  if (tl === "/ajuda" || tl === "ajuda") {
    chamarRender("/ajuda", { webhook_url: WEBHOOK_URL });
    enviarCardMenuPrincipal();
    return;
  }
  if (tl === "/link" || tl === "link") {
    chamarRender("/link", { webhook_url: WEBHOOK_URL });
    return;
  }
  if (tl === "/confirmar" || tl === "confirmar") {
    chamarRender("/confirmar", { advogado: advogado, webhook_url: WEBHOOK_URL });
    enviarCardEmail();
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


function chamarWebhookCard(card) {
  try {
    var fallbackText = (card && card._fallback_text) ? card._fallback_text : "";
    var cardPayload = JSON.parse(JSON.stringify(card || {}));
    delete cardPayload._fallback_text;

    var resp = UrlFetchApp.fetch(WEBHOOK_URL, {
      method: "post",
      contentType: "application/json",
      payload: JSON.stringify({ cardsV2: [cardPayload] }),
      muteHttpExceptions: true
    });
    if (resp.getResponseCode() !== 200 && resp.getResponseCode() !== 201) {
      Logger.log("Erro chamarWebhookCard: " + resp.getResponseCode() + " " + resp.getContentText());
      if (fallbackText) {
        chamarWebhook(fallbackText);
      }
    }
  } catch (e) {
    Logger.log("Erro chamarWebhookCard: " + e);
    if (card && card._fallback_text) {
      chamarWebhook(card._fallback_text);
    }
  }
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


function chamarRenderSync(endpoint, payload) {
  try {
    var resp = UrlFetchApp.fetch(RENDER_URL + endpoint, {
      method: "post",
      contentType: "application/json",
      payload: JSON.stringify(payload),
      muteHttpExceptions: true
    });
    var code = resp.getResponseCode();
    var body = resp.getContentText();
    if (code >= 200 && code < 300) {
      return JSON.parse(body || "{}");
    }
    Logger.log("Erro chamarRenderSync " + endpoint + ": " + code + " " + body);
    return null;
  } catch (e) {
    Logger.log("Erro chamarRenderSync " + endpoint + ": " + e);
    return null;
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
  enviarCardMenuPrincipal();
}
