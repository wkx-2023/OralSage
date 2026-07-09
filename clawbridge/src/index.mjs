import { createServer } from "node:http";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

import { BridgeStateStore } from "./state-store.mjs";
import { buildBackendBootstrapUrl, loadConfig } from "./config.mjs";
import { ClawBridgeService, ClawBridgeServiceError } from "./bridge-service.mjs";
import { buildKernelPreview, getKernelVersions } from "./kernel.mjs";

const config = loadConfig();
const kernelVersions = getKernelVersions();
const store = new BridgeStateStore(config.runtimeDir);
const state = {
  startedAt: new Date().toISOString(),
  configLoaded: true,
  kernelVersions,
  bootstrap: {
    ok: false,
    statusCode: null,
    fetchedAt: null,
    payload: null,
    error: null
  },
  artifacts: {
    manifestPath: null,
    kernelPreviewPath: null
  }
};
const bridgeService = new ClawBridgeService({
  config,
  store,
  getBootstrapState: () => state.bootstrap,
  log
});

function log(event, details = {}) {
  const payload = {
    ts: new Date().toISOString(),
    service: "clawbridge",
    event,
    ...details
  };
  console.log(JSON.stringify(payload));
}

function json(response, statusCode, payload) {
  response.statusCode = statusCode;
  response.setHeader("content-type", "application/json; charset=utf-8");
  response.end(JSON.stringify(payload));
}

function getHeader(request, name) {
  return String(request.headers[String(name).toLowerCase()] ?? "").trim();
}

function isAuthorizedInternalRequest(request) {
  return getHeader(request, "X-AlertClaw-ClawBridge-Secret") === config.internalSecret;
}

async function readJsonBody(request) {
  const chunks = [];
  for await (const chunk of request) {
    chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
  }
  const raw = Buffer.concat(chunks).toString("utf-8").trim();
  if (!raw) {
    return {};
  }
  return JSON.parse(raw);
}

async function probeBackendBootstrap() {
  const target = buildBackendBootstrapUrl(config);
  try {
    const response = await fetch(target, {
      headers: {
        "X-AlertClaw-ClawBridge-Secret": config.internalSecret,
        "X-AlertClaw-ClawBridge-Instance-Id": config.instanceId
      }
    });
    const payload = await response.json().catch(() => null);
    state.bootstrap = {
      ok: response.ok,
      statusCode: response.status,
      fetchedAt: new Date().toISOString(),
      payload,
      error: response.ok ? null : `bootstrap_http_${response.status}`
    };
    log("bootstrap_probe", {
      ok: response.ok,
      statusCode: response.status,
      target
    });
  } catch (error) {
    state.bootstrap = {
      ok: false,
      statusCode: null,
      fetchedAt: new Date().toISOString(),
      payload: null,
      error: error instanceof Error ? error.message : "bootstrap_probe_failed"
    };
    log("bootstrap_probe_failed", {
      target,
      message: state.bootstrap.error
    });
  }
}

async function writeRuntimeArtifacts() {
  await mkdir(config.runtimeDir, { recursive: true });
  const kernelPreview = buildKernelPreview(config, kernelVersions);
  const manifest = {
    generatedAt: new Date().toISOString(),
    phase: "binding-and-ingress",
    service: config.serviceName,
    version: config.serviceVersion,
    instanceId: config.instanceId,
    backendBootstrapUrl: buildBackendBootstrapUrl(config),
    backendBootstrap: state.bootstrap,
    kernelVersions,
    workers: bridgeService.getWorkerStats()
  };
  const manifestPath = path.join(config.runtimeDir, "bridge-manifest.json");
  const kernelPreviewPath = path.join(config.runtimeDir, "openclaw-kernel.preview.json");
  await writeFile(manifestPath, JSON.stringify(manifest, null, 2), "utf-8");
  await writeFile(kernelPreviewPath, JSON.stringify(kernelPreview, null, 2), "utf-8");
  state.artifacts = {
    manifestPath,
    kernelPreviewPath
  };
}

