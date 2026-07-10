import {
  buildClawbotInstanceId,
  collectInboundImages,
  extractInboundText,
  fetchInboundImageAsDataUrl,
  fetchTypingTicket,
  hasInboundVoiceItems,
  fetchQrCode,
  fetchQrStatus,
  fetchUpdates,
  normalizeWeixinAccountId,
  sendFileMessage,
  sendTypingStatus,
  sendTextMessage,
  shouldProcessDirectMessage,
  WeixinTypingStatus
} from "./weixin-api.mjs";
import { sendInboundChatMessage } from "./backend-client.mjs";

const TYPING_KEEPALIVE_INTERVAL_MS = 5000;

function nowIso() {
  return new Date().toISOString();
}

function createDelay(ms, signal) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, ms);
    if (!signal) {
      return;
    }
    signal.addEventListener(
      "abort",
      () => {
        clearTimeout(timer);
        reject(new Error("aborted"));
      },
      { once: true }
    );
  });
}

function isQrSessionFresh(session) {
  return Boolean(session?.expiresAt && Date.parse(session.expiresAt) > Date.now());
}

function formatActionLines(actions) {
  const lines = [];
  for (const action of actions ?? []) {
    if (action?.type === "navigate" && action?.path) {
      lines.push(`网页入口：${action.path}`);
    } else if (action?.type === "link" && action?.url) {
      lines.push(action.url);
    }
  }
  return lines;
}

function buildWeixinBindIntroText() {
  return [
    "你好，我是 AlertClaw 的 ClawBot，一个在微信中的舆情助手。",
    "我主要的功能是根据你自定义的监控词，全网实时抓取相关热点新闻，并聚类为事件向你推送，还可以进一步生成专业的舆情报告。",
    "你可以直接向我下达指令，例如：",
    "查询今日热榜",
    "新增监控关键词“AI”",
    "查看当前所有监控列表",
    "生成舆情报告",
    "",
    "我还支持多模态输入，你可以向我发送语音与图片。"
  ].join("\n");
}

function formatBridgeActionLines(actions) {
  const lines = [];
  for (const action of actions ?? []) {
    if (action?.type === "navigate" && action?.path) {
      lines.push(`网页入口：${action.path}`);
    } else if (action?.type === "link" && action?.url) {
      lines.push(action.url);
    }
  }
  return lines;
}

function buildInboundDispatchFallbackText(code) {
  if (code === "auth_weixin_not_bound") {
    return "当前微信 ClawBot 尚未绑定 AlertClaw 账号，请先到网页账号页完成扫码绑定。";
  }
  return "这次没有把消息转发给 AlertClaw，请稍后再试。";
}

function buildInboundVoiceFallbackText() {
  return "我收到了这条语音，但暂时还没有拿到可用的转写文本。请换成文字再发一次，或者重新发送一条更清晰的语音。";
}

function formatBridgeActionLinesForOralSage(actions) {
  const lines = [];
  for (const action of actions ?? []) {
    if (action?.type === "navigate" && action?.path) {
      lines.push(`网页入口：${action.path}`);
    } else if (action?.type === "link" && action?.url) {
      lines.push(action.url);
    }
  }
  return lines;
}

function buildWeixinBindIntroTextForOralSage() {
  return [
    "你好，我是 OralSage 的微信 ClawBot，一个学习计划助手。",
    "我可以把每日学习计划推送到微信，也可以在微信里帮你完成打卡、修改计划和延期计划。",
    "你可以直接发送这些指令：",
    "今日计划",
    "推荐计划",
    "添加计划 复习牙周病基础",
    "打卡 1",
    "修改 1 复习牙髓炎鉴别诊断",
    "延期 1 到明天",
    "",
    "语音和图片会先进入消息通道；当前计划功能以文字指令为主。"
  ].join("\n");
}

function buildInboundDispatchFallbackTextForOralSage(code) {
  if (code === "auth_weixin_not_bound") {
    return "当前微信 ClawBot 尚未绑定 OralSage 账号，请先到网页的微信连接页完成扫码绑定。";
  }
  return "这次没有把消息转发给 OralSage，请稍后再试。";
}

