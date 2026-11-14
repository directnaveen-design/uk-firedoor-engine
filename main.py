function getConfig_() {
  const props = PropertiesService.getScriptProperties();
  return {
    SUPABASE_URL: (props.getProperty("SUPABASE_URL") || "https://zrphihrvlknxqlhgsqou.supabase.co").replace(/\/$/, ""),
    SUPABASE_SECRET_KEY: props.getProperty("SUPABASE_SECRET_KEY"),
    RAILWAY_URL: (props.getProperty("RAILWAY_URL") || "https://uk-firedoor-engine-production.up.railway.app").replace(/\/$/, ""),
    WEBHOOK_SECRET: props.getProperty("WEBHOOK_SECRET"),
    ALLOWED_SENDERS: props.getProperty("ALLOWED_SENDERS") || "",
    DELIVERY_MODE: props.getProperty("DELIVERY_MODE") || "AUTO_SEND",
    STANDARD_VERSION: "BS 8214:2016",
    DONE_LABEL: "FireDoor-JobSent",
    ERROR_LABEL: "FireDoor-Error"
  };
}

function checkIncomingSurveys() {
  const cfg = getConfig_();
  if (!cfg.SUPABASE_SECRET_KEY || !cfg.WEBHOOK_SECRET) {
    throw new Error("Missing SUPABASE_SECRET_KEY or WEBHOOK_SECRET in Script Properties!");
  }

  const allowedList = cfg.ALLOWED_SENDERS
    .split(",")
    .map(function(s) { return s.trim().toLowerCase(); })
    .filter(function(s) { return s.length > 0; });

  if (allowedList.length === 0) {
    Logger.log("ALLOWED_SENDERS Script Property is empty or missing. Allowing no one.");
    return;
  }

  const myEmail = (Session.getEffectiveUser().getEmail() || "").trim().toLowerCase();
  const doneLabel = GmailApp.getUserLabelByName(cfg.DONE_LABEL) || GmailApp.createLabel(cfg.DONE_LABEL);
  const errorLabel = GmailApp.getUserLabelByName(cfg.ERROR_LABEL) || GmailApp.createLabel(cfg.ERROR_LABEL);

  const query = 'in:inbox is:unread newer_than:2h ("fire door" OR "firedoor" OR "BS 8214") -label:' + cfg.DONE_LABEL + ' -label:' + cfg.ERROR_LABEL;
  const threads = GmailApp.search(query, 0, 5);

  for (const thread of threads) {
    const messages = thread.getMessages();
    const lastMsg = messages[messages.length - 1];
    const subject = lastMsg.getSubject() || "";
    const fromRaw = lastMsg.getFrom() || "";
    const senderEmail = extractEmail_(fromRaw);

    if (lastMsg.isDraft() || senderEmail === myEmail) {
      thread.markRead();
      continue;
    }

    if (/no-?reply|donotreply|payoneer|linkedin|google|github|railway|supabase|vercel|newsletter|notification|alert|mailer-daemon|updates@|marketing@/i.test(senderEmail)) {
      thread.markRead();
      continue;
    }

    const isAllowed = allowedList.some(function(entry) {
      if (entry.charAt(0) === "@") return senderEmail.endsWith(entry);
      return senderEmail === entry;
    });
    if (!isAllowed) {
      Logger.log("Ignoring unauthorized sender: " + senderEmail);
      continue;
    }

    let pdfBlob = null;
    let clientMessagesText = "";
    for (let i = 0; i < messages.length; i++) {
      const msg = messages[i];
      const msgSender = extractEmail_(msg.getFrom() || "");
      if (msgSender === myEmail || msg.isDraft()) continue;

      clientMessagesText += "\n" + stripQuotedText_(msg.getPlainBody() || "");
      const atts = msg.getAttachments();
      for (const a of atts) {
        if (a.getContentType() === "application/pdf" || a.getName().toLowerCase().endsWith(".pdf")) {
          pdfBlob = a;
        }
      }
    }

    if (!/fire\s*door|bs\s*8214/i.test(subject + " " + clientMessagesText)) continue;

    const senderName = extractSenderName_(fromRaw);
    const analysis = analyzeEmailSmart_(subject, clientMessagesText, pdfBlob !== null, senderName);

    thread.markRead();

    if (!analysis.is_complete || !pdfBlob) {
      thread.reply(analysis.human_reply_if_missing);
      continue;
    }

    const jobId = "fd_" + lastMsg.getId();
    const rawPdfPath = jobId + "/survey.pdf";

    thread.addLabel(doneLabel);
    try {
      const sbJobResp = UrlFetchApp.fetch(cfg.SUPABASE_URL + "/rest/v1/compliance_jobs", {
        method: "post",
        contentType: "application/json",
        headers: { apikey: cfg.SUPABASE_SECRET_KEY, Authorization: "Bearer " + cfg.SUPABASE_SECRET_KEY },
        payload: JSON.stringify({
          job_id: jobId,
          gmail_thread_id: thread.getId(),
          sender_email: senderEmail,
          client_name: analysis.client_name,
          building_name: analysis.building_name,
          hourly_rate: Number(analysis.hourly_rate),
          material_markup: Number(analysis.material_markup),
          standard_version: cfg.STANDARD_VERSION,
          status: "pending"
        })
      });
      if (sbJobResp.getResponseCode() >= 300) {
        throw new Error("Supabase DB insert failed (" + sbJobResp.getResponseCode() + "): " + sbJobResp.getContentText());
      }

      const sbStorageResp = UrlFetchApp.fetch(cfg.SUPABASE_URL + "/storage/v1/object/raw-surveys/" + rawPdfPath, {
        method: "post",
        contentType: "application/pdf",
        headers: { apikey: cfg.SUPABASE_SECRET_KEY, Authorization: "Bearer " + cfg.SUPABASE_SECRET_KEY },
        payload: pdfBlob.getBytes()
      });
      if (sbStorageResp.getResponseCode() >= 300) {
        throw new Error("Supabase Storage upload failed (" + sbStorageResp.getResponseCode() + "): " + sbStorageResp.getContentText());
      }

      const railwayResp = UrlFetchApp.fetch(cfg.RAILWAY_URL + "/api/v1/process-job", {
        method: "post",
        contentType: "application/json",
        headers: { "x-webhook-secret": cfg.WEBHOOK_SECRET },
        payload: JSON.stringify({
          job_id: jobId,
          gmail_thread_id: thread.getId(),
          sender_email: senderEmail,
          client_name: analysis.client_name,
          building_name: analysis.building_name,
          hourly_rate: Number(analysis.hourly_rate),
          material_markup: Number(analysis.material_markup),
          site_fee: Number(analysis.site_fee),
          vat_rate: 0.20,
          standard_version: cfg.STANDARD_VERSION,
          raw_pdf_path: rawPdfPath
        })
      });
      if (railwayResp.getResponseCode() >= 300) {
        throw new Error("Railway trigger failed (" + railwayResp.getResponseCode() + "): " + railwayResp.getContentText());
      }
    } catch (err) {
      thread.removeLabel(doneLabel);
      thread.addLabel(errorLabel);
      MailApp.sendEmail(
        myEmail,
        "FireDoor Pipeline Error [" + jobId + "]",
        "An error occurred while dispatching job_id: " + jobId + "\n\nError details:\n" + err.toString()
      );
    }
  }
}