function buildHealthPayload() {
  return {
    service: config.serviceName,
    version: config.serviceVersion,
    mode: "binding-and-ingress",
    status: "ok",
    instanceId: config.instanceId,
    enableOpenClawKernel: config.enableOpenClawKernel,
    kernelVersions: state.kernelVersions,
    backendBootstrap: {
      ok: state.bootstrap.ok,
      statusCode: state.bootstrap.statusCode,
      fetchedAt: state.bootstrap.fetchedAt,
      error: state.bootstrap.error
    },
    runtimeArtifacts: state.artifacts,
    workers: bridgeService.getWorkerStats(),
    startedAt: state.startedAt
  };
}

const server = createServer(async (request, response) => {
  if (!request.url) {
    json(response, 400, { status: "error", message: "Missing request URL." });
    return;
  }

  const requestUrl = new URL(request.url, config.publicBaseUrl);
  if (requestUrl.pathname === "/health") {
    json(response, 200, buildHealthPayload());
    return;
  }
  if (requestUrl.pathname === "/ready") {
    const ready = Boolean(
      state.bootstrap.ok && state.artifacts.manifestPath && state.artifacts.kernelPreviewPath
    );
    json(response, ready ? 200 : 503, {
      status: ready ? "ready" : "starting",
      bridge: config.serviceName,
      instanceId: config.instanceId
    });
    return;
  }
  if (requestUrl.pathname === "/manifest") {
    json(response, 200, {
      config: {
        serviceName: config.serviceName,
        serviceVersion: config.serviceVersion,
        instanceId: config.instanceId,
        backendBaseUrl: config.backendBaseUrl,
        runtimeDir: config.runtimeDir
      },
      kernelPreview: buildKernelPreview(config, kernelVersions),
      backendBootstrap: state.bootstrap,
      workers: bridgeService.getWorkerStats()
    });
    return;
  }

  if (requestUrl.pathname.startsWith("/api/v1/")) {
    if (!isAuthorizedInternalRequest(request)) {
      json(response, 401, {
        status: "error",
        message: "ClawBridge internal authentication failed."
      });
      return;
    }

    try {
      if (request.method === "POST" && requestUrl.pathname === "/api/v1/weixin/qr/start") {
        const payload = await readJsonBody(request);
        const sessionId = String(payload.session_id ?? "").trim();
        if (!sessionId) {
          json(response, 422, {
            status: "error",
            message: "session_id is required."
          });
          return;
        }
        const qrSession = await bridgeService.startQrSession(sessionId);
        json(response, 200, {
          session_id: qrSession.sessionId,
          qr_data_url: qrSession.qrDataUrl,
          message: qrSession.message
        });
        return;
      }

      if (request.method === "GET" && requestUrl.pathname === "/api/v1/weixin/qr/status") {
        const sessionId = String(requestUrl.searchParams.get("session_id") ?? "").trim();
        const timeoutMs = Number.parseInt(
          String(requestUrl.searchParams.get("timeout_ms") ?? config.qrStatusTimeoutMs),
          10
        );
        if (!sessionId) {
          json(response, 422, {
            status: "error",
            message: "session_id is required."
          });
          return;
        }
        const status = await bridgeService.getQrSessionStatus(sessionId, timeoutMs);
        json(response, 200, {
          connected: status.connected,
          status: status.status,
          qr_data_url: status.qrDataUrl,
          message: status.message,
          weixin_account_id: status.weixinAccountId,
          weixin_user_id: status.weixinUserId,
          clawbot_instance_id: status.clawbotInstanceId
        });
        return;
      }

      if (request.method === "GET" && requestUrl.pathname.startsWith("/api/v1/weixin/accounts/")) {
        const accountId = decodeURIComponent(requestUrl.pathname.replace("/api/v1/weixin/accounts/", ""));
        const status = bridgeService.getAccountStatus(accountId);
        if (!status) {
          json(response, 404, {
            status: "error",
            message: "The requested Weixin account was not found."
          });
          return;
        }
        json(response, 200, status);
        return;
      }

      if (request.method === "POST" && requestUrl.pathname === "/api/v1/weixin/messages") {
        const payload = await readJsonBody(request);
        const text = String(payload.text ?? "").trim();
        const clawbotInstanceId = String(payload.clawbot_instance_id ?? "").trim() || null;
        const weixinAccountId = String(payload.weixin_account_id ?? "").trim() || null;
        if (!text) {
          json(response, 422, {
            status: "error",
            message: "text is required."
          });
          return;
        }
        if (!clawbotInstanceId && !weixinAccountId) {
          json(response, 422, {
            status: "error",
            message: "Either clawbot_instance_id or weixin_account_id is required."
          });
          return;
        }
        const sent = await bridgeService.sendProactiveMessage({
          text,
          clawbotInstanceId,
          weixinAccountId
        });
        json(response, 200, {
          status: sent.status,
          account_id: sent.accountId,
          weixin_account_id: sent.weixinAccountId,
          clawbot_instance_id: sent.clawbotInstanceId,
          message_id: sent.messageId,
          sent_at: sent.sentAt
        });
        return;
      }

      if (request.method === "POST" && requestUrl.pathname === "/api/v1/weixin/files") {
        const payload = await readJsonBody(request);
        const text = String(payload.text ?? "").trim();
        const clawbotInstanceId = String(payload.clawbot_instance_id ?? "").trim() || null;
        const weixinAccountId = String(payload.weixin_account_id ?? "").trim() || null;
        const fileName = String(payload.file_name ?? "").trim();
        const contentType = String(payload.content_type ?? "application/octet-stream").trim() || "application/octet-stream";
        const dataBase64 = String(payload.data_base64 ?? "").trim();
        if (!fileName || !dataBase64) {
          json(response, 422, {
            status: "error",
            message: "file_name and data_base64 are required."
          });
          return;
        }
        if (!clawbotInstanceId && !weixinAccountId) {
          json(response, 422, {
            status: "error",
            message: "Either clawbot_instance_id or weixin_account_id is required."
          });
          return;
        }
        const fileBuffer = Buffer.from(dataBase64, "base64");
        const sent = await bridgeService.sendProactiveFile({
          text,
          clawbotInstanceId,
          weixinAccountId,
          fileName,
          contentType,
          fileBuffer
        });
        json(response, 200, {
          status: sent.status,
          account_id: sent.accountId,
          weixin_account_id: sent.weixinAccountId,
          clawbot_instance_id: sent.clawbotInstanceId,
          message_id: sent.messageId,
          sent_at: sent.sentAt
        });
        return;
      }
    } catch (error) {
      const statusCode =
        error instanceof ClawBridgeServiceError && Number.isFinite(error.statusCode)
          ? error.statusCode
          : 502;
      json(response, statusCode, {
        status: "error",
        message: error instanceof Error ? error.message : String(error)
      });
      return;
    }
  }

  json(response, 404, {
    status: "not_found",
    message: "Unknown ClawBridge route."
  });
});