function buildInboundVoiceFallbackTextForOralSage() {
  return "我收到了这条语音，但暂时还没有拿到可用的转写文本。请换成文字再发一次。";
}

function renderReplyText(reply) {
  const base = String(reply?.reply_text ?? "").trim();
  const actionLines = formatBridgeActionLinesForOralSage(reply?.reply_actions);
  if (!base && actionLines.length === 0) {
    return "";
  }
  if (!actionLines.length) {
    return base;
  }
  return [base, ...actionLines].filter(Boolean).join("\n\n");
}

function buildImageFileName(index, mimeType) {
  const extension = mimeType.includes("/") ? mimeType.split("/")[1] : "png";
  return `weixin-image-${Date.now()}-${index + 1}.${extension}`;
}

function normalizeReplyAttachments(reply) {
  return Array.isArray(reply?.reply_attachments) ? reply.reply_attachments : [];
}

function decodeReplyAttachment(attachment, maxBytes) {
  const fileName = String(attachment?.file_name ?? "").trim();
  const contentType = String(attachment?.content_type ?? "application/octet-stream").trim() || "application/octet-stream";
  const dataBase64 = String(attachment?.data_base64 ?? "").trim();
  if (!fileName || !dataBase64) {
    throw new Error("The reply attachment is missing file_name or data_base64.");
  }
  const fileBuffer = Buffer.from(dataBase64, "base64");
  if (!fileBuffer.byteLength) {
    throw new Error("The reply attachment decoded to an empty file.");
  }
  if (fileBuffer.byteLength > maxBytes) {
    throw new Error(`The reply attachment exceeded the size limit of ${maxBytes} bytes.`);
  }
  return {
    fileName,
    contentType,
    fileBuffer,
    reportJobId: String(attachment?.report_job_id ?? "").trim() || null
  };
}

export class ClawBridgeServiceError extends Error {
  constructor(message, statusCode = 400) {
    super(message);
    this.name = "ClawBridgeServiceError";
    this.statusCode = statusCode;
  }
}

export class ClawBridgeService {
  constructor({ config, store, getBootstrapState, log, transport = {} }) {
    this.config = config;
    this.store = store;
    this.getBootstrapState = getBootstrapState;
    this.log = log;
    this.workers = new Map();
    this.sendInboundChatMessage = transport.sendInboundChatMessage ?? sendInboundChatMessage;
    this.sendTextMessage = transport.sendTextMessage ?? sendTextMessage;
    this.sendFileMessage = transport.sendFileMessage ?? sendFileMessage;
    this.fetchInboundImageAsDataUrl = transport.fetchInboundImageAsDataUrl ?? fetchInboundImageAsDataUrl;
    this.fetchTypingTicket = transport.fetchTypingTicket ?? fetchTypingTicket;
    this.sendTypingStatus = transport.sendTypingStatus ?? sendTypingStatus;
  }

  async init() {
    const accounts = this.store.listAccounts();
    for (const account of accounts) {
      if (account?.botToken && account?.baseUrl && account?.enabled !== false) {
        this.startWorker(account.accountId);
      }
    }
  }

  getWorkerStats() {
    return {
      activeWorkers: this.workers.size,
      accountIds: Array.from(this.workers.keys())
    };
  }

  async startQrSession(sessionId) {
    const existing = this.store.getQrSession(sessionId);
    if (existing && isQrSessionFresh(existing) && existing.status === "waiting" && existing.qrDataUrl) {
      return {
        sessionId: existing.sessionId,
        qrDataUrl: existing.qrDataUrl,
        message: existing.message || "Scan the QR code with Weixin."
      };
    }
    const qr = await fetchQrCode({
      baseUrl: this.config.weixinPluginBaseUrl,
      timeoutMs: this.config.apiTimeoutMs
    });
    const session = {
      sessionId,
      qrcode: qr.qrcode,
      qrDataUrl: qr.qrDataUrl,
      status: "waiting",
      pollBaseUrl: this.config.weixinPluginBaseUrl,
      message: "Scan the QR code with Weixin to connect ClawBot.",
      createdAt: nowIso(),
      updatedAt: nowIso(),
      expiresAt: new Date(Date.now() + 5 * 60_000).toISOString(),
      refreshCount: 0
    };
    await this.store.setQrSession(session);
    return {
      sessionId,
      qrDataUrl: qr.qrDataUrl,
      message: session.message
    };
  }

