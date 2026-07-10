import test from "node:test";
import assert from "node:assert/strict";

import { ClawBridgeService, ClawBridgeServiceError } from "../src/bridge-service.mjs";

function createStore(accounts = []) {
  const accountMap = new Map(accounts.map((account) => [account.accountId, { ...account }]));
  return {
    listAccounts() {
      return Array.from(accountMap.values()).map((account) => ({ ...account }));
    },
    getAccount(accountId) {
      const account = accountMap.get(accountId);
      return account ? { ...account } : null;
    },
    async patchAccount(accountId, updates) {
      const existing = accountMap.get(accountId);
      if (!existing) {
        return null;
      }
      const next = {
        ...existing,
        ...updates
      };
      accountMap.set(accountId, next);
      return { ...next };
    }
  };
}

function createService({
  store,
  sendTextMessageImpl,
  sendFileMessageImpl,
  sendInboundChatMessageImpl,
  fetchInboundImageAsDataUrlImpl,
  fetchTypingTicketImpl,
  sendTypingStatusImpl
}) {
  return new ClawBridgeService({
    config: {
      apiTimeoutMs: 15000,
      maxImageBytes: 5_000_000,
      maxAttachmentBytes: 15_000_000,
      weixinPluginBaseUrl: "https://ilinkai.weixin.qq.com"
    },
    store,
    getBootstrapState: () => ({ ok: true }),
    log: () => {},
    transport: {
      sendTextMessage: sendTextMessageImpl,
      sendFileMessage: sendFileMessageImpl,
      sendInboundChatMessage: sendInboundChatMessageImpl ?? (async () => ({ status: "ok" })),
      fetchInboundImageAsDataUrl: fetchInboundImageAsDataUrlImpl,
      fetchTypingTicket: fetchTypingTicketImpl,
      sendTypingStatus: sendTypingStatusImpl
    }
  });
}

test("sendProactiveMessage sends a text reply to the bound Weixin user", async () => {
  const calls = [];
  const store = createStore([
    {
      accountId: "bot-1-im-bot",
      weixinAccountId: "bot-1@im.bot",
      weixinUserId: "wx-user-1",
      clawbotInstanceId: "clawbridge:bot-1-im-bot",
      botToken: "token-1",
      baseUrl: "https://ilinkai.weixin.qq.com",
      enabled: true
    }
  ]);
  const service = createService({
    store,
    sendTextMessageImpl: async (payload) => {
      calls.push(payload);
      return {
        messageId: "msg-1"
      };
    }
  });

  const result = await service.sendProactiveMessage({
    text: "预警提醒",
    clawbotInstanceId: "clawbridge:bot-1-im-bot"
  });

  assert.equal(result.status, "sent");
  assert.equal(result.messageId, "msg-1");
  assert.equal(result.weixinAccountId, "bot-1@im.bot");
  assert.equal(calls[0].toUserId, "wx-user-1");
  assert.equal(calls[0].text, "预警提醒");
  assert.equal(store.getAccount("bot-1-im-bot").lastError, null);
  assert.ok(store.getAccount("bot-1-im-bot").lastReplyAt);
});

test("sendProactiveMessage can resolve the target account by Weixin account id", async () => {
  const store = createStore([
    {
      accountId: "bot-2-im-bot",
      weixinAccountId: "bot-2@im.bot",
      weixinUserId: "wx-user-2",
      clawbotInstanceId: "clawbridge:bot-2-im-bot",
      botToken: "token-2",
      baseUrl: "https://ilinkai.weixin.qq.com",
      enabled: true
    }
  ]);
  const service = createService({
    store,
    sendTextMessageImpl: async () => ({ messageId: "msg-2" })
  });

  const result = await service.sendProactiveMessage({
    text: "新的告警到了",
    weixinAccountId: "bot-2@im.bot"
  });

  assert.equal(result.clawbotInstanceId, "clawbridge:bot-2-im-bot");
});

test("sendProactiveFile sends a pdf to the bound Weixin user", async () => {
  const calls = [];
  const store = createStore([
    {
      accountId: "bot-3-im-bot",
      weixinAccountId: "bot-3@im.bot",
      weixinUserId: "wx-user-3",
      clawbotInstanceId: "clawbridge:bot-3-im-bot",
      botToken: "token-3",
      baseUrl: "https://ilinkai.weixin.qq.com",
      enabled: true
    }
  ]);
  const service = createService({
    store,
    sendTextMessageImpl: async () => ({ messageId: "unused" }),
    sendFileMessageImpl: async (payload) => {
      calls.push(payload);
      return { messageId: "file-1" };
    }
  });

  const result = await service.sendProactiveFile({
    text: "这是最新报告，请查收。",
    clawbotInstanceId: "clawbridge:bot-3-im-bot",
    fileName: "人工智能报告.pdf",
    contentType: "application/pdf",
    fileBuffer: Buffer.from("%PDF-1.4\nreport\n", "utf-8")
  });

  assert.equal(result.status, "sent");
  assert.equal(result.messageId, "file-1");
  assert.equal(calls[0].toUserId, "wx-user-3");
  assert.equal(calls[0].fileName, "人工智能报告.pdf");
  assert.equal(calls[0].contentType, "application/pdf");
  assert.equal(calls[0].text, "这是最新报告，请查收。");
  assert.ok(Buffer.isBuffer(calls[0].fileBuffer));
});

