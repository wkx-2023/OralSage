import test from "node:test";
import assert from "node:assert/strict";
import { createCipheriv } from "node:crypto";

import {
  buildClawbotInstanceId,
  collectInboundImages,
  extractInboundText,
  fetchInboundImageAsDataUrl,
  fetchTypingTicket,
  normalizeWeixinAccountId,
  sendFileMessage,
  sendTypingStatus,
  WeixinTypingStatus,
  shouldProcessDirectMessage
} from "../src/weixin-api.mjs";

test("normalizeWeixinAccountId keeps Tencent bot ids stable", () => {
  assert.equal(normalizeWeixinAccountId("a330f5e0e3de@im.bot"), "a330f5e0e3de-im-bot");
  assert.equal(buildClawbotInstanceId("a330f5e0e3de@im.bot"), "clawbridge:a330f5e0e3de-im-bot");
});

test("extractInboundText joins multiple text items", () => {
  const text = extractInboundText({
    item_list: [
      { type: 1, text_item: { text: "第一句" } },
      { type: 2 },
      { type: 1, text_item: { text: "第二句" } }
    ]
  });

  assert.equal(text, "第一句\n第二句");
});

test("extractInboundText includes voice transcription when Weixin provides it", () => {
  const text = extractInboundText({
    item_list: [
      { type: 3, voice_item: { text: "voice transcript" } },
      { type: 1, text_item: { text: "typed text" } }
    ]
  });

  assert.equal(text, "voice transcript\ntyped text");
});

test("shouldProcessDirectMessage skips groups and bot echoes", () => {
  assert.equal(
    shouldProcessDirectMessage({
      from_user_id: "wx-user-1",
      message_type: 1
    }),
    true
  );
  assert.equal(
    shouldProcessDirectMessage({
      from_user_id: "wx-user-1",
      group_id: "group-1",
      message_type: 1
    }),
    false
  );
  assert.equal(
    shouldProcessDirectMessage({
      from_user_id: "wx-user-1",
      message_type: 2
    }),
    false
  );
});