  async getQrSessionStatus(sessionId, timeoutMs) {
    const session = this.store.getQrSession(sessionId);
    if (!session) {
      throw new Error("The requested QR session was not found.");
    }
    if (session.status === "bound") {
      return this._buildQrStatusResponse(session, true);
    }
    const effectiveTimeoutMs = Math.max(1000, Number(timeoutMs || this.config.qrStatusTimeoutMs));
    const status = await fetchQrStatus({
      baseUrl: session.pollBaseUrl || this.config.weixinPluginBaseUrl,
      qrcode: session.qrcode,
      timeoutMs: effectiveTimeoutMs
    });

    if (status.status === "scaned_but_redirect" && status.redirectHost) {
      const nextSession = {
        ...session,
        pollBaseUrl: `https://${status.redirectHost}`,
        updatedAt: nowIso(),
        status: "waiting",
        message: "QR code scanned, waiting for the Weixin confirmation."
      };
      await this.store.setQrSession(nextSession);
      return this._buildQrStatusResponse(nextSession, false);
    }

    if (status.status === "scaned") {
      const nextSession = {
        ...session,
        status: "waiting",
        updatedAt: nowIso(),
        message: "QR code scanned, waiting for the Weixin confirmation."
      };
      await this.store.setQrSession(nextSession);
      return this._buildQrStatusResponse(nextSession, false);
    }

    if (status.status === "expired") {
      const refreshCount = Number(session.refreshCount ?? 0);
      if (refreshCount >= 2) {
        const expiredSession = {
          ...session,
          status: "expired",
          updatedAt: nowIso(),
          message: "The QR code has expired. Please request a new one."
        };
        await this.store.setQrSession(expiredSession);
        return this._buildQrStatusResponse(expiredSession, false);
      }
      const refreshed = await fetchQrCode({
        baseUrl: this.config.weixinPluginBaseUrl,
        timeoutMs: this.config.apiTimeoutMs
      });
      const refreshedSession = {
        ...session,
        qrcode: refreshed.qrcode,
        qrDataUrl: refreshed.qrDataUrl,
        status: "waiting",
        updatedAt: nowIso(),
        expiresAt: new Date(Date.now() + 5 * 60_000).toISOString(),
        refreshCount: refreshCount + 1,
        message: "The QR code was refreshed. Scan it again with Weixin."
      };
      await this.store.setQrSession(refreshedSession);
      return this._buildQrStatusResponse(refreshedSession, false);
    }

    if (status.status === "confirmed") {
      const rawAccountId = String(status.ilinkBotId ?? "").trim();
      const rawUserId = String(status.ilinkUserId ?? "").trim();
      if (!rawAccountId || !rawUserId) {
        const errorSession = {
          ...session,
          status: "error",
          updatedAt: nowIso(),
          message: "The Weixin gateway returned an incomplete binding identity."
        };
        await this.store.setQrSession(errorSession);
        return this._buildQrStatusResponse(errorSession, false);
      }
      const accountId = normalizeWeixinAccountId(rawAccountId);
      const existingAccount = this.store.getAccount(accountId);
      const account = {
        accountId,
        weixinAccountId: rawAccountId,
        weixinUserId: rawUserId,
        clawbotInstanceId: buildClawbotInstanceId(rawAccountId),
        botToken: String(status.botToken ?? existingAccount?.botToken ?? "").trim(),
        baseUrl: String(status.baseUrl ?? session.pollBaseUrl ?? this.config.weixinPluginBaseUrl).trim(),
        enabled: true,
        boundAt: existingAccount?.boundAt || nowIso(),
        updatedAt: nowIso(),
        lastPolledAt: existingAccount?.lastPolledAt || null,
        lastInboundAt: existingAccount?.lastInboundAt || null,
        lastReplyAt: existingAccount?.lastReplyAt || null,
        welcomeSentAt: existingAccount?.welcomeSentAt || null,
        lastError: null,
        getUpdatesBuf: existingAccount?.getUpdatesBuf || ""
      };
      await this.store.setAccount(account);
      const boundSession = {
        ...session,
        status: "bound",
        updatedAt: nowIso(),
        message: "ClawBot connected to Weixin successfully.",
        weixinAccountId: rawAccountId,
        weixinUserId: rawUserId,
        clawbotInstanceId: account.clawbotInstanceId
      };
      await this.store.setQrSession(boundSession);
      this.startWorker(account.accountId);
      return this._buildQrStatusResponse(boundSession, true);
    }

    const waitingSession = {
      ...session,
      status: "waiting",
      updatedAt: nowIso(),
      message: "Waiting for the QR login to finish."
    };
    await this.store.setQrSession(waitingSession);
    return this._buildQrStatusResponse(waitingSession, false);
  }

