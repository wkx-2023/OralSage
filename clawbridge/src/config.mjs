import { existsSync, readFileSync } from "node:fs";
import path from "node:path";

const DEFAULTS = {
  serviceName: "OralSage ClawBridge",
  serviceVersion: "0.1.0",
  instanceId: "oralsage-clawbridge-local",
  port: 8787,
  publicBaseUrl: "http://127.0.0.1:8787",
  backendBaseUrl: "http://127.0.0.1:8000",
  runtimeDir: "./runtime",
  internalSecret: "",
  enableOpenClawKernel: false,
  weixinPluginBaseUrl: "https://ilinkai.weixin.qq.com",
  deviceIdentityFile: "/run/openclaw-identity/device.json",
  pairedDevicesFile: "/run/openclaw-devices/paired.json",
  bootstrapRefreshSeconds: 60,
  apiTimeoutMs: 15000,
  chatDispatchTimeoutMs: 300000,
  qrStatusTimeoutMs: 35000,
  longPollTimeoutMs: 35000,
  maxImageBytes: 5000000,
  maxAttachmentBytes: 15000000
};

function stripInlineComment(value) {
  let quote = "";
  let escaped = false;
  for (let index = 0; index < value.length; index += 1) {
    const char = value[index];
    if (escaped) {
      escaped = false;
      continue;
    }
    if (char === "\\") {
      escaped = true;
      continue;
    }
    if (char === "'" || char === "\"") {
      if (quote === char) {
        quote = "";
      } else if (!quote) {
        quote = char;
      }
      continue;
    }
    if (char === "#" && !quote && (index === 0 || /\s/.test(value[index - 1]))) {
      return value.slice(0, index).trimEnd();
    }
  }
  return value.trim();
}

function normalizeEnvValue(value) {
  const normalized = stripInlineComment(value.trim());
  if (
    normalized.length >= 2 &&
    normalized[0] === normalized[normalized.length - 1] &&
    ["'", "\""].includes(normalized[0])
  ) {
    return normalized.slice(1, -1);
  }
  return normalized;
}

function readDotEnvFile(filePath) {
  if (!existsSync(filePath)) {
    return {};
  }
  const loaded = {};
  for (const rawLine of readFileSync(filePath, "utf-8").replace(/^\uFEFF/, "").split(/\r?\n/)) {
    let line = rawLine.trim();
    if (!line || line.startsWith("#") || !line.includes("=")) {
      continue;
    }
    if (line.startsWith("export ")) {
      line = line.slice(7).trimStart();
    }
    const separatorIndex = line.indexOf("=");
    const key = line.slice(0, separatorIndex).trim();
    if (!key) {
      continue;
    }
    loaded[key] = normalizeEnvValue(line.slice(separatorIndex + 1));
  }
  return loaded;
}

function loadRuntimeEnv(env) {
  if (env !== process.env) {
    return env;
  }
  const cwd = process.cwd();
  return {
    ...readDotEnvFile(path.resolve(cwd, "..", ".env")),
    ...readDotEnvFile(path.resolve(cwd, ".env")),
    ...env
  };
}

function parseBoolean(value, fallback) {
  if (value === undefined || value === null || String(value).trim() === "") {
    return fallback;
  }
  const normalized = String(value).trim().toLowerCase();
  return ["1", "true", "yes", "on"].includes(normalized);
}

function parsePositiveInteger(value, fallback) {
  const parsed = Number.parseInt(String(value ?? ""), 10);
  if (Number.isFinite(parsed) && parsed > 0) {
    return parsed;
  }
  return fallback;
}

function normalizeUrl(value, fallback) {
  const candidate = String(value ?? "").trim() || fallback;
  return candidate.replace(/\/+$/, "");
}

