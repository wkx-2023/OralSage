import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";

const EMPTY_STATE = {
  qrSessions: {},
  accounts: {}
};

export class BridgeStateStore {
  constructor(runtimeDir) {
    this.runtimeDir = runtimeDir;
    this.stateDir = path.join(runtimeDir, "state");
    this.statePath = path.join(this.stateDir, "clawbridge-state.json");
    this.state = structuredClone(EMPTY_STATE);
    this.writeChain = Promise.resolve();
  }

  async init() {
    await mkdir(this.stateDir, { recursive: true });
    try {
      const raw = await readFile(this.statePath, "utf-8");
      const parsed = JSON.parse(raw);
      this.state = {
        qrSessions: parsed?.qrSessions && typeof parsed.qrSessions === "object" ? parsed.qrSessions : {},
        accounts: parsed?.accounts && typeof parsed.accounts === "object" ? parsed.accounts : {}
      };
    } catch (error) {
      if (error && typeof error === "object" && error.code === "ENOENT") {
        await this.persist();
        return;
      }
      throw error;
    }
  }

  snapshot() {
    return structuredClone(this.state);
  }

  listQrSessions() {
    return Object.values(this.state.qrSessions).map((item) => ({ ...item }));
  }

  getQrSession(sessionId) {
    const session = this.state.qrSessions[sessionId];
    return session ? { ...session } : null;
  }

  async setQrSession(session) {
    this.state.qrSessions[session.sessionId] = { ...session };
    await this.persist();
    return this.getQrSession(session.sessionId);
  }

  async deleteQrSession(sessionId) {
    delete this.state.qrSessions[sessionId];
    await this.persist();
  }

  listAccounts() {
    return Object.values(this.state.accounts).map((item) => ({ ...item }));
  }

  getAccount(accountId) {
    const account = this.state.accounts[accountId];
    return account ? { ...account } : null;
  }

  async setAccount(account) {
    this.state.accounts[account.accountId] = { ...account };
    await this.persist();
    return this.getAccount(account.accountId);
  }

  async patchAccount(accountId, updates) {
    const existing = this.state.accounts[accountId];
    if (!existing) {
      return null;
    }
    this.state.accounts[accountId] = {
      ...existing,
      ...updates
    };
    await this.persist();
    return this.getAccount(accountId);
  }

  async deleteAccount(accountId) {
    delete this.state.accounts[accountId];
    await this.persist();
  }

  async persist() {
    this.writeChain = this.writeChain.then(async () => {
      await mkdir(this.stateDir, { recursive: true });
      await writeFile(this.statePath, JSON.stringify(this.state, null, 2), "utf-8");
    });
    return this.writeChain;
  }
}