  getAccountStatus(accountId) {
    const normalized = normalizeWeixinAccountId(accountId);
    const account = this.store.getAccount(normalized);
    if (!account) {
      return null;
    }
    return {
      accountId: account.accountId,
      weixinAccountId: account.weixinAccountId,
      weixinUserId: account.weixinUserId,
      clawbotInstanceId: account.clawbotInstanceId,
      enabled: account.enabled !== false,
      boundAt: account.boundAt || null,
      lastPolledAt: account.lastPolledAt || null,
      lastInboundAt: account.lastInboundAt || null,
      lastReplyAt: account.lastReplyAt || null,
      lastError: account.lastError || null,
      workerRunning: this.workers.has(normalized)
    };
  }

  async sendProactiveMessage({ text, clawbotInstanceId, weixinAccountId }) {
    const trimmedText = String(text ?? "").trim();
    if (!trimmedText) {
      throw new ClawBridgeServiceError("text is required.", 422);
    }

    const account = this._requireTargetAccount({ clawbotInstanceId, weixinAccountId });

    try {
      const sent = await this.sendTextMessage({
        baseUrl: account.baseUrl,
        token: account.botToken,
        toUserId: account.weixinUserId,
        text: trimmedText,
        timeoutMs: this.config.apiTimeoutMs
      });
      const sentAt = nowIso();
      await this.store.patchAccount(account.accountId, {
        lastReplyAt: sentAt,
        lastError: null,
        updatedAt: sentAt
      });
      this.log("proactive_message_sent", {
        accountId: account.accountId,
        clawbotInstanceId: account.clawbotInstanceId
      });
      return {
        status: "sent",
        accountId: account.accountId,
        weixinAccountId: account.weixinAccountId,
        clawbotInstanceId: account.clawbotInstanceId,
        messageId: String(sent?.messageId ?? "").trim() || null,
        sentAt
      };
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      await this.store.patchAccount(account.accountId, {
        lastError: message,
        updatedAt: nowIso()
      });
      throw new ClawBridgeServiceError(message || "The proactive message could not be sent.", 502);
    }
  }

