# astrbot_plugin_emoji_harvester

按 **QQ 号锁定** 采集指定用户在群聊 / 私聊中发出的**表情包**与**图片**，提供可视化的**图库界面**（AstrBot Plugin View），并支持**用 AI 分析图片内容、自动挑表情包发送**。

---

## ✨ 功能

| 能力 | 说明 |
| --- | --- |
| 🎯 按 QQ 号锁定 | 只有配置名单里的用户发图才会被采集，配置项 `target_qq` |
| 🖼️ 三种来源 | `image` 普通图片 / `face` QQ 系统表情 / `mface` QQ 商城表情包（动画表情） |
| 🤖 视觉语义标注 | 后台用视觉模型给每张图打「情绪 / 场景 / 描述 / 关键词」标签 |
| 🧠 智能挑图 | LLM 根据聊天情境从图库里挑最贴��的那张 |
| 📣 自动发表情包 | AI 判断此刻该不该发表情包（工作汇报会主动拒绝），合适就发 |
| 🛠️ LLM 工具 | 注册 `send_emoji` 工具给主对话模型，让它自己决定发不发 |
| 🧹 内容去重 | 按 SHA-256 去重，同一张图不会重复占空间 |
| 🖥️ 可视化图库 | 插件自带 Web View，浏览、搜索、筛选、删除、看 AI 标签 |
| 🔍 语义检索 | 按情绪 / 场景 / 描述 / 关键词搜索，中文按 2-gram 匹配 |
| 💬 聊天指令 | `/图库`、`/图库 搜索 关键词`、`/图库 清空`、`/发表情包`、`/图库状态` 等 |
| 🔒 路径安全 | 素材访问路径严格校验，杜绝路径穿越 |
| 📦 零第三方依赖 | 纯标准库实现，解析 PNG/GIF/JPEG/WEBP 尺寸不依赖 Pillow |

---

## 📦 安装

### 方式一：从仓库 URL 安装（推荐）

在 AstrBot WebUI → **插件管理** → **插件商店 / 从 URL 安装**，填入：

```
https://github.com/qweQ-1/astrbot_plugin_emoji_harvester
```

### 方式二：手动安装

```bash
cd AstrBot/data/plugins
git clone https://github.com/qweQ-1/astrbot_plugin_emoji_harvester.git
```

然后在 WebUI 里重载插件。

---

## ⚙️ 配置

插件安装后在 **插件配置页** 可设置：

### 采集相关

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `target_qq` | list | `[]` | **要采集的 QQ 号列表**（按 QQ 号锁定） |
| `collect_enabled` | bool | `true` | 采集总开关 |
| `collect_face` | bool | `true` | 采集 QQ 系统表情 |
| `collect_mface` | bool | `true` | 采集 QQ 商城表情包 |
| `only_in_group` | bool | `false` | 只采集群聊（关闭则私聊也采） |
| `max_file_mb` | int | `10` | 单张图片大小上限（MB） |
| `notify_on_collect` | bool | `false` | 每次采集后在群里提示 |
| `download_timeout` | int | `15` | 下载表情图片超时（秒） |

### AI 相关

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `ai_enabled` | bool | `false` | **AI 总开关**（视觉分析 + 智能发表情包） |
| `auto_send_enabled` | bool | `false` | 自动模式：AI 自己判断要不要发表情包 |
| `tool_send_enabled` | bool | `true` | 工具模式：把 `send_emoji` 工具交给对话模型 |
| `vision_model` | string | `""` | 视觉模型名（需支持看图），如 `buddy/glm-5v-turbo` |
| `vision_api_base` | string | `""` | 视觉模型 API 地址（OpenAI 兼容） |
| `vision_api_key` | string | `""` | 视觉模型 API Key |
| `provider_source_id` | string | `""` | 复用 AstrBot 已有的 provider source id |
| `vision_batch_size` | int | `5` | 每轮分析的图片数 |
| `vision_interval` | int | `60` | 后台分析轮询间隔（秒） |
| `vision_timeout` | int | `60` | 调用视觉模型超时（秒） |
| `auto_send_cooldown` | int | `30` | 自动发表情包冷却（秒） |
| `auto_send_max_per_hour` | int | `10` | 每小时最多自动发送多少张 |

**API 凭据二选一**：

1. 直接填 `vision_api_base` + `vision_api_key`（最简单）
2. 填 `provider_source_id`（填 AstrBot 里 provider source 的 id，如 `虎支反代`），
   插件会沿用它的 `api_base` 和 `key`

> 主对话模型（如 DeepSeek）通常**没有视觉能力**，所以视觉分析要用单独的模型。

---

## 🤖 AI 能力说明

### 1. 视觉语义标注（后台自动）

插件启动后会起一个后台任务，每隔 `vision_interval` 秒处理一批新采集的图，
用视觉模型输出结构化标签：

```json
{
  "emotion": "mocking",
  "scene": "嘲讽吐槽",
  "caption": "装嫩卖萌，求轻喷",
  "keywords": ["宝宝", "奶嘴", "嘲讽"]
}
```

标签存在 SQLite 里，图库界面能看到，语义检索也靠它。

