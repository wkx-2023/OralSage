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