function doPost(e) {
  try {
    const cfg = getConfig_();
    const incomingSecret = (e && e.parameter && e.parameter.secret) ? e.parameter.secret : "";
    if (incomingSecret !== cfg.WEBHOOK_SECRET) {
      return ContentService.createTextOutput(JSON.stringify({ ok: false, error: "Unauthorized" })).setMimeType(ContentService.MimeType.JSON);
    }

    const data = JSON.parse(e.postData.contents);
    if (data.status !== "completed") {
      return ContentService.createTextOutput(JSON.stringify({ ok: false })).setMimeType(ContentService.MimeType.JSON);
    }

    const headers = { apikey: cfg.SUPABASE_SECRET_KEY, Authorization: "Bearer " + cfg.SUPABASE_SECRET_KEY };
    const xlsxBlob = UrlFetchApp.fetch(cfg.SUPABASE_URL + "/storage/v1/object/deliverables/" + data.xlsx_path, { headers }).getBlob().setName("UK_FireDoor_Compliance_Pack.xlsx");
    const pdfBlob = UrlFetchApp.fetch(cfg.SUPABASE_URL + "/storage/v1/object/deliverables/" + data.pdf_path, { headers }).getBlob().setName("UK_FireDoor_PreWork_Report.pdf");

    const thread = GmailApp.getThreadById(data.gmail_thread_id);
    const replyBody = "Hi " + data.client_name + ",\n\nPlease find attached your BS 8214:2016 Remediation Pack and Pre-Work Report for " + data.building_name + " (" + data.failed_count + " doors actioned out of " + data.total_surveyed + " surveyed).\n\nBest regards,\nCompliance Team";

    if (cfg.DELIVERY_MODE === "AUTO_SEND") {
      thread.reply(replyBody, { attachments: [xlsxBlob, pdfBlob] });
    } else {
      thread.createDraftReply(replyBody, { attachments: [xlsxBlob, pdfBlob] });
    }

    return ContentService.createTextOutput(JSON.stringify({ ok: true })).setMimeType(ContentService.MimeType.JSON);
  } catch (err) {
    return ContentService.createTextOutput(JSON.stringify({ ok: false, error: err.toString() })).setMimeType(ContentService.MimeType.JSON);
  }
}

