# 企业微信接入可行性调研(对标现有飞书 bot)

调研日期:2026-06-29。结论先行:**可以,按飞书同样的"长连接"形式接入,核心代码大量复用。**

## 一、为什么可行:企微「智能机器人」长连接 ≈ 飞书长连接事件订阅
企业微信 2024 年起推出的**智能机器人(AI Bot)**支持**长连接 WebSocket 模式**:开发者用 `BotID + Secret`
发起 `aibot_subscribe` 建 WebSocket,企微把单聊/群@消息通过 `aibot_msg_callback` 推下来,
开发者主动推送回复(支持流式,`finish=true` 收尾)。**无需公网回调 URL,内网/本机直接连** —— 这正是
我们飞书 bot 现在用 `lark_oapi` WsClient 的同款玩法。LangBot / blockcell / QClaw / OpenClaw 等都已用此方式接 AI agent,路成熟。

## 二、能力对照(飞书现状 → 企微对应)
| 能力 | 飞书(现 `claude-feishu.py`) | 企微智能机器人长连接 |
|---|---|---|
| 接入方式 | `lark_oapi` WsClient 长连接 | `aibot_subscribe` WebSocket(BotID+Secret) |
| 内网/本机运行 | ✅ 无需公网 | ✅ 无需公网(长连接模式) |
| 收私聊消息 | `im.message.receive_v1` | `aibot_msg_callback`(chattype=single) |
| 收群@消息 | 同上 + mentions | `aibot_msg_callback`(群@) |
| 主动发消息 | REST `/im/v1/messages` 随时发 | `aibot_send_msg`(⚠️ 需用户先发过消息) |
| 流式更新 | 卡片 PATCH(25KB预算/5QPS) | 原生流式消息(`finish=true`),更自然 |
| 富文本 | interactive 卡片 markdown | 图文混排 |
| 进会话欢迎 | — | `aibot_event_callback` |

## 三、复用 vs 重写
- **直接复用(平台无关,0改动)**:`claude_core.py` —— ClaudeSession、跑 Claude CLI、会话/resume/model/mode、转写语音。
- **新写一份 `claude-wecom.py`**(对标 `claude-feishu.py`),替换平台特定层:
  1. **接入层**:`lark_oapi` WsClient → 企微长连接(官方 SDK,或用 `websockets` 自实现 `aibot_subscribe` 握手 + 30s 心跳 + 断线重连)。
  2. **收发消息**:飞书 REST → 企微 API(`aibot_send_msg` / 流式)。
  3. **流式输出**:`FeishuStreamerV2`(卡片 PATCH)→ 企微流式消息(主动推 chunk,`finish=true` 结束)。企微原生流式,反而比飞书 PATCH 卡片更省事。
  4. **命令/会话/relay/名册**:`/new /resume /model /agent`、bot 间接力等业务逻辑**几乎照搬**,只换发送函数。
- 工作量估计:接入层是新东西(1~2天打通握手+流式),其余多为移植。整体远小于从零做。

## 四、必须注意的差异与坑
1. ⚠️ **主动推送受限**:企微规定"用户需先在会话给机器人发过消息,机器人才能主动推送"。
   影响:① bot 间 relay/定时推报告等"无人触发的主动消息"场景受限;② 群里首次须有人 @ 激活。飞书无此限制,迁移时这点要重新设计。
2. **单长连接**:每个 bot 同一时刻只能一个长连接(新连踢旧连)。我们 3 角色=3 bot=3 连接,各自独立,OK;高可用要主备而非多连。
3. **频率限制**:每会话 30 条/分钟、1000 条/小时。流式更新频率要控(飞书是 5QPS 刷卡片,企微按"条"计,流式 chunk 别太碎)。
4. **开通智能机器人**:管理后台/工作台→智能机器人→手动创建→API 模式→**使用长连接**→拿 BotID+Secret。需要企业管理员操作;无企业可个人新建一个企业当管理员。
5. **可见范围/权限**:创建时配可见成员 + 授权;注意 open_id/userid 口径(企微是 userid,与飞书 open_id 不同,relay 名册逻辑要按 userid 改)。
6. 官方文档页提供 SDK 下载,但 Python 版成熟度未知 —— 若官方无 Python SDK,长连接协议简单(JSON over WebSocket),用 `websockets` 自实现成本可控,可参考 blockcell/LangBot 的实现。

## 五、落地步骤建议
1. 管理员开通一个测试用智能机器人(长连接模式),拿 BotID+Secret。
2. 先写最小 PoC:`websockets` 连 `aibot_subscribe` → 收 `aibot_msg_callback` → 回一条固定文本。打通握手+心跳+重连。
3. 接 `claude_core.ClaudeSession`,跑通"私聊→Claude→流式回复"。
4. 移植命令/会话管理;再决定是否移植 relay(注意主动推送限制)。
5. 复刻 `start-bot.sh` 的多角色 .env 机制(`.env.wecom-<role>`)。

## 实测联调坑(2026-06-29 用真实 bot 验证,已写进 claude-wecom.py)
1. **必须清除 HTTP(S)_PROXY**:websockets 15 默认读 `HTTPS_PROXY` 环境变量走代理,某些运行环境(本机有 `HTTPS_PROXY=127.0.0.1:xxxx` 反代)会让 wss 连接被重置(`ConnectionResetError`)。连接前 `os.environ.pop` 掉所有 *_proxy,直连企微网关。验证:直连 TLS 握手 TLSv1.3 OK,订阅返回 `{"errcode":0,"errmsg":"ok"}`。
2. **心跳必须用应用层 JSON,不能用协议层 ping**:企微 WS 不走标准 ping/pong。用 `ping_interval=25` 会触发 `1002 invalid opcode` / `1002 incorrect masking` / `1011 keepalive ping timeout` 连环报错。正解:`websockets.connect(ping_interval=None, ping_timeout=None)` + 每 30s 自发 `{"cmd":"heartbeat","headers":{"req_id":"ping_<ms>"}}`(参考 `chengyongru/wecom_aibot_sdk`)。
3. **单 bot 单长连接**:同一 botid 多进程会互相踢(新连踢旧连),调试时务必 `pkill` 清场只留一个;重连退避 ≥2s,避免疯狂重连触发订阅频率保护。
4. **响应帧无 `cmd` 字段**:订阅/心跳/发送的 ack 是 `{"headers":{"req_id":...},"errcode":0,"errmsg":"ok"}`,按 `req_id` 前缀(`ping_`/`aibot_subscribe_`)分流;`aibot_msg_callback` 等推送才带 `cmd`。

## 六、参考(均为已落地的企微智能机器人长连接接入)
- 官方文档:[智能机器人长连接](https://developer.work.weixin.qq.com/document/path/101463) · [接收消息](https://developer.work.weixin.qq.com/document/path/100719) · [概述](https://developer.work.weixin.qq.com/document/path/101039)
- 开源参考:[LangBot 企微接入](https://docs.langbot.app/zh/usage/platforms/wecom/wecombot) · [blockcell 配置指南](https://github.com/blockcell-labs/blockcell/blob/main/docs/channels/zh/06_wecom.md) · [wecom-bot-svr(回调框架)](https://github.com/easy-wx/wecom-bot-svr)
