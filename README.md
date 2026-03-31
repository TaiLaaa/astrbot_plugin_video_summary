# 🎬 AstrBot 视频链接总结插件

让 bot 识别常见视频链接，并自动生成 AI 总结，适合群聊 / 私聊里的“发链接就解析”场景。

## ✨ 功能

### 1. 视频链接总结
- 支持通过 `/视频总结 <视频链接>` 直接触发
- 支持识别消息中的视频链接并进行总结
- 适合快速了解视频内容、亮点和主题

### 2. 多种触发方式
- 群聊中 **@bot + 视频链接** 可触发
- 群聊中 **@bot + 自然语言 + 视频链接** 可触发
- 私聊中直接发送视频链接可触发
- 回复一条带视频链接的消息，也可提取链接进行总结

### 3. 双模式总结
- **普通模式**：更快、更省、输出更短
- **完整模式**：更完整、更稳、细节更多

### 4. 兼容 AstrBot 人格
- 复用 AstrBot 当前人格模板输出
- 总结内容会更贴近 bot 当前说话风格

## 📦 安装

在 AstrBot 中安装插件，填入仓库地址：

```
https://github.com/TaiLaaa/astrbot_plugin_video_summary
```

> ⚠️ **安装后请重启一次 AstrBot**，确保插件和配置项正确加载。
>
> 插件配置中的供应商和人格选项会由 AstrBot 根据当前环境自动加载。

## 🖼️ T2I / Playwright 说明

如果你需要使用 **T2I 输出**，就需要额外安装 Playwright 及 Chromium，不然相关渲染能力无法使用。

安装命令：

```bash
pip install playwright
playwright install chromium
```

如果你是 **Docker / Debian / Ubuntu** 环境，除了上面的 Python 依赖，还需要补齐 Chromium 的系统运行库，否则会出现这类报错：

```text
error while loading shared libraries: libnspr4.so: cannot open shared object file
```

建议额外安装：

```bash
apt update
apt install -y \
  libnspr4 libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
  libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 \
  libpangocairo-1.0-0 libpango-1.0-0 libcairo2 libxkbcommon0 libgtk-3-0
```

`ffmpeg` 也需要安装，不然插件无法正常处理视频内容，也就无法完成视频理解与总结。

安装命令（Ubuntu / Debian）：

```bash
apt update
apt install -y ffmpeg
```

> 如果你的 AstrBot 使用虚拟环境，请在对应环境中执行 Playwright 安装命令。
>
> 如果 `ffmpeg` 不可用，视频下载后的抽帧与后续处理可能失败。
>
> 如果 T2I 日志里出现 `libnspr4.so` / `libnss3.so` / `BrowserType.launch` 相关报错，优先检查系统依赖是否完整。

## ⚙️ 配置说明

安装后在 AstrBot 的插件配置页面中设置：

| 配置项 | 说明 |
|--------|------|
| **总结模式** | 选择普通模式或完整模式 |
| **普通模式 provider** | 普通模式使用的模型供应商 ID |
| **完整模式 provider** | 完整模式使用的模型供应商 ID |
| **普通模式提示词** | 普通模式附加提示词，可留空 |
| **完整模式提示词** | 完整模式附加提示词，可留空 |
| **ffmpeg 路径** | ffmpeg 可执行文件路径，默认 `/usr/bin/ffmpeg` |
| **T2I 输出** | 是否使用更适合渲染/截图的分段排版 |

> 💡 如果你的模型支持视觉输入，插件会优先走关键帧理解；如果不支持，会自动降级为保守总结。

## 🔗 当前支持链接

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
- 视频直链：`.mp4` `.m3u8` `.mov` `.mkv` `.webm`

## 💬 触发方式

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

### 3. 解析后的后续追问

插件会暂存最近一次视频解析上下文，用于接住同一话题下的短追问。

- 默认上下文保留 **10 分钟**
- **超过 10 分钟无回应**，不再继续把后续聊天当成这条视频的解析追问
- 超时后如需继续解析同一个视频，请重新发送视频链接或重新触发 `/视频总结`

## 🔧 工作原理

```text
视频链接 ──→ 链接识别
            │
            ├── 提取视频元信息
            ├── 下载视频
            ├── 抽取关键帧
            ├── 调用模型理解内容
            └── 按人格风格输出最终总结
```

- **普通模式**：关键帧较少，优先快速理解
- **完整模式**：关键帧更多，先客观理解再做风格化输出
- **自动降级**：模型不支持视觉时，会回退为保守解析

## ⚠️ 注意事项

- 不是所有模型都真的支持“看视频”
- 视觉能力弱或不支持视觉的模型，会走降级逻辑
- 视频平台能否正常下载，会直接影响总结效果
- ffmpeg / 浏览器环境异常时，关键帧链路可能失败
- 如果启用 T2I 输出但未安装 Playwright，相关渲染能力将不可用

## 📝 使用建议

- 日常快速看内容：用 **普通模式**
- 想看更完整的内容提炼：用 **完整模式**
- 如果模型支持视觉，效果通常会更好
- 建议把指令触发 `/视频总结` 作为保底入口保留

## 📁 文件结构

```text
astrbot_plugin_video_summary/
├── main.py
├── metadata.yaml
├── _conf_schema.json
└── README.md
```

## 🏷️ 插件信息

- 插件名：`astrbot_plugin_video_summary`
- 显示名：`视频链接总结`

## 🚀 仓库

GitHub：<https://github.com/TaiLaaa/astrbot_plugin_video_summary>