function stripQuotedText_(text) {
  const lines = text.split(/\r?\n/);
  const cleaned = [];
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const trimmed = line.trim();
    if (/^On\s+.+\s+wrote:\s*$/i.test(trimmed) || /^-----Original Message-----/i.test(trimmed) || /^From:\s+/i.test(trimmed)) break;
    if (trimmed.charAt(0) === ">") continue;
    cleaned.push(line);
  }
  return cleaned.join("\n").trim();
}

function extractValidHourlyRate_(text) {
  const regex = /(?:(?:£|GBP)\s*(\d+(?:\.\d{1,2})?)\s*(?:\/\s*hr|\/\s*hour|per\s*hour|per\s*hr|p\/h|hourly)|(\d+(?:\.\d{1,2})?)\s*(?:GBP|£)\s*(?:\/\s*hr|\/\s*hour|per\s*hour|per\s*hr|p\/h|hourly))/gi;
  const distinctRates = {};
  let match;
  while ((match = regex.exec(text)) !== null) {
    const rawVal = parseFloat(match[1] || match[2]);
    if (!isNaN(rawVal) && rawVal >= 15 && rawVal <= 200) {
      distinctRates[rawVal.toFixed(2)] = rawVal;
    }
  }
  const keys = Object.keys(distinctRates);
  if (keys.length === 1) return { rate: distinctRates[keys[0]], ambiguous: false };
  if (keys.length > 1) return { rate: null, ambiguous: true };
  return { rate: null, ambiguous: false };
}

function extractMaterialMarkup_(text) {
  const match = text.match(/(\d+(?:\.\d{1,2})?)\s*%\s*(?:material\s*)?markup/i) || text.match(/markup\s*(?:of|:)?\s*(\d+(?:\.\d{1,2})?)\s*%/i);
  if (match) {
    const pct = parseFloat(match[1]);
    if (!isNaN(pct) && pct >= 0 && pct <= 100) return pct / 100.0;
  }
  return 0.15;
}