test("fetchInboundImageAsDataUrl decrypts Weixin CDN images into valid image data URLs", async () => {
  const originalFetch = globalThis.fetch;
  const pngBuffer = Buffer.from(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO7Z5T8AAAAASUVORK5CYII=",
    "base64"
  );
  const aesKey = Buffer.from("00112233445566778899aabbccddeeff", "hex");
  const cipher = createCipheriv("aes-128-ecb", aesKey, null);
  const encryptedBuffer = Buffer.concat([cipher.update(pngBuffer), cipher.final()]);
  const inboundImages = collectInboundImages({
    item_list: [
      {
        type: 2,
        image_item: {
          aeskey: aesKey.toString("hex"),
          media: {
            full_url: "https://cdn.example/image.bin",
            encrypt_query_param: "download-param"
          }
        }
      }
    ]
  });

  globalThis.fetch = async (url) => {
    assert.equal(String(url), "https://cdn.example/image.bin");
    return new Response(encryptedBuffer, {
      status: 200,
      headers: { "Content-Type": "application/octet-stream" }
    });
  };

  try {
    const image = await fetchInboundImageAsDataUrl(inboundImages[0], {
      timeoutMs: 15000,
      maxBytes: 5_000_000,
      cdnBaseUrl: "https://ilinkai.weixin.qq.com"
    });
    assert.equal(image.mimeType, "image/png");
    assert.equal(image.dataUrl, `data:image/png;base64,${pngBuffer.toString("base64")}`);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("sendFileMessage encodes aes_key from the upload hex key for file downloads", async () => {
  const originalFetch = globalThis.fetch;
  const fetchCalls = [];
  const downloadParam = "download-param-123";

  globalThis.fetch = async (url, options = {}) => {
    fetchCalls.push({
      url: String(url),
      method: options.method ?? "GET",
      headers: options.headers ?? {},
      body: options.body
    });

    if (fetchCalls.length === 1) {
      return new Response(
        JSON.stringify({
          upload_param: "upload-param-123"
        }),
        {
          status: 200,
          headers: { "Content-Type": "application/json" }
        }
      );
    }

    if (fetchCalls.length === 2) {
      return new Response("", {
        status: 200,
        headers: { "x-encrypted-param": downloadParam }
      });
    }

    if (fetchCalls.length === 3 || fetchCalls.length === 4) {
      return new Response("{}", {
        status: 200,
        headers: { "Content-Type": "application/json" }
      });
    }

    throw new Error(`Unexpected fetch call ${fetchCalls.length}`);
  };

  try {
    const fileBuffer = Buffer.from("%PDF-1.4\nreport\n", "utf-8");
    await sendFileMessage({
      baseUrl: "https://ilinkai.weixin.qq.com",
      token: "token-1",
      toUserId: "wx-user-1",
      text: "请下载 PDF",
      contextToken: "ctx-1",
      fileName: "report.pdf",
      fileBuffer,
      timeoutMs: 15000,
      cdnBaseUrl: "https://ilinkai.weixin.qq.com"
    });
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(fetchCalls.length, 4);

  const uploadRequest = JSON.parse(String(fetchCalls[0].body));
  const captionRequest = JSON.parse(String(fetchCalls[2].body));
  const sendMessageRequest = JSON.parse(String(fetchCalls[3].body));

  assert.equal(uploadRequest.aeskey, uploadRequest.aeskey.toLowerCase());
  assert.equal(captionRequest.msg.item_list[0].text_item.text, "请下载 PDF");
  assert.equal(
    sendMessageRequest.msg.item_list[0].file_item.media.aes_key,
    Buffer.from(uploadRequest.aeskey).toString("base64")
  );
  assert.equal(
    sendMessageRequest.msg.item_list[0].file_item.media.encrypt_query_param,
    downloadParam
  );
  assert.equal(sendMessageRequest.msg.item_list[0].file_item.file_name, "report.pdf");
  assert.equal(
    sendMessageRequest.msg.item_list[0].file_item.len,
    String(Buffer.byteLength("%PDF-1.4\nreport\n", "utf-8"))
  );
});

test("typing APIs request getconfig and sendtyping with the official payloads", async () => {
  const originalFetch = globalThis.fetch;
  const fetchCalls = [];

  globalThis.fetch = async (url, options = {}) => {
    fetchCalls.push({
      url: String(url),
      method: options.method ?? "GET",
      headers: options.headers ?? {},
      body: options.body
    });

    if (fetchCalls.length === 1) {
      return new Response(
        JSON.stringify({
          typing_ticket: "typing-ticket-xyz"
        }),
        {
          status: 200,
          headers: { "Content-Type": "application/json" }
        }
      );
    }

    if (fetchCalls.length === 2) {
      return new Response("{}", {
        status: 200,
        headers: { "Content-Type": "application/json" }
      });
    }

    throw new Error(`Unexpected fetch call ${fetchCalls.length}`);
  };

  try {
    const typingTicket = await fetchTypingTicket({
      baseUrl: "https://ilinkai.weixin.qq.com",
      token: "token-typing",
      toUserId: "wx-user-typing",
      contextToken: "ctx-typing",
      timeoutMs: 15000
    });
    assert.equal(typingTicket, "typing-ticket-xyz");

    await sendTypingStatus({
      baseUrl: "https://ilinkai.weixin.qq.com",
      token: "token-typing",
      toUserId: "wx-user-typing",
      typingTicket,
      status: WeixinTypingStatus.CANCEL,
      timeoutMs: 15000
    });
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(fetchCalls.length, 2);
  assert.equal(fetchCalls[0].url, "https://ilinkai.weixin.qq.com/ilink/bot/getconfig");
  assert.equal(fetchCalls[1].url, "https://ilinkai.weixin.qq.com/ilink/bot/sendtyping");

  const getConfigRequest = JSON.parse(String(fetchCalls[0].body));
  const sendTypingRequest = JSON.parse(String(fetchCalls[1].body));

  assert.equal(getConfigRequest.ilink_user_id, "wx-user-typing");
  assert.equal(getConfigRequest.context_token, "ctx-typing");
  assert.equal(sendTypingRequest.ilink_user_id, "wx-user-typing");
  assert.equal(sendTypingRequest.typing_ticket, "typing-ticket-xyz");
  assert.equal(sendTypingRequest.status, WeixinTypingStatus.CANCEL);
});
