# YouTube 同行频道监控（GitHub Actions 版）

监控 14 个活跃海外同行 YouTube 频道（Fluke / Fluke Calibration / Fluke Europe / Fluke Process Instruments / BeamexCalibration / WIKA Group / WIKA USA / Mensor / AMETEK STC (JOFRA) / AMETEK Inc. / Additel / Testo / Baker Hughes（覆盖 Druck）/ OMEGA Engineering），每天北京时间 08:00 由 GitHub Actions 境外 runner 自动采集，结果写回仓库 `data/latest.json`，并（可选）推送飞书群机器人。

频道甄别说明：Beamex / WIKA 主频道 / AMETEK STC 的频道 ID 均经 2026-10-29 runner 实地验证（官方描述 + 订阅数 + 近期视频）；Druck 确认无独立官方频道，已停用该条目，其内容经 Baker Hughes 频道覆盖。

## 为什么这么绕

需求方的服务器环境直连不了 YouTube（实测 502），GitHub Actions 的 runner 在海外可直连；公共仓库的 Actions 对个人免费。飞书侧的值守 agent 每天通过 GitHub API 读取 `data/latest.json`，把结果并进 09:00 的同行动态日报。

## 工作方式

- 每天北京时间 08:00（UTC 00:00）触发 `collect.py`，也可在 Actions 页面手动触发（workflow_dispatch）
- 每个频道走 YouTube RSS（`feeds/videos.xml?channel_id=...`），无需 API key、无配额
- 频道列表在 `channels.json`：没填 `channel_id` 的条目，脚本会从 `handle` 或 `user`（旧式用户名）自动解析出频道 ID 并回写提交
- 时间窗：上次成功运行 −1h 到现在，下限 now−25h（漏跑多天会自动拉宽追平）；已上报的 `video_id` 记在 `data/state.json` 永不重复报
- 单频道失败不影响其他频道，失败明细写进 `data/latest.json` 的 `failures`
- 有新视频且配置了 webhook 时，向飞书群机器人发文本消息

## 一次性配置

1. 把本目录内容推到一个 GitHub 仓库（根目录需有 `collect.py`、`channels.json`、`.github/workflows/collect.yml`）
2. 仓库 Settings → Secrets and variables → Actions，添加：
   - `FEISHU_WEBHOOK_URL`：飞书群 → 设置 → 群机器人 → 添加「自定义机器人」后拿到的 webhook 地址（不配则只落盘、不推群）
   - `FEISHU_WEBHOOK_SECRET`：自定义机器人的签名密钥（没开签名校验可不配）
3. Actions 页确认 `youtube-monitor` 工作流已启用；手动 Run一次 验证
4. 查看 `data/latest.json`：`failures` 为空即全部频道解析成功；有失败按提示修正 `channels.json` 里的 handle

## 日常维护

- 某频道改名/搬家：改 `channels.json` 对应条目的 `handle`，或直接填死 `channel_id`
- 不想再监控某频道：把该条目 `enabled` 改为 `false`
- 日报整合在飞书侧完成，本仓库只负责采集与落盘