function extractSiteFee_(text) {
  const match = text.match(/£\s*(\d+(?:\.\d{1,2})?)\s*(?:site\s*setup|site\s*fee|admin\s*fee|certification\s*fee|setup\s*fee|site\s*setup\s*\/\s*certification\s*fee)/i)
    || text.match(/(?:site\s*setup|site\s*fee|admin\s*fee|certification\s*fee)\s*(?:of|:)?\s*£\s*(\d+(?:\.\d{1,2})?)/i);
  if (match) {
    const fee = parseFloat(match[1]);
    if (!isNaN(fee) && fee >= 0 && fee <= 5000) return fee;
  }
  return 0.0;
}

function analyzeEmailSmart_(subject, clientText, hasPdf, fallbackSenderName) {
  const combined = subject + "\n" + clientText;
  const rateResult = extractValidHourlyRate_(combined);
  const hourlyRate = rateResult.rate;
  const materialMarkup = extractMaterialMarkup_(combined);
  const siteFee = extractSiteFee_(combined);

  const siteMatch = combined.match(/(?:for|at|site:?)\s+([A-Z][A-Za-z0-9\s\-',()]{2,50}?)(?:\.|,|\n|Our|Please|$)/);
  const buildingName = siteMatch ? siteMatch[1].trim() : (subject.replace(/Re:|Fwd:|Fire Door|Survey|Quote|Tender Pricing & Admin Pack|-/gi, "").trim() || "Commercial Site");

  const clientMatch = combined.match(/from\s+([A-Z][A-Za-z0-9\s&]+?(?:Ltd|Limited|Solutions|Contractors|Fire|Group|Services))/i);
  const clientName = clientMatch ? clientMatch[1].trim() : (fallbackSenderName || "there");

  const greetingName = fallbackSenderName || clientName || "there";
  const isComplete = Boolean(hasPdf && hourlyRate !== null);
  let missingReply = "";

  const ratePromptText = rateResult.ambiguous
    ? "confirm a single joiner's hourly rate (as multiple rates were mentioned)"
    : "confirm your joiner's hourly rate (e.g. £55/hr)";

  if (!hasPdf && hourlyRate === null) {
    missingReply = "Hi " + greetingName + ",\n\nThanks for getting in touch regarding " + buildingName + ". To prepare your BS 8214:2016 remediation pack and dynamic quote, could you please reply with the Fire Door Inspection Survey PDF attached and " + ratePromptText + "?\n\nBest regards,\nCompliance Team";
  } else if (!hasPdf) {
    missingReply = "Hi " + greetingName + ",\n\nThanks for confirming your £" + hourlyRate + "/hr joinery rate for " + buildingName + ". It looks like the Fire Door Survey PDF attachment didn't come through—could you please reply and attach the survey PDF so we can generate your BS 8214:2016 pack right away?\n\nBest regards,\nCompliance Team";
  } else if (hourlyRate === null) {
    missingReply = "Hi " + greetingName + ",\n\nThanks for sending over the survey PDF for " + buildingName + ". Before we generate your 4-tab Excel quote calculator and BS 8214:2016 report, could you quickly reply to " + ratePromptText + "?\n\nBest regards,\nCompliance Team";
  }

  return {
    is_complete: isComplete,
    client_name: clientName,
    building_name: buildingName,
    hourly_rate: hourlyRate,
    material_markup: materialMarkup,
    site_fee: siteFee,
    human_reply_if_missing: missingReply
  };
}

function extractEmail_(fromStr) {
  const match = fromStr.match(/<([^>]+)>/);
  return (match ? match[1] : fromStr).trim().toLowerCase();
}

function extractSenderName_(fromStr) {
  const namePart = fromStr.split("<")[0].replace(/["']/g, "").trim();
  if (!namePart || namePart.indexOf("@") !== -1) return "there";
  return namePart.split(" ")[0];
}