> QQ 系统表情只有 20~24 像素，视觉模型会拒绝（`image_invalid`），
> 插件会把它们自动标成 `system` 而不是反复重试。

### 2. 智能挑图（`send_emoji` 工具）

启用 `ai_enabled` 后，插件会注册一个 LLM 工具：

```
send_emoji(emotion: string, context: string)
```

主对话模型可以在合适的时机调用它，比如：

> 用户：今天摸鱼被老板抓到了
> 机器人：（调用 send_emoji，emotion="surprised"）→ 自动发一张震惊的表情包

### 3. 自动发表情包

启用 `auto_send_enabled` 后，插件会在每条消息到达时自己判断：

- 情绪强烈的反应（大笑、震惊、无语、感动、祝贺、嘲讽）→ 发表情包
- 严肃话题、工作汇报、技术讨论 → **不发表情包**

实测效果：

| 消息 | 判断 |
| --- | --- |
| 「哈哈哈这个太搞笑了」 | ✅ 发，选了软萌图 |
| 「无语。。。」 | ✅ 发，选了嘲讽图 |
| 「今天的会议纪要我发到邮箱了」 | ❌ 不发（"属于工作汇报和信息传递，无需表情包"） |

带限流保护：冷却时间 + 每小时上限，避免刷屏。

---

## 🖥️ 图库界面

打开 AstrBot WebUI → **插件管理** → 找到「表情包图库」插件 → 进入 **图库** 标签页。

界面提供：

- 顶部统计卡（素材总数 / 动画表情数 / 占用空间 / 监听中的 QQ）
- 搜索框（文件名、发送者、哈希）
- 筛选条（来源、排序、发送者、**情绪**、动静态）
- 网格卡片：显示 AI 标注的**情绪 / 场景标签**和**一句话描述**
- 点开是大图预览（←/→ 切换、Esc 关闭），底部显示 AI 标签和关键词
- 悬停出现删除按钮，支持单条删除与一键清空
- 深色模式自动跟随 AstrBot 主题

---

## 💬 指令

| 指令 | 说明 |
| --- | --- |
| `/图库` | 查看统计与最近收录 |
| `/图库 搜索 关键词` | 搜索素材 |
| `/图库 清空` | 清空图库（不可恢复） |
| `/发表情包` | 让 AI 根据当前消息挑一张 |
| `/发表情包 开心 分享了好消息` | 指定情绪和情境 |
| `/图库状态` | 查看图库与 AI 标注状态 |
| `/添加图库目标 <QQ>` | 加入采集名单 |
| `/移除图库目标 <QQ>` | 移出采集名单 |

---

## 📂 数据存放

```
AstrBot/data/plugin_data/astrbot_plugin_emoji_harvester/
├── assets.db          # SQLite 索引（含情绪 / 场景 / 描述 / 关键词）
├── manifest.json      # 导出清单（调用导出 API 后生成）
└── media/
    └── ab/
        └── ab12...cd.jpg   # 素材本体，按 sha256 前两位分目录
```

插件重装 / 升级不会丢数据（放在 `plugin_data` 目录）。
老版本数据库会自动补上视觉标注相关的列。

---

## 🧪 本地跑测试

```bash
python3 -m unittest discover -s tests -v
```

**86 个测试**，只依赖标准库，不需要 AstrBot 运行时，也不需要网络。

---

## 📝 实现说明

### 为什么要读 `raw_message`

AstrBot 的 `aiocqhttp` 适配器在解析 OneBot 消息时会**直接跳过 `mface` 段**（QQ 商城表情包），
所以光看 `event.get_messages()` 是拿不到商城表情的。插件因此额外读取
`event.message_obj.raw_message`（保留了完整的原始 OneBot 事件），
从中取出 `face` / `mface` 段再单独处理。

### 表情图地址

- 系统表情 `face`：`https://qzonestyle.gtimg.cn/qzone/em/e{id}.gif`（id 是纯数字）
- 商城表情包 `mface`：`https://p.qpic.cn/qq_expression/{uin}/{uin}_0_0_0_{emoji_id}_0_0/0`
  - `emoji_id` 必须是 **32 位十六进制**（QQ 协议强制校验）
  - 路径里的 `uin` 必须与表情归属的账号一致，换 uin 会 404
  - NapCat 实现会直接给 `url` 字段，优先用它

### 图片类型判定

QQ 表情包的地址后缀**不可靠**（`p.qpic.cn` 的 `/0` 结尾实际返回的是 JPEG），
所以存储层按**内容魔数**判定 MIME，落盘后缀跟着真实内容走。

### 为什么视觉分析要单独用客户端

不走 AstrBot 的 provider 管理器：
- 视觉分析要用**特定**的视觉模型（主对话模型可能没有视觉能力）
- 插件要在后台批量跑分析，用 provider 会污染对话上下文

### 语义检索

没有引入分词器和向量库，用加权 LIKE 打分：
情绪 ×3.0、场景 ×2.5、描述 ×2.0、关键词 ×2.0、文件名 ×0.5。
中文按 2-gram 切，英文按空格切。

---

## 📄 License

MIT
