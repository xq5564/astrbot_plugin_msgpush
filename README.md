# astrbot_plugin_msgpush - 消息推送桥接

将**其他平台 / 外部系统的消息**实时推送到 **AstrBot 接入的指定会话**（QQ 群、QQ 私聊、微信、Telegram、Discord 等）。

原理：插件在 AstrBot 内启动一个带 Token 鉴权的 HTTP API 服务，外部任何能发 HTTP 请求的东西（脚本、网页、监控告警、Telegram Bot 转发程序、手机快捷指令等）调用接口即可让 AstrBot 把消息发到指定会话。

## 功能

- **两种互斥工作模式**：
  - **原始模式（raw）**：外部平台的原文直接转发到目标会话
  - **解析模式（parse）**：原文交给目标会话的 **AI 上下文**解析（带会话历史与人格），把 AI 的结果发出，并写回会话历史保持上下文连续
- 推送消息统一格式：`[来源平台]推送\n内容`，来源平台由接口的 `source` 字段指定
- HTTP 推送接口，Token 鉴权（支持 Header / X-Token / URL 参数三种传递方式）
- **别名绑定**：在目标会话里执行一次指令，之后外部推送只写别名，无需关心内部会话 ID
- 支持推送**纯文本**、**文本 + 图片**（原始模式图片自动下载转发；解析模式图片交给多模态 LLM 理解）
- 绑定持久化保存（AstrBot 重启不丢失）
- WebUI 配置面板：模式、端口、Token、来源标签、解析提示词、图片大小上限
- 内置指令：绑定 / 解绑 / 列表 / 模式切换 / 测试

## 两种模式（互斥，全局二选一）

| | 原始模式 raw | 解析模式 parse |
|---|---|---|
| 行为 | `[Telegram]推送\n原文内容` 直接发到会话 | 原文进入目标会话的 LLM 上下文，**AI 解析后把结果发出** |
| 会话里看到 | 推送原文 | AI 的分析/回应 |
| 依赖 | 无 | 需在 AstrBot 中配置 LLM 提供商 |

切换方式（任选其一）：

- 会话内指令：`/推送模式` 查看，`/推送模式 原始` 或 `/推送模式 解析` 切换
- WebUI → 插件配置 → `mode`（`raw` / `parse` 下拉单选）

解析模式说明：

- 会自动加载目标会话的**对话历史**与**人格 system_prompt**，推送原文作为最新一条用户消息参与解析
- AI 的回复会尽量写回该会话的对话历史，后续群友继续和 Bot 聊天时它"记得"这条推送
- 解析行为可通过 WebUI 配置里的 `parse_prompt`（附加提示词）自定义
- 若目标会话无历史 / 加载失败，会自动降级为无上下文单次解析，不影响发送

## 安装

方式一：手动安装

```
将 astrbot_plugin_msgpush 整个文件夹放入 AstrBot 的 data/plugins/ 目录，重启 AstrBot 或在 WebUI 中重载插件。
```

方式二：WebUI → 插件市场 → 从 URL 安装（若你已将本插件上传到 GitHub 仓库）。

依赖 `astrbot >= 3.5`（已在使用 `astrbot.api` 新接口），`aiohttp` 会在安装时自动 pip。

## 使用步骤

### 第 1 步：绑定目标会话

在你想接收推送消息的会话（例如某个 QQ 群）里发送：

```
/绑定推送 群1
```

> 别名自定义，不要带空格。同一个别名重复绑定会覆盖（即换绑）。

相关指令：

| 指令 | 说明 |
|------|------|
| `/绑定推送 <别名>` | 将当前会话绑定为推送目标 |
| `/解绑推送 <别名>` | 删除某个绑定 |
| `/推送列表` | 查看所有绑定目标 |
| `/推送模式 [原始\|解析]` | 查看或切换推送处理模式 |
| `/推送测试 <别名> <消息>` | 按当前模式向目标发一条测试消息 |

### 第 2 步：获取 Token 与端口

在 WebUI → 插件 → 消息推送桥接 → 配置中查看：

- `port`：HTTP 服务端口（默认 6199）
- `token`：鉴权 Token（留空时插件会自动生成随机 Token）

### 第 3 步：外部推送

```bash
curl -X POST "http://<AstrBot主机IP>:6199/push/send" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"target": "群1", "source": "Telegram", "content": "今晚开会"}'
```

`source` 为来源平台名（会话里将显示为 `[Telegram]推送`），不传则使用配置里的默认来源标签。

带图片：

```bash
curl -X POST "http://<AstrBot主机IP>:6199/push/send" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "target": "群1",
    "content": "看这张图",
    "images": ["https://example.com/pic.jpg"]
  }'
```

## API 一览

所有接口均需鉴权（`Authorization: Bearer <token>`、`X-Token: <token>` 或 `?token=<token>`）。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/push/sessions` | 列出已绑定目标 + AstrBot 内部已知会话（含 umo） |
| POST | `/push/send` | 推送消息。Body: `{"target", "content", "source", "images"}` |
| POST | `/push/bind` | 远程绑定。Body: `{"alias", "umo"}` |
| POST | `/push/unbind` | 远程解绑。Body: `{"alias"}` |
| GET | `/` | 健康检查 / 接口列表 |

`target` 说明：填**别名**，或直接填 `unified_msg_origin`（形如 `aiocqhttp:GroupMessage:123456`，可从 `/push/sessions` 获取）。

返回示例：

```json
{"ok": true, "mode": "raw", "target": "群1", "umo": "aiocqhttp:GroupMessage:123456"}
```

解析模式下返回中额外包含 `result` 字段（AI 的回复文本）。

Python 示例（其他平台转发脚本常用）：

```python
import requests

def push_to_astrbot(content: str, target: str = "群1", source: str = "Telegram", images: list = None):
    return requests.post(
        "http://127.0.0.1:6199/push/send",
        headers={"Authorization": "Bearer <token>"},
        json={"target": target, "source": source, "content": content, "images": images or []},
        timeout=60,
    ).json()

resp = push_to_astrbot("今晚开会")
print(resp.get("result", ""))  # 解析模式下为 AI 的回复
```

## 典型场景

- **Telegram → QQ 群互通**：写个小脚本监听 Telegram Bot 消息，收到即调 `/push/send` 推到 QQ 群
- **服务器监控告警**：crontab / Prometheus Alertmanager webhook 推送告警到群
- **网站表单通知**：表单提交后推送到私聊

## 注意事项

- **解析模式**依赖 AstrBot 已配置 LLM 提供商；未配置时该模式的推送会返回错误
- 部分平台适配器对**主动消息**有限制（如 QQ 官方机器人接口的主动推送频控），个人 QQ（aiocqhttp/NapCat 等）一般无限制
- Token 请妥善保管；若服务只供本机脚本调用，建议配置里把 `host` 改为 `127.0.0.1`
- 插件数据（绑定关系）保存在 `data/msg_push/bindings.json`

## License

MIT