async function refreshBootstrapAndArtifacts() {
  await probeBackendBootstrap();
  await writeRuntimeArtifacts();
}

async function start() {
  if (!config.internalSecret) {
    throw new Error("CLAWBRIDGE_INTERNAL_SECRET or AUTH_INTERNAL_OPENCLAW_SECRET is required.");
  }
  await store.init();
  await refreshBootstrapAndArtifacts();
  await bridgeService.init();
  server.listen(config.port, () => {
    log("started", {
      port: config.port,
      instanceId: config.instanceId,
      enableOpenClawKernel: config.enableOpenClawKernel
    });
  });
  setInterval(() => {
    refreshBootstrapAndArtifacts().catch((error) => {
      log("background_refresh_failed", {
        message: error instanceof Error ? error.message : String(error)
      });
    });
  }, config.bootstrapRefreshSeconds * 1000).unref();
}

async function shutdown(signal) {
  log("shutdown", { signal });
  await bridgeService.stopAllWorkers();
  server.close(() => process.exit(0));
}

process.on("SIGINT", () => {
  shutdown("SIGINT").catch(() => process.exit(0));
});
process.on("SIGTERM", () => {
  shutdown("SIGTERM").catch(() => process.exit(0));
});

start().catch((error) => {
  log("startup_failed", {
    message: error instanceof Error ? error.message : String(error)
  });
  process.exit(1);
});