  async sendProactiveFile({
    text,
    clawbotInstanceId,
    weixinAccountId,
    fileName,
    contentType,
    fileBuffer
  }) {
    const account = this._requireTargetAccount({ clawbotInstanceId, weixinAccountId });
    if (!fileName) {
      throw new ClawBridgeServiceError("file_name is required.", 422);
    }
    if (!fileBuffer?.byteLength) {
      throw new ClawBridgeServiceError("file data is required.", 422);
    }
    if (fileBuffer.byteLength > this.config.maxAttachmentBytes) {
      throw new ClawBridgeServiceError("The requested file exceeded the configured attachment size limit.", 413);
    }

    try {
      const sent = await this.sendFileMessage({
        baseUrl: account.baseUrl,
        token: account.botToken,
        toUserId: account.weixinUserId,
        text: String(text ?? "").trim(),
        fileName,
        contentType: String(contentType ?? "").trim() || "application/octet-stream",
        fileBuffer,
        timeoutMs: this.config.apiTimeoutMs,
        cdnBaseUrl: this.config.weixinPluginBaseUrl
      });
      const sentAt = nowIso();
      await this.store.patchAccount(account.accountId, {
        lastReplyAt: sentAt,
        lastError: null,
        updatedAt: sentAt
      });
      this.log("proactive_file_sent", {
        accountId: account.accountId,
        clawbotInstanceId: account.clawbotInstanceId,
        fileName
      });
      return {
        status: "sent",
        accountId: account.accountId,
        weixinAccountId: account.weixinAccountId,
        clawbotInstanceId: account.clawbotInstanceId,
        messageId: String(sent?.messageId ?? "").trim() || null,
        sentAt
      };
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      await this.store.patchAccount(account.accountId, {
        lastError: message,
        updatedAt: nowIso()
      });
      throw new ClawBridgeServiceError(message || "The proactive file could not be sent.", 502);
    }
  }

  startWorker(accountId) {
    if (this.workers.has(accountId)) {
      return;
    }
    const controller = new AbortController();
    const run = this._runWorker(accountId, controller.signal)
      .catch((error) => {
        this.log("worker_failed", {
          accountId,
          message: error instanceof Error ? error.message : String(error)
        });
      })
      .finally(() => {
        this.workers.delete(accountId);
      });
    this.workers.set(accountId, {
      controller,
      run
    });
    this.log("worker_started", { accountId });
  }

  async stopAllWorkers() {
    const workers = Array.from(this.workers.values());
    for (const worker of workers) {
      worker.controller.abort();
    }
    await Promise.allSettled(workers.map((worker) => worker.run));
  }

  async _runWorker(accountId, signal) {
    while (!signal.aborted) {
      const account = this.store.getAccount(accountId);
      if (!account?.botToken || !account?.baseUrl || account.enabled === false) {
        return;
      }
      try {
        const updates = await fetchUpdates({
          baseUrl: account.baseUrl,
          token: account.botToken,
          getUpdatesBuf: account.getUpdatesBuf || "",
          timeoutMs: this.config.longPollTimeoutMs
        });
        await this.store.patchAccount(accountId, {
          getUpdatesBuf: updates.getUpdatesBuf ?? account.getUpdatesBuf ?? "",
          lastPolledAt: nowIso(),
          updatedAt: nowIso(),
          lastError: null
        });
        if (updates.ret && updates.ret !== 0) {
          await this.store.patchAccount(accountId, {
            lastError: updates.errmsg || `Weixin getUpdates failed with ret=${updates.ret}.`,
            updatedAt: nowIso()
          });
          await createDelay(2000, signal);
          continue;
        }
        for (const message of updates.msgs ?? []) {
          await this._handleInboundMessage(accountId, message);
        }
      } catch (error) {
        if (signal.aborted) {
          return;
        }
        await this.store.patchAccount(accountId, {
          lastError: error instanceof Error ? error.message : String(error),
          updatedAt: nowIso()
        });
        await createDelay(2000, signal);
      }
    }
  }

