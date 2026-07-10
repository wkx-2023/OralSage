# OralSage ClawBridge

`OralSage ClawBridge` 是 OralSage 的微信 `ClawBot` 接入服务，负责把微信扫码、微信消息和后端学习计划能力连接起来。

它负责：

1. 生成微信扫码绑定二维码
2. 保存微信机器人会话和 `bot_token`
3. 轮询微信侧入站消息
4. 将微信消息转发给 OralSage 后端
5. 将后端回复返回给微信用户
6. 主动向已绑定微信用户推送每日学习计划

它不承载学习计划业务本身。每日计划、打卡、修改计划和延期计划由 Python 后端的学习计划服务处理。

## 本地运行

先启动 OralSage 后端，默认地址为 `http://127.0.0.1:8000`。

```bash
cd clawbridge
npm install
npm run start
```

默认端口：`8787`

建议环境变量：

```bash
CLAWBRIDGE_BACKEND_BASE_URL=http://127.0.0.1:8000
CLAWBRIDGE_PUBLIC_BASE_URL=http://127.0.0.1:8787
CLAWBRIDGE_INSTANCE_ID=oralsage-clawbridge-local
CLAWBRIDGE_INTERNAL_SECRET=oralsage-local-dev-secret
CLAWBRIDGE_RUNTIME_DIR=./runtime
```

后端也要设置同样的 `ORALSAGE_CLAWBRIDGE_INTERNAL_SECRET` 或 `CLAWBRIDGE_INTERNAL_SECRET`。

## 主要路由

1. `/health`
2. `/ready`
3. `/manifest`
4. `/api/v1/weixin/qr/start`
5. `/api/v1/weixin/qr/status`
6. `/api/v1/weixin/accounts/{account_id}`
7. `/api/v1/weixin/messages`
8. `/api/v1/weixin/files`

`/api/v1/weixin/messages` 是内部接口，用于 OralSage 后端主动推送每日计划。

## 后端配合接口

ClawBridge 启动后会向后端请求：

1. `/api/v1/internal/clawbridge/bootstrap`
2. `/api/v1/internal/clawbridge/chatbox/messages`

微信用户发来的文本会被转发到 `chatbox/messages`，后端会按计划助手指令处理：

1. `今日计划`
2. `推荐计划`
3. `添加计划 复习牙周病基础`
4. `打卡 1`
5. `修改 1 复习牙髓炎鉴别诊断`
6. `延期 1 到明天`

## 运行时产物

`CLAWBRIDGE_RUNTIME_DIR` 会存放：

1. `bridge-manifest.json`
2. `openclaw-kernel.preview.json`
3. `state/clawbridge-state.json`