export function loadConfig(env = process.env) {
  env = loadRuntimeEnv(env);
  const internalSecret = String(
    env.CLAWBRIDGE_INTERNAL_SECRET
      ?? env.AUTH_INTERNAL_OPENCLAW_SECRET
      ?? env.AUTH_INTERNAL_WEIXIN_SECRET
      ?? env.AUTH_INTERNAL_QQBOT_SECRET
      ?? env.NEWPROJECT_INTERNAL_SECRET
      ?? DEFAULTS.internalSecret
  ).trim();

  return {
    serviceName: String(env.CLAWBRIDGE_SERVICE_NAME ?? DEFAULTS.serviceName).trim() || DEFAULTS.serviceName,
    serviceVersion: String(env.CLAWBRIDGE_SERVICE_VERSION ?? DEFAULTS.serviceVersion).trim() || DEFAULTS.serviceVersion,
    instanceId: String(env.CLAWBRIDGE_INSTANCE_ID ?? DEFAULTS.instanceId).trim() || DEFAULTS.instanceId,
    port: parsePositiveInteger(env.CLAWBRIDGE_PORT, DEFAULTS.port),
    publicBaseUrl: normalizeUrl(env.CLAWBRIDGE_PUBLIC_BASE_URL, DEFAULTS.publicBaseUrl),
    backendBaseUrl: normalizeUrl(env.CLAWBRIDGE_BACKEND_BASE_URL, DEFAULTS.backendBaseUrl),
    runtimeDir: String(env.CLAWBRIDGE_RUNTIME_DIR ?? DEFAULTS.runtimeDir).trim() || DEFAULTS.runtimeDir,
    internalSecret,
    enableOpenClawKernel: parseBoolean(
      env.CLAWBRIDGE_ENABLE_OPENCLAW_KERNEL,
      DEFAULTS.enableOpenClawKernel
    ),
    weixinPluginBaseUrl: normalizeUrl(
      env.CLAWBRIDGE_WEIXIN_PLUGIN_BASE_URL,
      DEFAULTS.weixinPluginBaseUrl
    ),
    deviceIdentityFile:
      String(env.CLAWBRIDGE_DEVICE_IDENTITY_FILE ?? DEFAULTS.deviceIdentityFile).trim() ||
      DEFAULTS.deviceIdentityFile,
    pairedDevicesFile:
      String(env.CLAWBRIDGE_PAIRED_DEVICES_FILE ?? DEFAULTS.pairedDevicesFile).trim() ||
      DEFAULTS.pairedDevicesFile,
    bootstrapRefreshSeconds: parsePositiveInteger(
      env.CLAWBRIDGE_BOOTSTRAP_REFRESH_SECONDS,
      DEFAULTS.bootstrapRefreshSeconds
    ),
    apiTimeoutMs: parsePositiveInteger(
      env.CLAWBRIDGE_API_TIMEOUT_MS,
      DEFAULTS.apiTimeoutMs
    ),
    chatDispatchTimeoutMs: parsePositiveInteger(
      env.CLAWBRIDGE_CHAT_DISPATCH_TIMEOUT_MS,
      DEFAULTS.chatDispatchTimeoutMs
    ),
    qrStatusTimeoutMs: parsePositiveInteger(
      env.CLAWBRIDGE_QR_STATUS_TIMEOUT_MS,
      DEFAULTS.qrStatusTimeoutMs
    ),
    longPollTimeoutMs: parsePositiveInteger(
      env.CLAWBRIDGE_LONG_POLL_TIMEOUT_MS,
      DEFAULTS.longPollTimeoutMs
    ),
    maxImageBytes: parsePositiveInteger(
      env.CLAWBRIDGE_MAX_IMAGE_BYTES,
      DEFAULTS.maxImageBytes
    ),
    maxAttachmentBytes: parsePositiveInteger(
      env.CLAWBRIDGE_MAX_ATTACHMENT_BYTES,
      DEFAULTS.maxAttachmentBytes
    )
  };
}

export function buildBackendBootstrapUrl(config) {
  return new URL("/api/v1/internal/clawbridge/bootstrap", `${config.backendBaseUrl}/`).toString();
}