test("sendProactiveMessage rejects missing accounts with a 404 error", async () => {
  const service = createService({
    store: createStore(),
    sendTextMessageImpl: async () => ({ messageId: "msg-x" })
  });

  await assert.rejects(
    () =>
      service.sendProactiveMessage({
        text: "预警提醒",
        clawbotInstanceId: "clawbridge:missing"
      }),
    (error) =>
      error instanceof ClawBridgeServiceError &&
      error.statusCode === 404 &&
      error.message === "The requested Weixin account was not found."
  );
});

test("inbound messages send the final reply in the original request context", async () => {
  const textCalls = [];
  const store = createStore([
    {
      accountId: "bot-4-im-bot",
      weixinAccountId: "bot-4@im.bot",
      weixinUserId: "wx-user-4",
      clawbotInstanceId: "clawbridge:bot-4-im-bot",
      botToken: "token-4",
      baseUrl: "https://ilinkai.weixin.qq.com",
      enabled: true
    }
  ]);
  const service = createService({
    store,
    sendTextMessageImpl: async (payload) => {
      textCalls.push(payload);
      return { messageId: `msg-${textCalls.length}` };
    },
    sendInboundChatMessageImpl: async () => ({
      reply_text: "处理完成",
      reply_attachments: []
    })
  });

  await service._handleInboundMessage("bot-4-im-bot", {
    from_user_id: "wx-user-4",
    context_token: "ctx-1",
    item_list: [
      {
        type: 1,
        text_item: {
          text: "你好"
        }
      }
    ]
  });

  assert.equal(textCalls.length, 1);
  assert.equal(textCalls[0].text, "处理完成");
  assert.equal(textCalls[0].contextToken, "ctx-1");
  assert.equal(textCalls[0].toUserId, "wx-user-4");
});

test("the first inbound message after binding sends the Weixin intro before the normal reply", async () => {
  const textCalls = [];
  const store = createStore([
    {
      accountId: "bot-welcome-im-bot",
      weixinAccountId: "bot-welcome@im.bot",
      weixinUserId: "wx-user-welcome",
      clawbotInstanceId: "clawbridge:bot-welcome-im-bot",
      botToken: "token-welcome",
      baseUrl: "https://ilinkai.weixin.qq.com",
      enabled: true,
      boundAt: "2026-04-11T01:00:00.000Z",
      welcomeSentAt: null
    }
  ]);
  const service = createService({
    store,
    fetchTypingTicketImpl: async () => "",
    sendTextMessageImpl: async (payload) => {
      textCalls.push(payload);
      return { messageId: `msg-${textCalls.length}` };
    },
    sendInboundChatMessageImpl: async ({ message }) => {
      assert.equal(message.text, "first question");
      return {
        reply_text: "normal reply",
        reply_attachments: []
      };
    }
  });

  await service._handleInboundMessage("bot-welcome-im-bot", {
    from_user_id: "wx-user-welcome",
    context_token: "ctx-welcome",
    item_list: [
      {
        type: 1,
        text_item: {
          text: "first question"
        }
      }
    ]
  });

  assert.equal(textCalls.length, 2);
  assert.equal(
    textCalls[0].text,
    [
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
    ].join("\n")
  );
  assert.equal(textCalls[0].contextToken, "ctx-welcome");
  assert.equal(textCalls[1].text, "normal reply");
  assert.ok(store.getAccount("bot-welcome-im-bot").welcomeSentAt);
  return;
  assert.equal(
    textCalls[0].text,
    [
      "你好，我是 AlertClaw 的 ClawBot，一个在微信中的舆情助手。",
      "我主要的功能是根据你自定义的监控词，全网实时抓取相关热点新闻，并聚类为事件向你推送，还可以进一步生成专业的舆情报告。",
      "你可以直接向我下达指令，例如：",
      "查询今日热榜",
      "新增监控关键词“AI”",
      "查看当前所有监控列表",
      "生成舆情报告",
      "",
      "我还支持多模态输入，你可以向我发送语音与图片。"
    ].join("\n")
  );
  assert.equal(textCalls[0].contextToken, "ctx-welcome");
  assert.equal(textCalls[1].text, "normal reply");
  assert.ok(store.getAccount("bot-welcome-im-bot").welcomeSentAt);
});

