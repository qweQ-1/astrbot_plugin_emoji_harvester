# astrbot_plugin_emoji_harvester

按 **QQ 号锁定** 采集指定用户在群聊 / 私聊中发出的**表情包**与**图片**，并提供一个可视化的**图库界面**（AstrBot Plugin View）。

---

## ✨ 功能

| 能力 | 说明 |
| --- | --- |
| 🎯 按 QQ 号锁定 | 只有配置名单里的用户发图才会被采集，配置项 `target_qq` |
| 🖼️ 三种来源 | `image` 普通图片 / `face` QQ 系统表情 / `mface` QQ 商城表情包（动画表情） |
| 🧹 内容去重 | 按 SHA-256 去重，同一张图不会重复占空间 |
| 🖥️ 可视化图库 | 插件自带 Web View，在 AstrBot WebUI 的插件页里浏览、搜索、筛选、删除 |
| 🔍 检索筛选 | 按文件名 / 发送者 / 哈希搜索，按来源、发送者、动静态筛选，四种排序 |
| 💬 聊天指令 | `/图库`、`/图库 搜索 关键词`、`/图库 清空`、`/添加图库目标`、`/移除图库目标` |
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

也可以直接在群里用指令增删名单：

```
/添加图库目标 123456789
/移除图库目标 123456789
```

---

## 🖥️ 图库界面

打开 AstrBot WebUI → **插件管理** → 找到「表情包图库」插件 → 进入 **图库** 标签页。

界面提供：

- 顶部统计卡（素材总数 / 动画表情数 / 占用空间 / 监听中的 QQ）
- 搜索框（文件名、发送者、哈希）
- 筛选条（来源、排序、发送者、仅动画 / 仅静态）
- 瀑布网格，点开是大图预览（支持左右键切换、Esc 关闭）
- 悬停出现删除按钮，支持单条删除与一键清空
- 深色模式自动跟随 AstrBot 主题

---

## 💬 指令

| 指令 | 说明 |
| --- | --- |
| `/图库` | 查看统计与最近收录 |
| `/图库 搜索 关键词` | 搜索素材 |
| `/图库 清空` | 清空图库（不可恢复） |
| `/添加图库目标 <QQ>` | 加入采集名单 |
| `/移除图库目标 <QQ>` | 移出采集名单 |

---

## 📂 数据存放

```
AstrBot/data/plugin_data/astrbot_plugin_emoji_harvester/
├── assets.db          # SQLite 索引
├── manifest.json      # 导出清单（调用导出 API 后生成）
└── media/
    └── ab/
        └── ab12...cd.gif   # 素材本体，按 sha256 前两位分目录
```

插件重装 / 升级不会丢数据（放在 `plugin_data` 目录）。

---

## 🧪 本地跑测试

```bash
python3 -m unittest discover -s tests -v
```

测试只依赖标准库，不需要 AstrBot 运行时。

---

## 📝 实现说明

### 为什么要读 `raw_message`

AstrBot 的 `aiocqhttp` 适配器在解析 OneBot 消息时会**直接跳过 `mface` 段**（QQ 商城表情包），
所以光看 `event.get_messages()` 是拿不到商城表情的。插件因此额外读取
`event.message_obj.raw_message`（保留了完整的原始 OneBot 事件），
从中取出 `face` / `mface` 段再单独处理。

### 表情图地址

- 系统表情 `face`：`https://qzonestyle.gtimg.cn/qzone/em/e{id}.gif`
- 商城表情 `mface`：优先用段里的 `url`；没有时用 `key` 拼 `https://qzone.style.qq.com/qzone/em/e{key}.gif`

### 图片尺寸解析

为了不引入 Pillow 依赖，`imageinfo.py` 手工解析 PNG / GIF / JPEG / BMP / WEBP 的头部字节获取宽高。
解析失败只会导致尺寸显示为 `0×0`，不影响采集本身。

---

## 📄 License

MIT
