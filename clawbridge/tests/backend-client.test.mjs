import test from "node:test";
import assert from "node:assert/strict";

import { sendInboundChatMessage } from "../src/backend-client.mjs";

test("sendInboundChatMessage forwards the internal bridge headers", async () => {
  const originalFetch = global.fetch;
  global.fetch = async (url, options) => {
    assert.equal(url, "http://app:8000/api/v1/internal/clawbridge/chatbox/messages");
    assert.equal(options.headers["X-AlertClaw-ClawBridge-Secret"], "secret-1");
    assert.equal(options.headers["X-AlertClaw-ClawBridge-Instance-Id"], "bridge-1");
    return {
      ok: true,
      json: async () => ({ status: "ok", reply_text: "hello" })
    };
  };

  await assert.doesNotReject(() =>
    sendInboundChatMessage({
      config: {
        backendBaseUrl: "http://app:8000",
        internalSecret: "secret-1",
        instanceId: "bridge-1"
      },
      bootstrap: {
        payload: {
          endpoints: {
            internal_chatbox_messages: "/api/v1/internal/clawbridge/chatbox/messages"
          },
          headers: {
            clawbridge_secret: "X-AlertClaw-ClawBridge-Secret",
            clawbridge_instance_id: "X-AlertClaw-ClawBridge-Instance-Id"
          }
        }
      },
      message: {
        clawbot_instance_id: "clawbridge:bot-1",
        text: "你好"
      }
    })
  );

  global.fetch = originalFetch;
});

test("sendInboundChatMessage exposes backend error codes", async () => {
  const originalFetch = global.fetch;
  global.fetch = async () => ({
    ok: false,
    status: 404,
    json: async () => ({
      detail: {
        error_code: "auth_weixin_not_bound",
        message: "not bound"
      }
    })
  });

  await assert.rejects(
    () =>
      sendInboundChatMessage({
        config: {
          backendBaseUrl: "http://app:8000",
          internalSecret: "secret-1",
          instanceId: "bridge-1"
        },
        bootstrap: {
          payload: {
            endpoints: {
              internal_chatbox_messages: "/api/v1/internal/clawbridge/chatbox/messages"
            },
            headers: {
              clawbridge_secret: "X-AlertClaw-ClawBridge-Secret",
              clawbridge_instance_id: "X-AlertClaw-ClawBridge-Instance-Id"
            }
          }
        },
        message: {
          clawbot_instance_id: "clawbridge:bot-1",
          text: "你好"
        }
      }),
    (error) => error.code === "auth_weixin_not_bound"
  );

  global.fetch = originalFetch;
});
