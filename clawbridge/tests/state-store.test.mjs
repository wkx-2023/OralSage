import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

import { BridgeStateStore } from "../src/state-store.mjs";

test("BridgeStateStore persists qr sessions and accounts", async () => {
  const runtimeDir = await mkdtemp(path.join(tmpdir(), "clawbridge-state-"));
  const store = new BridgeStateStore(runtimeDir);
  await store.init();

  await store.setQrSession({
    sessionId: "qr-1",
    qrDataUrl: "data:image/png;base64,abc",
    status: "waiting"
  });
  await store.setAccount({
    accountId: "bot-1",
    weixinAccountId: "bot-1@im.bot",
    clawbotInstanceId: "clawbridge:bot-1",
    botToken: "token-1",
    baseUrl: "https://ilinkai.weixin.qq.com"
  });

  const reloaded = new BridgeStateStore(runtimeDir);
  await reloaded.init();

  assert.equal(reloaded.getQrSession("qr-1").qrDataUrl, "data:image/png;base64,abc");
  assert.equal(reloaded.getAccount("bot-1").weixinAccountId, "bot-1@im.bot");
});
