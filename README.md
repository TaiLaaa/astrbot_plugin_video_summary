# astrbot_plugin_video_summary

AstrBot 视频链接总结插件。

支持从常见视频链接中提取内容并生成 AI 总结，适合群聊/私聊场景下的“发链接即解析”能力。

## 功能特性

- 支持指令触发：`/视频总结 <视频链接>`
- 支持自然语言 + 链接触发
- 支持群聊 @bot 触发、私聊直接触发、回复消息取链接
- 支持普通模式 / 完整模式
- 可复用 AstrBot 当前人格模板进行输出
- 支持视觉模型关键帧理解，不支持视觉时自动降级保守总结
- 内置 Playwright / yt-dlp 视频处理链路

## 当前支持链接

已内置基础识别：

- Bilibili：`bilibili.com` / `b23.tv`
- 抖音：`douyin.com` / `iesdouyin.com` / `v.douyin.com`
- 小红书：`xiaohongshu.com` / `xhslink.com`
- 快手：`kuaishou.com`
- 微博视频：`weibo.com` / `weibo.cn` / `video.weibo.com`
- YouTube：`youtube.com` / `youtu.be`
- 西瓜视频：`ixigua.com`
- 百度好看：`haokan.baidu.com`
- QQ 视频页：`qq.com/x/page/`
- 常见视频直链：`.mp4` `.m3u8` `.mov` `.mkv` `.webm`

## 安装说明

## 1. 环境要求

建议先确认以下环境可用：

- AstrBot
- Python 3.12+
- `ffmpeg`
- Playwright Chromium
- `yt-dlp`

Ubuntu/Debian 可先安装系统依赖：

```bash
apt update
apt install -y ffmpeg python3 python3-pip
```

## 2. 安装 Python 依赖

如果你的 AstrBot 环境还没有这些包，执行：

```bash
pip install yt-dlp playwright
```

安装 Playwright 浏览器：

```bash
playwright install chromium
```

如果你的 AstrBot 使用虚拟环境，请在对应 venv 里执行以上命令。

## 3. 部署插件

进入 AstrBot 插件目录：

```bash
cd /root/astrbot/data/plugins
```

拉取仓库：

```bash
git clone https://github.com/TaiLaaa/astrbot_plugin_video_summary.git
```

最终目录应类似：

```text
/root/astrbot/data/plugins/astrbot_plugin_video_summary
```

## 4. 补充依赖说明

本仓库默认**不提交 `vendor/` 目录**，避免 GitHub 推送时触发密钥扫描拦截。

因此运行环境需要满足下面二选一：

### 方案 A：直接安装到 Python 环境（推荐）

```bash
pip install yt-dlp
```

### 方案 B：手动放入 `vendor/`

如果你坚持离线/vendor 方案，也可以自行把 `yt_dlp` 放到插件目录下的 `vendor/` 中。

例如：

```text
astrbot_plugin_video_summary/
└── vendor/
    └── yt_dlp/
```

推荐优先使用 **方案 A**，更简单。

## 5. 配置插件

将插件放入目录后，在 AstrBot 管理界面中加载/启用插件。

然后按你的模型环境配置以下内容：

- 普通模式 provider
- 完整模式 provider
- `ffmpeg_bin` 路径
- 自定义提示词（如需要）

如果服务器上的 ffmpeg 不在默认位置，可手动指定：

```text
/usr/bin/ffmpeg
```

## 6. 重启或重载 AstrBot

完成安装后，重启 AstrBot，或在管理界面重载插件。

如果你是 Docker 部署，通常可直接重启 AstrBot 容器。

## 7. 验证安装

安装完成后，可直接发送：

```text
/视频总结 https://www.bilibili.com/video/BV1xx411c7mD
```

如果能正常返回总结结果，说明插件已成功加载。

如未生效，优先检查：

- 插件目录名是否正确
- 依赖是否安装成功
- `ffmpeg` 是否可执行
- Playwright Chromium 是否已安装
- 当前 provider 是否可用

## 触发方式

### 1. 指令触发

```text
/视频总结 <视频链接>
```

示例：

```text
/视频总结 https://www.bilibili.com/video/BV1xx411c7mD
```

### 2. 自然触发

典型场景：

```text
@bot 看下这个视频 https://...
@bot 总结一下这个链接 https://...
```

也支持：

- 私聊直接发送视频链接
- 回复一条带视频链接的消息并要求总结

默认不会在群聊里对“单独发一个链接且未 @bot”的消息误触发。

## 模式说明

### 普通模式

适合快速理解、低成本、较短输出：

- 关键帧数量较少
- 总结更简洁
- 速度更快
- 失败时自动回退到保守解析

### 完整模式

适合追求更完整、更稳定的总结：

- 关键帧数量更多
- 先客观理解，再人格化输出
- 输出更完整
- 耗时和成本更高

## 文件结构

```text
astrbot_plugin_video_summary/
├── main.py
├── metadata.yaml
├── _conf_schema.json
├── README.md
└── vendor/   # 本地可选，不纳入仓库
```

## 元信息

当前插件名：`astrbot_plugin_video_summary`

显示名：`视频链接总结`

## 说明

该插件当前走的是“下载视频 / 提取元信息 / 抽关键帧 / 模型总结”的实现路线，
不是将整段视频直接作为原生输入喂给模型。

因此实际效果会受以下因素影响：

- 模型是否支持视觉理解
- 视频平台可否成功下载
- 关键帧提取是否成功
- 服务器上的 Playwright / ffmpeg 环境是否完整

## 仓库

GitHub：<https://github.com/TaiLaaa/astrbot_plugin_video_summary>