  async _handleInboundMessage(accountId, message) {
    if (!shouldProcessDirectMessage(message)) {
      return;
    }
    const account = this.store.getAccount(accountId);
    if (!account) {
      return;
    }
    const toUserId = String(message.from_user_id ?? "").trim() || account.weixinUserId;
    const contextToken = String(message.context_token ?? "").trim();
    await this._sendBindIntroIfNeeded({
      accountId,
      account,
      toUserId,
      contextToken
    });
    const text = extractInboundText(message);
    const attachments = [];
    const inboundImages = collectInboundImages(message).slice(0, 3);
    for (const [index, imageRef] of inboundImages.entries()) {
      try {
        const image = await this.fetchInboundImageAsDataUrl(imageRef, {
          timeoutMs: this.config.apiTimeoutMs,
          maxBytes: this.config.maxImageBytes,
          cdnBaseUrl: this.config.weixinPluginBaseUrl
        });
        attachments.push({
          type: "image",
          mime_type: image.mimeType,
          data_url: image.dataUrl,
          file_name: buildImageFileName(index, image.mimeType)
        });
      } catch (error) {
        this.log("image_fetch_skipped", {
          accountId,
          message: error instanceof Error ? error.message : String(error)
        });
      }
    }
    if (!text && attachments.length === 0 && hasInboundVoiceItems(message)) {
      await this.sendTextMessage({
        baseUrl: account.baseUrl,
        token: account.botToken,
        toUserId,
        text: buildInboundVoiceFallbackTextForOralSage(),
        contextToken,
        timeoutMs: this.config.apiTimeoutMs
      });
      const fallbackAt = nowIso();
      await this.store.patchAccount(accountId, {
        lastReplyAt: fallbackAt,
        lastError: null,
        updatedAt: fallbackAt
      });
      return;
    }
    if (!text && attachments.length === 0) {
      return;
    }
    const inboundAt = nowIso();
    await this.store.patchAccount(accountId, {
      lastInboundAt: inboundAt,
      updatedAt: inboundAt
    });
    const typingController = await this._startTypingIndicator({
      accountId,
      account,
      toUserId,
      contextToken
    });
    try {
      let reply;
      try {
        reply = await this.sendInboundChatMessage({
          config: this.config,
          bootstrap: this.getBootstrapState(),
          message: {
            clawbot_instance_id: account.clawbotInstanceId,
            weixin_account_id: account.weixinAccountId,
            weixin_user_id: toUserId,
            text,
            attachments
          }
        });
      } catch (error) {
        const code = String(error?.code ?? "");
        const fallbackReplyText = buildInboundDispatchFallbackTextForOralSage(code);
        await this.sendTextMessage({
          baseUrl: account.baseUrl,
          token: account.botToken,
          toUserId,
          text: fallbackReplyText,
          contextToken,
          timeoutMs: this.config.apiTimeoutMs
        });
        await this.store.patchAccount(accountId, {
          lastReplyAt: nowIso(),
          lastError: error instanceof Error ? error.message : String(error),
          updatedAt: nowIso()
        });
        return;
      }

      const replyText = renderReplyText(reply);
      const replyAttachments = normalizeReplyAttachments(reply);
      if (!replyText && replyAttachments.length === 0) {
        return;
      }
      if (replyAttachments.length > 0) {
        if (replyText) {
          await this.sendTextMessage({
            baseUrl: account.baseUrl,
            token: account.botToken,
            toUserId,
            text: replyText,
            contextToken,
            timeoutMs: this.config.apiTimeoutMs
          });
        }
        for (const [index, attachment] of replyAttachments.entries()) {
          const decoded = decodeReplyAttachment(attachment, this.config.maxAttachmentBytes);
          await this.sendFileMessage({
            baseUrl: account.baseUrl,
            token: account.botToken,
            toUserId,
            text: "",
            contextToken: contextToken || undefined,
            fileName: decoded.fileName,
            contentType: decoded.contentType,
            fileBuffer: decoded.fileBuffer,
            timeoutMs: this.config.apiTimeoutMs,
            cdnBaseUrl: this.config.weixinPluginBaseUrl
          });
        }
      } else {
        await this.sendTextMessage({
          baseUrl: account.baseUrl,
          token: account.botToken,
          toUserId,
          text: replyText,
          contextToken,
          timeoutMs: this.config.apiTimeoutMs
        });
      }
      await this.store.patchAccount(accountId, {
        lastReplyAt: nowIso(),
        lastError: null,
        updatedAt: nowIso()
      });
    } finally {
      await typingController.stop();
    }
  }

  async _sendBindIntroIfNeeded({ accountId, account, toUserId, contextToken }) {
    if (!account?.boundAt || account?.welcomeSentAt || !account.botToken || !String(toUserId ?? "").trim()) {
      return;
    }
    try {
      await this.sendTextMessage({
        baseUrl: account.baseUrl,
        token: account.botToken,
        toUserId,
        text: buildWeixinBindIntroTextForOralSage(),
        contextToken,
        timeoutMs: this.config.apiTimeoutMs
      });
      const sentAt = nowIso();
      await this.store.patchAccount(accountId, {
        welcomeSentAt: sentAt,
        lastReplyAt: sentAt,
        lastError: null,
        updatedAt: sentAt
      });
    } catch (error) {
      this.log("bind_intro_failed", {
        accountId,
        message: error instanceof Error ? error.message : String(error)
      });
    }
  }

