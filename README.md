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

## 依赖环境

建议环境：

- AstrBot
- Python 3.12+
- `ffmpeg`
- Playwright Chromium
- `yt-dlp`

安装示例：

```bash
pip install yt-dlp playwright
```

如果需要浏览器运行环境：

```bash
playwright install chromium
```

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