test("inbound messages decrypt image attachments before dispatching them to the backend", async () => {
  const imageCalls = [];
  const store = createStore([
    {
      accountId: "bot-image-im-bot",
      weixinAccountId: "bot-image@im.bot",
      weixinUserId: "wx-user-image",
      clawbotInstanceId: "clawbridge:bot-image-im-bot",
      botToken: "token-image",
      baseUrl: "https://ilinkai.weixin.qq.com",
      enabled: true
    }
  ]);
  const service = createService({
    store,
    fetchInboundImageAsDataUrlImpl: async (imageRef, options) => {
      imageCalls.push({ imageRef, options });
      return {
        mimeType: "image/png",
        dataUrl: "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO7Z5T8AAAAASUVORK5CYII="
      };
    },
    sendTextMessageImpl: async () => ({ messageId: "msg-image" }),
    sendInboundChatMessageImpl: async ({ message }) => {
      assert.equal(message.text, "please analyze this");
      assert.deepEqual(message.attachments, [
        {
          type: "image",
          mime_type: "image/png",
          data_url: "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO7Z5T8AAAAASUVORK5CYII=",
          file_name: "weixin-image-1-1.png"
        }
      ]);
      return {
        reply_text: "done",
        reply_attachments: []
      };
    }
  });

  const originalDateNow = Date.now;
  Date.now = () => 1;
  try {
    await service._handleInboundMessage("bot-image-im-bot", {
      from_user_id: "wx-user-image",
      context_token: "ctx-image",
      item_list: [
        {
          type: 1,
          text_item: {
            text: "please analyze this"
          }
        },
        {
          type: 2,
          image_item: {
            aeskey: "00112233445566778899aabbccddeeff",
            media: {
              full_url: "https://cdn.example/image.bin",
              encrypt_query_param: "download-param"
            }
          }
        }
      ]
    });
  } finally {
    Date.now = originalDateNow;
  }

  assert.equal(imageCalls.length, 1);
  assert.equal(imageCalls[0].imageRef.fullUrl, "https://cdn.example/image.bin");
  assert.equal(imageCalls[0].imageRef.encryptQueryParam, "download-param");
  assert.equal(imageCalls[0].imageRef.aesKeyHex, "00112233445566778899aabbccddeeff");
  assert.equal(imageCalls[0].options.cdnBaseUrl, "https://ilinkai.weixin.qq.com");
});

test("inbound voice messages use the Weixin voice transcription as the user text", async () => {
  const textCalls = [];
  const store = createStore([
    {
      accountId: "bot-voice-im-bot",
      weixinAccountId: "bot-voice@im.bot",
      weixinUserId: "wx-user-voice",
      clawbotInstanceId: "clawbridge:bot-voice-im-bot",
      botToken: "token-voice",
      baseUrl: "https://ilinkai.weixin.qq.com",
      enabled: true
    }
  ]);
  const service = createService({
    store,
    fetchTypingTicketImpl: async () => "",
    sendTextMessageImpl: async (payload) => {
      textCalls.push(payload);
      return { messageId: "msg-voice" };
    },
    sendInboundChatMessageImpl: async ({ message }) => {
      assert.equal(message.text, "voice transcript from weixin");
      assert.deepEqual(message.attachments, []);
      return {
        reply_text: "voice handled",
        reply_attachments: []
      };
    }
  });

  await service._handleInboundMessage("bot-voice-im-bot", {
    from_user_id: "wx-user-voice",
    context_token: "ctx-voice",
    item_list: [
      {
        type: 3,
        voice_item: {
          text: "voice transcript from weixin"
        }
      }
    ]
  });

  assert.equal(textCalls.length, 1);
  assert.equal(textCalls[0].text, "voice handled");
});

test("voice messages without a usable transcription return an explicit fallback", async () => {
  const textCalls = [];
  const store = createStore([
    {
      accountId: "bot-voice-fallback-im-bot",
      weixinAccountId: "bot-voice-fallback@im.bot",
      weixinUserId: "wx-user-voice-fallback",
      clawbotInstanceId: "clawbridge:bot-voice-fallback-im-bot",
      botToken: "token-voice-fallback",
      baseUrl: "https://ilinkai.weixin.qq.com",
      enabled: true
    }
  ]);
  const service = createService({
    store,
    sendTextMessageImpl: async (payload) => {
      textCalls.push(payload);
      return { messageId: "msg-voice-fallback" };
    },
    sendInboundChatMessageImpl: async () => {
      throw new Error("sendInboundChatMessage should not run without voice transcription");
    }
  });

  await service._handleInboundMessage("bot-voice-fallback-im-bot", {
    from_user_id: "wx-user-voice-fallback",
    context_token: "ctx-voice-fallback",
    item_list: [
      {
        type: 3,
        voice_item: {}
      }
    ]
  });

  assert.equal(textCalls.length, 1);
  assert.equal(
    textCalls[0].text,
    "我收到了这条语音，但暂时还没有拿到可用的转写文本。请换成文字再发一次。"
  );
  return;
  assert.equal(
    textCalls[0].text,
    "我收到了这条语音，但暂时还没有拿到可用的转写文本。请换成文字再发一次，或者重新发送一条更清晰的语音。"
  );
});

