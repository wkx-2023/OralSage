# AlertClaw ClawBridge

`AlertClaw ClawBridge` 是本仓库中自研的微信 `ClawBot` 接入服务。

它主要负责：

1. 二维码绑定
2. 持久化微信机器人会话
3. 轮询微信侧的入站消息
4. 将入站消息转发到 AlertClaw 后端
5. 将后端回复返回给微信
6. 向已绑定的微信用户主动推送监控预警
7. 将生成完成的 PDF 报告以文件形式发送回微信

它不承载产品业务逻辑。监控、报告、PDF 导出以及主聊天编排仍然由 Python 后端负责。

## 本地运行

```bash
cd clawbridge
npm install
npm run start
```

默认端口：`8787`

主要路由：

1. `/health`
2. `/ready`
3. `/manifest`
4. `/api/v1/weixin/qr/start`
5. `/api/v1/weixin/qr/status`
6. `/api/v1/weixin/accounts/{account_id}`
7. `/api/v1/weixin/messages`
8. `/api/v1/weixin/files`

`/api/v1/weixin/messages` 仅供内部调用。AlertClaw 后端会通过它让 ClawBridge 主动发送微信预警消息。

`/api/v1/weixin/files` 仅供内部调用。AlertClaw 后端会通过它让 ClawBridge 发送生成完成的 PDF 文件。

## 运行时产物

`CLAWBRIDGE_RUNTIME_DIR` 会存放：

1. `bridge-manifest.json`
2. `openclaw-kernel.preview.json`
3. `state/clawbridge-state.json`
