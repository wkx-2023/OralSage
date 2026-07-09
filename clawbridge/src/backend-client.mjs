function normalizeHeaders(bootstrap) {
  const headers = bootstrap?.payload?.headers;
  return {
    clawbridgeSecret: headers?.clawbridge_secret || "X-AlertClaw-ClawBridge-Secret",
    clawbridgeInstanceId: headers?.clawbridge_instance_id || "X-AlertClaw-ClawBridge-Instance-Id"
  };
}

function normalizeEndpoints(bootstrap) {
  const endpoints = bootstrap?.payload?.endpoints;
  return {
    internalChatboxMessages:
      endpoints?.internal_chatbox_messages || "/api/v1/internal/clawbridge/chatbox/messages"
  };
}

export async function sendInboundChatMessage({
  config,
  bootstrap,
  message
}) {
  const headers = normalizeHeaders(bootstrap);
  const endpoints = normalizeEndpoints(bootstrap);
  const target = new URL(endpoints.internalChatboxMessages, `${config.backendBaseUrl}/`).toString();
  const controller = new AbortController();
  const timeout = setTimeout(
    () => controller.abort(),
    config.chatDispatchTimeoutMs ?? config.apiTimeoutMs ?? 15000
  );
  const response = await fetch(target, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      [headers.clawbridgeSecret]: config.internalSecret,
      [headers.clawbridgeInstanceId]: config.instanceId
    },
    body: JSON.stringify(message),
    signal: controller.signal
  }).finally(() => clearTimeout(timeout));
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = payload?.detail;
    const errorCode =
      typeof detail?.error_code === "string" && detail.error_code.trim()
        ? detail.error_code.trim()
        : `http_${response.status}`;
    const errorMessage =
      typeof detail?.message === "string" && detail.message.trim()
        ? detail.message.trim()
        : `Backend chat dispatch failed with status ${response.status}.`;
    const error = new Error(errorMessage);
    error.code = errorCode;
    throw error;
  }
  if (!payload || typeof payload !== "object") {
    throw new Error("Backend chat dispatch returned an invalid payload.");
  }
  return payload;
}