  async _startTypingIndicator({ accountId, account, toUserId, contextToken }) {
    if (!account?.botToken || !account?.baseUrl || !String(toUserId ?? "").trim()) {
      return { stop: async () => {} };
    }

    let typingTicket = "";
    try {
      typingTicket = await this.fetchTypingTicket({
        baseUrl: account.baseUrl,
        token: account.botToken,
        toUserId,
        contextToken,
        timeoutMs: this.config.apiTimeoutMs
      });
    } catch (error) {
      this.log("typing_ticket_failed", {
        accountId,
        message: error instanceof Error ? error.message : String(error)
      });
      return { stop: async () => {} };
    }

    if (!typingTicket) {
      return { stop: async () => {} };
    }

    let stopped = false;
    let sendChain = Promise.resolve();
    const sendStatus = (status, logEvent) => {
      sendChain = sendChain
        .catch(() => {})
        .then(async () => {
          await this.sendTypingStatus({
            baseUrl: account.baseUrl,
            token: account.botToken,
            toUserId,
            typingTicket,
            status,
            timeoutMs: this.config.apiTimeoutMs
          });
        })
        .catch((error) => {
          this.log(logEvent, {
            accountId,
            message: error instanceof Error ? error.message : String(error)
          });
        });
      return sendChain;
    };

    await sendStatus(WeixinTypingStatus.TYPING, "typing_status_failed");
    const timer = setInterval(() => {
      void sendStatus(WeixinTypingStatus.TYPING, "typing_keepalive_failed");
    }, TYPING_KEEPALIVE_INTERVAL_MS);
    timer.unref?.();

    return {
      stop: async () => {
        if (stopped) {
          return;
        }
        stopped = true;
        clearInterval(timer);
        await sendStatus(WeixinTypingStatus.CANCEL, "typing_cancel_failed");
      }
    };
  }

  _buildQrStatusResponse(session, connected) {
    return {
      connected,
      status: session.status || (connected ? "bound" : "waiting"),
      qrDataUrl: session.qrDataUrl || null,
      message: session.message || (connected ? "ClawBot connected successfully." : "Waiting for the QR login to finish."),
      weixinAccountId: session.weixinAccountId || null,
      weixinUserId: session.weixinUserId || null,
      clawbotInstanceId: session.clawbotInstanceId || null
    };
  }

  _resolveTargetAccount({ clawbotInstanceId, weixinAccountId }) {
    const normalizedInstanceId = String(clawbotInstanceId ?? "").trim();
    if (normalizedInstanceId) {
      const account = this.store
        .listAccounts()
        .find((candidate) => String(candidate?.clawbotInstanceId ?? "").trim() === normalizedInstanceId);
      if (account) {
        return account;
      }
    }

    const normalizedAccountId = normalizeWeixinAccountId(weixinAccountId);
    if (!normalizedAccountId) {
      return null;
    }
    return this.store.getAccount(normalizedAccountId);
  }

  _requireTargetAccount({ clawbotInstanceId, weixinAccountId }) {
    const account = this._resolveTargetAccount({ clawbotInstanceId, weixinAccountId });
    if (!account) {
      throw new ClawBridgeServiceError("The requested Weixin account was not found.", 404);
    }
    if (account.enabled === false) {
      throw new ClawBridgeServiceError("The requested Weixin account is currently disabled.", 409);
    }
    if (!account.botToken || !account.baseUrl) {
      throw new ClawBridgeServiceError("The requested Weixin account is not ready for message delivery.", 409);
    }
    if (!account.weixinUserId) {
      throw new ClawBridgeServiceError("The requested Weixin account does not have a bound user identity.", 409);
    }
    return account;
  }
}
