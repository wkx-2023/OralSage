import { createRequire } from "node:module";
import fs from "node:fs";
import path from "node:path";

import { buildBackendBootstrapUrl } from "./config.mjs";

const require = createRequire(import.meta.url);

function readPackageVersion(packagePath, fallback = "unknown") {
  try {
    return require(packagePath).version ?? fallback;
  } catch {
    try {
      const packageName = packagePath.replace(/\/package\.json$/, "");
      const entryFile = require.resolve(packageName);
      let currentDir = path.dirname(entryFile);
      for (let attempts = 0; attempts < 6; attempts += 1) {
        const candidate = path.join(currentDir, "package.json");
        if (fs.existsSync(candidate)) {
          return JSON.parse(fs.readFileSync(candidate, "utf-8")).version ?? fallback;
        }
        const nextDir = path.dirname(currentDir);
        if (nextDir === currentDir) {
          break;
        }
        currentDir = nextDir;
      }
    } catch {
      return fallback;
    }
  }
  return fallback;
}

export function getKernelVersions() {
  return {
    openclaw: readPackageVersion("openclaw/package.json"),
    weixinPlugin: readPackageVersion("@tencent-weixin/openclaw-weixin/package.json")
  };
}

export function buildKernelPreview(config, kernelVersions = getKernelVersions()) {
  return {
    generatedAt: new Date().toISOString(),
    bridge: {
      name: config.serviceName,
      version: config.serviceVersion,
      instanceId: config.instanceId,
      publicBaseUrl: config.publicBaseUrl,
      runtimeDir: config.runtimeDir
    },
    kernel: {
      openclawPackage: "openclaw",
      openclawVersion: kernelVersions.openclaw,
      weixinPluginPackage: "@tencent-weixin/openclaw-weixin",
      weixinPluginVersion: kernelVersions.weixinPlugin,
      enableOpenClawKernel: config.enableOpenClawKernel,
      weixinPluginBaseUrl: config.weixinPluginBaseUrl,
      deviceIdentityFile: config.deviceIdentityFile,
      pairedDevicesFile: config.pairedDevicesFile
    },
    backend: {
      baseUrl: config.backendBaseUrl,
      bootstrapUrl: buildBackendBootstrapUrl(config),
      internalHeaders: {
        secret: "X-AlertClaw-ClawBridge-Secret",
        instanceId: "X-AlertClaw-ClawBridge-Instance-Id",
        clawbotInstanceId: "X-AlertClaw-ClawBot-Instance-Id",
        weixinAccountId: "X-Weixin-Account-Id",
        weixinUserId: "X-Weixin-User-Id"
      }
    }
  };
}