test("inbound messages use the official typing indicator while waiting for a reply", async () => {
  const typingCalls = [];
  const textCalls = [];
  const store = createStore([
    {
      accountId: "bot-typing-im-bot",
      weixinAccountId: "bot-typing@im.bot",
      weixinUserId: "wx-user-typing",
      clawbotInstanceId: "clawbridge:bot-typing-im-bot",
      botToken: "token-typing",
      baseUrl: "https://ilinkai.weixin.qq.com",
      enabled: true
    }
  ]);
  const service = createService({
    store,
    fetchTypingTicketImpl: async (payload) => {
      typingCalls.push({ stage: "ticket", ...payload });
      return "typing-ticket-1";
    },
    sendTypingStatusImpl: async (payload) => {
      typingCalls.push({ stage: "status", ...payload });
    },
    sendTextMessageImpl: async (payload) => {
      textCalls.push(payload);
      return { messageId: "msg-typing" };
    },
    sendInboundChatMessageImpl: async () => {
      assert.deepEqual(
        typingCalls.map((entry) => [entry.stage, entry.status ?? null]),
        [
          ["ticket", null],
          ["status", 1]
        ]
      );
      return {
        reply_text: "正在为你整理结果",
        reply_attachments: []
      };
    }
  });

  await service._handleInboundMessage("bot-typing-im-bot", {
    from_user_id: "wx-user-typing",
    context_token: "ctx-typing",
    item_list: [
      {
        type: 1,
        text_item: {
          text: "帮我看看最新情况"
        }
      }
    ]
  });

  assert.equal(textCalls.length, 1);
  assert.equal(textCalls[0].toUserId, "wx-user-typing");
  assert.equal(textCalls[0].contextToken, "ctx-typing");
  assert.equal(typingCalls[0].toUserId, "wx-user-typing");
  assert.equal(typingCalls[0].contextToken, "ctx-typing");
  assert.equal(typingCalls[1].typingTicket, "typing-ticket-1");
  assert.equal(typingCalls[2].typingTicket, "typing-ticket-1");
  assert.deepEqual(
    typingCalls
      .filter((entry) => entry.stage === "status")
      .map((entry) => entry.status),
    [1, 2]
  );
});

test("inbound messages send PDF attachments in the original request context", async () => {
  const textCalls = [];
  const fileCalls = [];
  const store = createStore([
    {
      accountId: "bot-5-im-bot",
      weixinAccountId: "bot-5@im.bot",
      weixinUserId: "wx-user-5",
      clawbotInstanceId: "clawbridge:bot-5-im-bot",
      botToken: "token-5",
      baseUrl: "https://ilinkai.weixin.qq.com",
      enabled: true
    }
  ]);
  const service = createService({
    store,
    sendTextMessageImpl: async (payload) => {
      textCalls.push(payload);
      return { messageId: "msg-1" };
    },
    sendFileMessageImpl: async (payload) => {
      fileCalls.push(payload);
      return { messageId: "file-1" };
    },
    sendInboundChatMessageImpl: async () => ({
      reply_text: "报告已经生成完成，请直接下载 PDF。",
      reply_attachments: [
        {
          file_name: "舆情报告.pdf",
          content_type: "application/pdf",
          data_base64: Buffer.from("%PDF-1.4\nreport\n", "utf-8").toString("base64")
        }
      ]
    })
  });

  await service._handleInboundMessage("bot-5-im-bot", {
    from_user_id: "wx-user-5",
    context_token: "ctx-5",
    item_list: [
      {
        type: 1,
        text_item: {
          text: "生成报告"
        }
      }
    ]
  });

  assert.equal(textCalls.length, 1);
  assert.equal(textCalls[0].contextToken, "ctx-5");
  assert.equal(textCalls[0].text, "报告已经生成完成，请直接下载 PDF。");
  assert.equal(fileCalls.length, 1);
  assert.equal(fileCalls[0].toUserId, "wx-user-5");
  assert.equal(fileCalls[0].fileName, "舆情报告.pdf");
  assert.equal(fileCalls[0].text, "");
  assert.equal(fileCalls[0].contextToken, "ctx-5");
});
