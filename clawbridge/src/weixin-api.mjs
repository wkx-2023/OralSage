import { createCipheriv, createDecipheriv, createHash, randomBytes, randomUUID } from "node:crypto";

const DEFAULT_BOT_TYPE = "3";
const MESSAGE_TYPE_USER = 1;
const MESSAGE_TYPE_BOT = 2;
const MESSAGE_STATE_FINISH = 2;
const MESSAGE_ITEM_TYPE_TEXT = 1;
const MESSAGE_ITEM_TYPE_IMAGE = 2;
const MESSAGE_ITEM_TYPE_VOICE = 3;
const MESSAGE_ITEM_TYPE_FILE = 4;
const UPLOAD_MEDIA_TYPE_FILE = 3;
const CDN_UPLOAD_MAX_RETRIES = 3;
export const WeixinTypingStatus = Object.freeze({
  TYPING: 1,
  CANCEL: 2
});

function trimBaseUrl(value) {
  return String(value ?? "").trim().replace(/\/+$/, "");
}

function ensureJsonObject(value, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} returned an unexpected payload.`);
  }
  return value;
}

function buildCdnUploadUrl({ cdnBaseUrl, uploadParam, fileKey }) {
  return `${trimBaseUrl(cdnBaseUrl)}/upload?encrypted_query_param=${encodeURIComponent(uploadParam)}&filekey=${encodeURIComponent(fileKey)}`;
}

function buildCdnDownloadUrl({ cdnBaseUrl, encryptedQueryParam }) {
  return `${trimBaseUrl(cdnBaseUrl)}/download?encrypted_query_param=${encodeURIComponent(encryptedQueryParam)}`;
}

function aesEcbPaddedSize(size) {
  return Math.ceil((size + 1) / 16) * 16;
}

function encryptAesEcb(buffer, key) {
  const cipher = createCipheriv("aes-128-ecb", key, null);
  return Buffer.concat([cipher.update(buffer), cipher.final()]);
}

function decryptAesEcb(buffer, key) {
  const decipher = createDecipheriv("aes-128-ecb", key, null);
  return Buffer.concat([decipher.update(buffer), decipher.final()]);
}

function parseAesKeyBase64(aesKeyBase64) {
  const decoded = Buffer.from(aesKeyBase64, "base64");
  if (decoded.length === 16) {
    return decoded;
  }
  if (decoded.length === 32 && /^[0-9a-f]{32}$/i.test(decoded.toString("ascii"))) {
    return Buffer.from(decoded.toString("ascii"), "hex");
  }
  throw new Error("The inbound image aes_key is not a supported Weixin encoding.");
}

function getInboundImageKey(image) {
  const aesKeyHex = String(image?.aesKeyHex ?? "").trim();
  if (aesKeyHex) {
    if (!/^[0-9a-f]{32}$/i.test(aesKeyHex)) {
      throw new Error("The inbound image aeskey is not a valid 16-byte hex string.");
    }
    return Buffer.from(aesKeyHex, "hex");
  }
  const aesKeyBase64 = String(image?.aesKey ?? "").trim();
  if (!aesKeyBase64) {
    return null;
  }
  return parseAesKeyBase64(aesKeyBase64);
}

function normalizeMimeType(value) {
  return String(value ?? "")
    .split(";", 1)[0]
    .trim()
    .toLowerCase();
}

function detectImageMimeType(buffer) {
  if (buffer.length >= 8 && buffer.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))) {
    return "image/png";
  }
  if (buffer.length >= 3 && buffer[0] === 0xff && buffer[1] === 0xd8 && buffer[2] === 0xff) {
    return "image/jpeg";
  }
  if (buffer.length >= 6) {
    const header = buffer.subarray(0, 6).toString("ascii");
    if (header === "GIF87a" || header === "GIF89a") {
      return "image/gif";
    }
  }
  if (
    buffer.length >= 12 &&
    buffer.subarray(0, 4).toString("ascii") === "RIFF" &&
    buffer.subarray(8, 12).toString("ascii") === "WEBP"
  ) {
    return "image/webp";
  }
  if (buffer.length >= 2 && buffer.subarray(0, 2).toString("ascii") === "BM") {
    return "image/bmp";
  }
  return "";
}

function resolveInboundImageMimeType(buffer, responseMimeType) {
  const detectedMimeType = detectImageMimeType(buffer);
  if (detectedMimeType) {
    return detectedMimeType;
  }
  const normalizedResponseMimeType = normalizeMimeType(responseMimeType);
  if (normalizedResponseMimeType.startsWith("image/")) {
    return normalizedResponseMimeType;
  }
  throw new Error("The inbound image did not decode into a supported image format.");
}

async function requestJson({ method, url, headers, body, timeoutMs }) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, {
      method,
      headers,
      body,
      signal: controller.signal
    });
    const text = await response.text();
    let payload = null;
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch {
        throw new Error(`Weixin iLink returned a non-JSON response for ${url}.`);
      }
    }
    if (!response.ok) {
      const message =
        typeof payload?.errmsg === "string" && payload.errmsg.trim()
          ? payload.errmsg.trim()
          : `Weixin iLink request failed with status ${response.status}.`;
      throw new Error(message);
    }
    return ensureJsonObject(payload ?? {}, url);
  } finally {
    clearTimeout(timeout);
  }
}

function buildPostHeaders(body) {
  return {
    "Content-Type": "application/json",
    "Content-Length": String(Buffer.byteLength(body, "utf-8"))
  };
}

function buildAuthHeaders(token, body) {
  const headers = buildPostHeaders(body);
  if (token?.trim()) {
    headers.AuthorizationType = "ilink_bot_token";
    headers.Authorization = `Bearer ${token.trim()}`;
  }
  return headers;
}

function buildBaseInfo() {
  return {
    channel_version: "alertclaw-clawbridge"
  };
}

async function sendMessageRequest({
  baseUrl,
  token,
  body,
  timeoutMs
}) {
  const target = `${trimBaseUrl(baseUrl)}/ilink/bot/sendmessage`;
  await requestJson({
    method: "POST",
    url: target,
    headers: buildAuthHeaders(token, body),
    body,
    timeoutMs
  });
}

async function sendMessageItems({
  baseUrl,
  token,
  toUserId,
  text,
  contextToken,
  timeoutMs,
  items
}) {
  let lastClientId = "";
  const queue = [];
  if (text) {
    queue.push({
      type: MESSAGE_ITEM_TYPE_TEXT,
      text_item: { text }
    });
  }
  queue.push(...items);

  for (const item of queue) {
    lastClientId = `alertclaw-clawbridge-${randomUUID()}`;
    const body = JSON.stringify({
      msg: {
        from_user_id: "",
        to_user_id: toUserId,
        client_id: lastClientId,
        message_type: MESSAGE_TYPE_BOT,
        message_state: MESSAGE_STATE_FINISH,
        item_list: [item],
        context_token: contextToken || undefined
      },
      base_info: buildBaseInfo()
    });
    await sendMessageRequest({
      baseUrl,
      token,
      body,
      timeoutMs
    });
  }

  return {
    messageId: lastClientId
  };
}

async function getUploadUrl({
  baseUrl,
  token,
  toUserId,
  fileKey,
  rawSize,
  rawMd5,
  encryptedSize,
  aesKeyHex,
  timeoutMs
}) {
  const body = JSON.stringify({
    filekey: fileKey,
    media_type: UPLOAD_MEDIA_TYPE_FILE,
    to_user_id: toUserId,
    rawsize: rawSize,
    rawfilemd5: rawMd5,
    filesize: encryptedSize,
    no_need_thumb: true,
    aeskey: aesKeyHex,
    base_info: buildBaseInfo()
  });
  return requestJson({
    method: "POST",
    url: `${trimBaseUrl(baseUrl)}/ilink/bot/getuploadurl`,
    headers: buildAuthHeaders(token, body),
    body,
    timeoutMs
  });
}

async function uploadEncryptedFileToCdn({
  cdnBaseUrl,
  uploadFullUrl,
  uploadParam,
  fileKey,
  encryptedBuffer
}) {
  if (!uploadFullUrl?.trim() && !uploadParam?.trim()) {
    throw new Error("Weixin CDN upload URL is missing.");
  }
  const target = uploadFullUrl?.trim()
    ? uploadFullUrl.trim()
    : buildCdnUploadUrl({
        cdnBaseUrl,
        uploadParam,
        fileKey
      });
  let lastError = null;

  for (let attempt = 1; attempt <= CDN_UPLOAD_MAX_RETRIES; attempt += 1) {
    try {
      const response = await fetch(target, {
        method: "POST",
        headers: {
          "Content-Type": "application/octet-stream"
        },
        body: new Uint8Array(encryptedBuffer)
      });
      if (response.status >= 400 && response.status < 500) {
        const errorText = response.headers.get("x-error-message") || (await response.text());
        throw new Error(`CDN upload client error ${response.status}: ${errorText}`);
      }
      if (response.status !== 200) {
        const errorText = response.headers.get("x-error-message") || `status ${response.status}`;
        throw new Error(`CDN upload server error: ${errorText}`);
      }
      const downloadParam = response.headers.get("x-encrypted-param");
      if (!downloadParam) {
        throw new Error("CDN upload response missing x-encrypted-param header.");
      }
      return downloadParam;
    } catch (error) {
      lastError = error;
      if (error instanceof Error && error.message.includes("client error")) {
        throw error;
      }
    }
  }

  throw lastError instanceof Error ? lastError : new Error("CDN upload failed.");
}

export function normalizeWeixinAccountId(accountId) {
  const normalized = String(accountId ?? "").trim();
  if (!normalized) {
    return "";
  }
  if (normalized.endsWith("@im.bot")) {
    return `${normalized.slice(0, -7)}-im-bot`;
  }
  if (normalized.endsWith("@im.wechat")) {
    return `${normalized.slice(0, -10)}-im-wechat`;
  }
  return normalized.replace(/[^a-zA-Z0-9_-]+/g, "-");
}

export function buildClawbotInstanceId(accountId) {
  const normalized = normalizeWeixinAccountId(accountId);
  return normalized ? `clawbridge:${normalized}` : "";
}

export async function fetchQrCode({ baseUrl, timeoutMs, botType = DEFAULT_BOT_TYPE }) {
  const target = `${trimBaseUrl(baseUrl)}/ilink/bot/get_bot_qrcode?bot_type=${encodeURIComponent(botType)}`;
  const payload = await requestJson({
    method: "GET",
    url: target,
    headers: {},
    timeoutMs
  });
  const qrcode = String(payload.qrcode ?? "").trim();
  const qrDataUrl = String(payload.qrcode_img_content ?? "").trim();
  if (!qrcode || !qrDataUrl) {
    throw new Error("Weixin iLink did not return a usable QR code.");
  }
  return {
    qrcode,
    qrDataUrl
  };
}

export async function fetchQrStatus({ baseUrl, qrcode, timeoutMs }) {
  const target = `${trimBaseUrl(baseUrl)}/ilink/bot/get_qrcode_status?qrcode=${encodeURIComponent(qrcode)}`;
  let payload;
  try {
    payload = await requestJson({
      method: "GET",
      url: target,
      headers: {},
      timeoutMs
    });
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") {
      return { status: "wait" };
    }
    return { status: "wait" };
  }
  return {
    status: String(payload.status ?? "wait").trim() || "wait",
    botToken: String(payload.bot_token ?? "").trim() || undefined,
    ilinkBotId: String(payload.ilink_bot_id ?? "").trim() || undefined,
    ilinkUserId: String(payload.ilink_user_id ?? "").trim() || undefined,
    baseUrl: String(payload.baseurl ?? "").trim() || undefined,
    redirectHost: String(payload.redirect_host ?? "").trim() || undefined
  };
}

export async function fetchUpdates({
  baseUrl,
  token,
  getUpdatesBuf,
  timeoutMs
}) {
  const body = JSON.stringify({
    get_updates_buf: getUpdatesBuf ?? "",
    base_info: buildBaseInfo()
  });
  try {
    const payload = await requestJson({
      method: "POST",
      url: `${trimBaseUrl(baseUrl)}/ilink/bot/getupdates`,
      headers: buildAuthHeaders(token, body),
      body,
      timeoutMs
    });
    return {
      ret: Number(payload.ret ?? 0),
      errcode: typeof payload.errcode === "number" ? payload.errcode : undefined,
      errmsg: typeof payload.errmsg === "string" ? payload.errmsg : undefined,
      msgs: Array.isArray(payload.msgs) ? payload.msgs : [],
      getUpdatesBuf: typeof payload.get_updates_buf === "string" ? payload.get_updates_buf : (getUpdatesBuf ?? ""),
      longPollingTimeoutMs:
        typeof payload.longpolling_timeout_ms === "number" ? payload.longpolling_timeout_ms : undefined
    };
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") {
      return {
        ret: 0,
        msgs: [],
        getUpdatesBuf: getUpdatesBuf ?? ""
      };
    }
    throw error;
  }
}

export async function fetchTypingTicket({
  baseUrl,
  token,
  toUserId,
  contextToken,
  timeoutMs
}) {
  const body = JSON.stringify({
    ilink_user_id: toUserId,
    context_token: contextToken || undefined,
    base_info: buildBaseInfo()
  });
  const payload = await requestJson({
    method: "POST",
    url: `${trimBaseUrl(baseUrl)}/ilink/bot/getconfig`,
    headers: buildAuthHeaders(token, body),
    body,
    timeoutMs
  });
  return String(payload.typing_ticket ?? "").trim();
}

export async function sendTypingStatus({
  baseUrl,
  token,
  toUserId,
  typingTicket,
  status = WeixinTypingStatus.TYPING,
  timeoutMs
}) {
  if (!String(toUserId ?? "").trim() || !String(typingTicket ?? "").trim()) {
    return;
  }
  const body = JSON.stringify({
    ilink_user_id: toUserId,
    typing_ticket: typingTicket,
    status,
    base_info: buildBaseInfo()
  });
  await requestJson({
    method: "POST",
    url: `${trimBaseUrl(baseUrl)}/ilink/bot/sendtyping`,
    headers: buildAuthHeaders(token, body),
    body,
    timeoutMs
  });
}

export function extractInboundText(message) {
  const items = Array.isArray(message?.item_list) ? message.item_list : [];
  const textChunks = [];
  for (const item of items) {
    if (item?.type === MESSAGE_ITEM_TYPE_TEXT && item?.text_item?.text != null) {
      textChunks.push(String(item.text_item.text));
      continue;
    }
    if (item?.type === MESSAGE_ITEM_TYPE_VOICE && item?.voice_item?.text != null) {
      textChunks.push(String(item.voice_item.text));
    }
  }
  return textChunks.join("\n").trim();
}

export function hasInboundVoiceItems(message) {
  const items = Array.isArray(message?.item_list) ? message.item_list : [];
  return items.some((item) => item?.type === MESSAGE_ITEM_TYPE_VOICE);
}

export function collectInboundImageUrls(message) {
  const items = Array.isArray(message?.item_list) ? message.item_list : [];
  const urls = [];
  for (const item of items) {
    if (item?.type !== MESSAGE_ITEM_TYPE_IMAGE) {
      continue;
    }
    const mediaUrl =
      String(item?.image_item?.media?.full_url ?? "").trim() ||
      String(item?.image_item?.url ?? "").trim();
    if (mediaUrl) {
      urls.push(mediaUrl);
    }
  }
  return urls;
}

export function collectInboundImages(message) {
  const items = Array.isArray(message?.item_list) ? message.item_list : [];
  const images = [];
  for (const item of items) {
    if (item?.type !== MESSAGE_ITEM_TYPE_IMAGE) {
      continue;
    }
    const media = item?.image_item?.media ?? {};
    const fullUrl = String(media.full_url ?? "").trim() || String(item?.image_item?.url ?? "").trim();
    const encryptQueryParam = String(media.encrypt_query_param ?? "").trim();
    if (!fullUrl && !encryptQueryParam) {
      continue;
    }
    images.push({
      fullUrl,
      encryptQueryParam,
      aesKeyHex: String(item?.image_item?.aeskey ?? "").trim(),
      aesKey: String(media.aes_key ?? "").trim()
    });
  }
  return images;
}

export async function fetchImageAsDataUrl(url, { timeoutMs, maxBytes }) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { signal: controller.signal });
    if (!response.ok) {
      throw new Error(`Image download failed with status ${response.status}.`);
    }
    const arrayBuffer = await response.arrayBuffer();
    const buffer = Buffer.from(arrayBuffer);
    if (buffer.byteLength > maxBytes) {
      throw new Error("Image download exceeded the configured size limit.");
    }
    const mimeType = String(response.headers.get("content-type") ?? "image/jpeg").trim() || "image/jpeg";
    return {
      mimeType,
      dataUrl: `data:${mimeType};base64,${buffer.toString("base64")}`
    };
  } finally {
    clearTimeout(timeout);
  }
}

export async function fetchInboundImageAsDataUrl(image, { timeoutMs, maxBytes, cdnBaseUrl }) {
  const fullUrl = String(image?.fullUrl ?? "").trim();
  const encryptQueryParam = String(image?.encryptQueryParam ?? "").trim();
  if (!fullUrl && !encryptQueryParam) {
    throw new Error("The inbound image is missing both full_url and encrypt_query_param.");
  }

  const key = getInboundImageKey(image);
  const target = fullUrl || buildCdnDownloadUrl({ cdnBaseUrl, encryptedQueryParam: encryptQueryParam });
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(target, { signal: controller.signal });
    if (!response.ok) {
      throw new Error(`Image download failed with status ${response.status}.`);
    }
    const arrayBuffer = await response.arrayBuffer();
    let buffer = Buffer.from(arrayBuffer);
    if (key) {
      buffer = decryptAesEcb(buffer, key);
    }
    if (buffer.byteLength > maxBytes) {
      throw new Error("Image download exceeded the configured size limit.");
    }
    const mimeType = resolveInboundImageMimeType(buffer, response.headers.get("content-type"));
    return {
      mimeType,
      dataUrl: `data:${mimeType};base64,${buffer.toString("base64")}`
    };
  } finally {
    clearTimeout(timeout);
  }
}

export async function sendTextMessage({
  baseUrl,
  token,
  toUserId,
  text,
  contextToken,
  timeoutMs
}) {
  return sendMessageItems({
    baseUrl,
    token,
    toUserId,
    text,
    contextToken,
    timeoutMs,
    items: []
  });
}

export async function sendFileMessage({
  baseUrl,
  token,
  toUserId,
  text,
  contextToken,
  fileName,
  fileBuffer,
  timeoutMs,
  cdnBaseUrl
}) {
  const rawMd5 = createHash("md5").update(fileBuffer).digest("hex");
  const fileKey = randomBytes(16).toString("hex");
  const aesKey = randomBytes(16);
  const aesKeyHex = aesKey.toString("hex");
  const encryptedBuffer = encryptAesEcb(fileBuffer, aesKey);
  const uploadPayload = await getUploadUrl({
    baseUrl,
    token,
    toUserId,
    fileKey,
    rawSize: fileBuffer.byteLength,
    rawMd5,
    encryptedSize: aesEcbPaddedSize(fileBuffer.byteLength),
    aesKeyHex,
    timeoutMs
  });
  const downloadParam = await uploadEncryptedFileToCdn({
    cdnBaseUrl,
    uploadFullUrl: String(uploadPayload.upload_full_url ?? "").trim() || undefined,
    uploadParam: String(uploadPayload.upload_param ?? "").trim() || undefined,
    fileKey,
    encryptedBuffer
  });

  return sendMessageItems({
    baseUrl,
    token,
    toUserId,
    text,
    contextToken,
    timeoutMs,
    items: [
      {
        type: MESSAGE_ITEM_TYPE_FILE,
        file_item: {
          media: {
            encrypt_query_param: downloadParam,
            // Match the working Weixin client contract for file attachments.
            aes_key: Buffer.from(aesKeyHex).toString("base64"),
            encrypt_type: 1
          },
          file_name: fileName,
          len: String(fileBuffer.byteLength)
        }
      }
    ]
  });
}

export function shouldProcessDirectMessage(message) {
  if (!message || typeof message !== "object") {
    return false;
  }
  if (message.group_id) {
    return false;
  }
  const messageType = typeof message.message_type === "number" ? message.message_type : MESSAGE_TYPE_USER;
  if (messageType !== MESSAGE_TYPE_USER) {
    return false;
  }
  return Boolean(String(message.from_user_id ?? "").trim());
}
