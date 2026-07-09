import test from "node:test";
import assert from "node:assert/strict";

import { buildBackendBootstrapUrl, loadConfig } from "../src/config.mjs";
import { buildKernelPreview } from "../src/kernel.mjs";

test("loadConfig prefers AlertClaw bridge settings", () => {
  const config = loadConfig({
    CLAWBRIDGE_SERVICE_NAME: "Bridge X",
    CLAWBRIDGE_INTERNAL_SECRET: "secret-1",
    CLAWBRIDGE_PORT: "9900",
    CLAWBRIDGE_ENABLE_OPENCLAW_KERNEL: "true",
    CLAWBRIDGE_BACKEND_BASE_URL: "http://app:8000/"
  });

  assert.equal(config.serviceName, "Bridge X");
  assert.equal(config.internalSecret, "secret-1");
  assert.equal(config.port, 9900);
  assert.equal(config.enableOpenClawKernel, true);
  assert.equal(buildBackendBootstrapUrl(config), "http://app:8000/api/v1/internal/clawbridge/bootstrap");
});

test("buildKernelPreview exposes kernel and backend metadata", () => {
  const config = loadConfig({
    CLAWBRIDGE_INTERNAL_SECRET: "secret-2",
    CLAWBRIDGE_INSTANCE_ID: "bridge-local-2"
  });

  const preview = buildKernelPreview(config, {
    openclaw: "2026.4.9",
    weixinPlugin: "2.1.7"
  });

  assert.equal(preview.bridge.instanceId, "bridge-local-2");
  assert.equal(preview.kernel.openclawVersion, "2026.4.9");
  assert.equal(preview.backend.internalHeaders.secret, "X-AlertClaw-ClawBridge-Secret");
});
