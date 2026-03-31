import asyncio
import html
import importlib
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.message.components import Image
from astrbot.core.provider.provider import Provider

BASE_DIR = Path(__file__).resolve().parent
VENDOR_DIR = BASE_DIR / "vendor"
if str(VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(VENDOR_DIR))

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


class VideoSummaryPlugin(Star):
    _shared_dependency_bootstrap_lock: asyncio.Lock | None = None
    _shared_dependency_bootstrap_done = False

    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config or {}
        self.ffmpeg_bin = str(self.config.get("ffmpeg_bin", "/usr/bin/ffmpeg") or "/usr/bin/ffmpeg")
        self._browser = None
        self._recent_video_contexts: dict[str, dict[str, Any]] = {}
        self._yt_dlp_module = None
        self._async_playwright_factory = None
        self._dependency_bootstrap_error = ""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._ensure_runtime_dependencies())
        except RuntimeError:
            pass

    def _get_arg(self, message_str: str) -> str:
        if not message_str:
            return ""
        parts = str(message_str).strip().split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""

    def _mode(self) -> str:
        return str(self.config.get("summary_mode", "normal") or "normal")

    def _pick_provider_id(self) -> str:
        mode = self._mode()
        return str(self.config.get("full_provider_id", "") or "") if mode == "full" else str(self.config.get("normal_provider_id", "") or "")

    def _pick_prompt_suffix(self) -> str:
        mode = self._mode()
        return str(self.config.get("full_prompt", "") or "") if mode == "full" else str(self.config.get("normal_prompt", "") or "")

    def _use_t2i_output(self) -> bool:
        return bool(self.config.get("t2i_output", False))

    def _python_executable(self) -> str:
        return sys.executable or "python"

    def _fonts_dir(self) -> Path:
        return BASE_DIR / "assets" / "fonts"

    def _iter_bundled_font_files(self) -> list[Path]:
        fonts_dir = self._fonts_dir()
        if not fonts_dir.exists():
            return []
        preferred_names = [
            "loli.ttf",
            "Lolita-2.ttf",
            "萝莉体第二版.ttf",
            "萝莉体 第二版.ttf",
            "NotoSansSC-Regular.otf",
            "NotoSansSC-Regular.ttf",
            "SourceHanSansSC-Regular.otf",
            "SourceHanSansSC-Regular.ttf",
        ]
        seen = set()
        files: list[Path] = []
        for name in preferred_names:
            path = fonts_dir / name
            if path.exists() and path.is_file():
                files.append(path)
                seen.add(path.name.lower())
        for path in sorted(fonts_dir.iterdir()):
            if not path.is_file():
                continue
            if path.suffix.lower() not in {".ttf", ".otf"}:
                continue
            if path.name.lower() in seen:
                continue
            files.append(path)
            seen.add(path.name.lower())
        return files

    def _bundled_font_faces_css(self) -> str:
        rules = []
        for path in self._iter_bundled_font_files():
            family = path.stem.replace(" ", "")
            font_format = "opentype" if path.suffix.lower() == ".otf" else "truetype"
            rules.append(
                "@font-face {"
                f"font-family:'{family}';"
                f"src:url('file://{path.as_posix()}') format('{font_format}');"
                "font-display:swap;"
                "}"
            )
        return "\n".join(rules)

    def _preferred_font_stack(self) -> str:
        custom_families = [f'"{path.stem.replace(" ", "")}"' for path in self._iter_bundled_font_files()]
        fallback = ['"Heiti TC"', '"PingFang SC"', '"Microsoft YaHei"', '"Noto Sans SC"', 'sans-serif']
        return ",".join(custom_families + fallback)

    async def _run_subprocess(self, *args: str) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        return proc.returncode, (out or b"").decode("utf-8", errors="ignore")

    async def _ensure_python_package(self, import_name: str, package_name: str) -> tuple[bool, str]:
        try:
            return bool(importlib.import_module(import_name)), ""
        except Exception:
            pass
        code, output = await self._run_subprocess(self._python_executable(), "-m", "pip", "install", package_name)
        if code == 0:
            importlib.invalidate_caches()
            try:
                return bool(importlib.import_module(import_name)), ""
            except Exception as e:
                return False, str(e)
        return False, output.strip()[-500:]

    async def _ensure_apt_packages(self, packages: list[str]) -> tuple[bool, str]:
        if not packages:
            return True, ""
        if shutil.which("apt-get") is None:
            return False, "系统中不存在 apt-get，无法自动安装系统依赖"
        missing = []
        for pkg in packages:
            code, _ = await self._run_subprocess("dpkg", "-s", pkg)
            if code != 0:
                missing.append(pkg)
        if not missing:
            return True, ""
        code, output = await self._run_subprocess("apt-get", "update")
        if code != 0:
            return False, output.strip()[-500:]
        env = os.environ.copy()
        env.setdefault("DEBIAN_FRONTEND", "noninteractive")
        proc = await asyncio.create_subprocess_exec(
            "apt-get", "install", "-y", *missing,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        out, _ = await proc.communicate()
        text = (out or b"").decode("utf-8", errors="ignore")
        return proc.returncode == 0, text.strip()[-800:]

    async def _ensure_system_runtime_packages(self) -> tuple[bool, str]:
        return await self._ensure_apt_packages([
            "ffmpeg",
            "fonts-noto-cjk",
            "libnspr4",
            "libnss3",
            "libatk1.0-0",
            "libatk-bridge2.0-0",
            "libcups2",
            "libxcomposite1",
            "libxdamage1",
            "libxfixes3",
            "libxrandr2",
            "libgbm1",
            "libasound2",
            "libpangocairo-1.0-0",
            "libpango-1.0-0",
            "libcairo2",
            "libxkbcommon0",
            "libgtk-3-0",
        ])

    async def _ensure_playwright_browsers(self) -> tuple[bool, str]:
        code, output = await self._run_subprocess(self._python_executable(), "-m", "playwright", "install", "chromium")
        return code == 0, output.strip()[-500:]

    async def _ensure_runtime_dependencies(self):
        if self._yt_dlp_module and self._async_playwright_factory:
            return
        lock = self.__class__._shared_dependency_bootstrap_lock
        if lock is None:
            lock = asyncio.Lock()
            self.__class__._shared_dependency_bootstrap_lock = lock
        async with lock:
            if self.__class__._shared_dependency_bootstrap_done and self._yt_dlp_module and self._async_playwright_factory:
                return
            errors = []
            ok, detail = await self._ensure_python_package("yt_dlp", "yt-dlp")
            if ok:
                self._yt_dlp_module = importlib.import_module("yt_dlp")
            else:
                errors.append(f"yt-dlp 自动安装失败: {detail}")
            if self._use_t2i_output():
                sys_ok, sys_detail = await self._ensure_system_runtime_packages()
                if not sys_ok:
                    errors.append(f"系统依赖自动安装失败: {sys_detail}")
                ok, detail = await self._ensure_python_package("playwright.async_api", "playwright")
                if ok:
                    self._async_playwright_factory = importlib.import_module("playwright.async_api").async_playwright
                    browser_ok, browser_detail = await self._ensure_playwright_browsers()
                    if not browser_ok:
                        errors.append(f"Chromium 自动安装失败: {browser_detail}")
                else:
                    errors.append(f"playwright 自动安装失败: {detail}")
            else:
                try:
                    self._async_playwright_factory = importlib.import_module("playwright.async_api").async_playwright
                except Exception:
                    self._async_playwright_factory = None
            self._dependency_bootstrap_error = "；".join([e for e in errors if e])
            self.__class__._shared_dependency_bootstrap_done = True

    async def _get_yt_dlp(self):
        if not self._yt_dlp_module:
            await self._ensure_runtime_dependencies()
            if not self._yt_dlp_module:
                try:
                    self._yt_dlp_module = importlib.import_module("yt_dlp")
                except Exception:
                    pass
        if not self._yt_dlp_module:
            raise RuntimeError(self._dependency_bootstrap_error or "未能自动安装 yt-dlp")
        return self._yt_dlp_module

    async def _get_async_playwright(self):
        if not self._async_playwright_factory:
            await self._ensure_runtime_dependencies()
            if not self._async_playwright_factory:
                try:
                    self._async_playwright_factory = importlib.import_module("playwright.async_api").async_playwright
                except Exception:
                    pass
        if not self._async_playwright_factory:
            raise RuntimeError(self._dependency_bootstrap_error or "未能自动安装 playwright")
        return self._async_playwright_factory

    def _format_style_instruction(self) -> str:
        if self._use_t2i_output():
            if self._mode() == "normal":
                return (
                    "输出排版要求：使用简洁的 T2I 风格排版。"
                    "默认只输出 1 段短总结；"
                    "如果确实有必要，最多再补 1-2 段极短补充；"
                    "每段都要短，视觉上清爽，适合直接渲染或截图阅读；"
                    "不要列表，不要标题，不要把内容铺得很长。"
                )
            return (
                "输出排版要求：使用 T2I 风格排版。"
                "整体按 3-6 个自然段输出，段与段之间空一行；"
                "允许少量短行和轻量条目，但不要使用 markdown 标题、表格、代码块；"
                "每段尽量短，视觉上清爽，适合直接渲染或截图阅读；"
                "如果信息很多，优先拆成短段，不要挤成一大坨。"
            )
        if self._mode() == "normal":
            return (
                "输出排版要求：使用正常聊天排版。"
                "整体控制在 1-4 个自然段；"
                "每段保持简短，通常 3-4 句以内；"
                "不要默认列点，不要展开成长文；"
                "如果一句就能说清，可以只发 1 段。"
            )
        return (
            "输出排版要求：使用分段式正常聊天排版。"
            "完整模式首轮总结按 6-8 个自然段输出，段与段之间空一行；"
            "每段保持正常聊天可读性，不要写成列表或报告；"
            "先整体概括，再展开重点，最后补一句结论或看点；"
            "不要把内容挤成一整段。"
        )

    def _pick_provider(self):
        provider_id = self._pick_provider_id()
        provider = self.context.get_provider_by_id(provider_id) if provider_id else None
        return provider or self.context.get_using_provider()

    def _get_provider_hint(self, provider: Provider) -> str:
        bits = []
        for attr in ("id", "provider_id", "model", "model_name", "name"):
            try:
                value = getattr(provider, attr, None)
            except Exception:
                value = None
            if value:
                bits.append(str(value))
        try:
            meta = getattr(provider, "meta", None)
            if isinstance(meta, dict):
                for key in ("id", "model", "name"):
                    if meta.get(key):
                        bits.append(str(meta.get(key)))
        except Exception:
            pass
        return " ".join(bits).lower()

    def _cleanup_output(self, text: str) -> str:
        text = (text or "").strip()
        if not text:
            return ""
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"^\s*(视频总结完成|总结如下|以下是总结|以下是该视频的总结)[:：]?\s*", "", text, flags=re.I)
        text = re.sub(r"^\s{0,3}(#{1,6}|[-=*]{3,}|>{1,3})\s*", "", text, flags=re.M)
        text = re.sub(r"^\s*[*•·●▪◦]\s*", "", text, flags=re.M)
        text = re.sub(r"^\s*\d+[\.、]\s*", "", text, flags=re.M)
        text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
        text = re.sub(r"__(.*?)__", r"\1", text)
        text = re.sub(r"`([^`]+)`", r"\1", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        lines = []
        for line in text.split("\n"):
            stripped = line.strip()
            if stripped:
                lines.append(stripped)
            else:
                if not lines or lines[-1] != "":
                    lines.append("")
        text = "\n".join(lines).strip()
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _split_sentences(self, text: str) -> list[str]:
        text = self._cleanup_output(text)
        if not text:
            return []
        text = text.replace("\n\n", "\n")
        chunks = []
        for part in re.split(r"(?<=[。！？!?；;])\s*|\n+", text):
            part = part.strip()
            if part:
                chunks.append(part)
        return chunks

    def _post_format_output(self, text: str, max_paragraphs: Optional[int] = None) -> str:
        cleaned = self._cleanup_output(text)
        if not cleaned:
            return ""
        pieces = self._split_sentences(cleaned)
        if not pieces:
            return cleaned
        if self._mode() == "normal":
            if self._use_t2i_output():
                result = "\n\n".join(pieces[:2]).strip()
            else:
                blocks = []
                current = []
                for piece in pieces:
                    current.append(piece)
                    if len(current) >= 3:
                        blocks.append("".join(current))
                        current = []
                    if len(blocks) >= 4:
                        break
                if current and len(blocks) < 4:
                    blocks.append("".join(current))
                result = "\n\n".join(blocks[:4]).strip()
        elif self._use_t2i_output():
            blocks = []
            current = []
            current_len = 0
            for piece in pieces:
                limit = 22 if len(piece) <= 26 else 34
                if current and current_len + len(piece) > limit:
                    blocks.append("\n".join(current))
                    current = [piece]
                    current_len = len(piece)
                else:
                    current.append(piece)
                    current_len += len(piece)
            if current:
                blocks.append("\n".join(current))
            result = "\n\n".join(blocks).strip()
        else:
            blocks = []
            current = []
            for piece in pieces:
                current.append(piece)
                if len(current) >= 2:
                    blocks.append("".join(current))
                    current = []
            if current:
                blocks.append("".join(current))
            result = "\n\n".join(blocks).strip()

        if max_paragraphs is not None and result:
            paragraphs = [seg.strip() for seg in re.split(r"\n\s*\n+", result) if seg.strip()]
            result = "\n\n".join(paragraphs[:max(1, max_paragraphs)]).strip()
        return result

    async def _yield_segmented_text(self, event: AstrMessageEvent, text: str):
        cleaned = self._cleanup_output(text)
        if not cleaned:
            return
        segments = [seg.strip() for seg in re.split(r"\n\s*\n+", cleaned) if seg.strip()]
        if not segments:
            segments = [cleaned]
        for seg in segments:
            yield event.plain_result(seg)

    async def _ensure_browser(self):
        if self._browser and self._browser.is_connected():
            return self._browser
        async_playwright = await self._get_async_playwright()
        pw = await async_playwright().start()
        executable_path = None
        candidates = [
            "/root/astrbot/ms-playwright/chromium-1208/chrome-linux64/chrome",
            "/root/astrbot/ms-playwright/chromium_headless_shell-1208/chrome-headless-shell-linux64/chrome-headless-shell",
            shutil.which("chromium"),
            shutil.which("chromium-browser"),
            shutil.which("google-chrome"),
            shutil.which("chrome"),
            shutil.which("msedge"),
        ]
        for path in candidates:
            if path and os.path.exists(path):
                executable_path = path
                break
        self._browser = await pw.chromium.launch(
            headless=True,
            executable_path=executable_path,
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
        )
        return self._browser

    def _build_t2i_card_html(self, text: str, meta: dict[str, Any]) -> str:
        title = html.escape(str(meta.get("title", "视频总结") or "视频总结"))
        body = html.escape(text or "").replace("\n", "<br>")
        mode_label = "普通模式" if self._mode() == "normal" else "完整模式"
        font_faces_css = self._bundled_font_faces_css()
        font_stack = self._preferred_font_stack()
        return f'''<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
{font_faces_css}
body {{ margin:0; background:#f3f5f7; font-family:{font_stack}; }}
.card {{ width:720px; margin:0; padding:32px 34px 30px; box-sizing:border-box; background:linear-gradient(180deg,#ffffff 0%,#f8fafc 100%); color:#0f172a; }}
.badge {{ display:inline-block; padding:6px 12px; border-radius:999px; background:#e0f2fe; color:#0369a1; font-size:18px; font-weight:700; margin-bottom:18px; }}
.title {{ font-size:30px; line-height:1.35; font-weight:800; margin-bottom:18px; word-break:break-word; font-family:{font_stack}; }}
.body {{ font-size:24px; line-height:1.75; color:#1e293b; word-break:break-word; white-space:normal; font-family:{font_stack}; }}
.footer {{ margin-top:22px; font-size:16px; color:#64748b; }}
</style></head><body><div class="card"><div class="badge">{mode_label}</div><div class="title">{title}</div><div class="body">{body}</div><div class="footer">视频链接总结</div></div></body></html>'''

    async def _render_text_card(self, text: str, meta: dict[str, Any]) -> bytes:
        html_content = self._build_t2i_card_html(text, meta)
        browser = await self._ensure_browser()
        page = await browser.new_page(viewport={"width": 760, "height": 200})
        try:
            await page.set_content(html_content, wait_until="networkidle")
            card = page.locator(".card")
            png = await card.screenshot(type="png")
            return png
        finally:
            await page.close()

    def _sanitize_persona_prompt(self, text: str) -> str:
        if not text:
            return ""
        cleaned = str(text)
        cleaned = re.sub(r"&&[^\s&]+&", "", cleaned)
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()

    async def _load_persona_prompt(self, event: Optional[AstrMessageEvent] = None) -> tuple[str, str]:
        persona_mode = str(self.config.get("persona_mode", "none") or "none").strip()
        if persona_mode == "none":
            logger.info("[video_summary] 人格模式=none，跳过人格注入")
            return "", "未使用"

        target_persona = ""
        debug_origin = ""
        debug_cid = ""
        debug_persona_id = ""
        if persona_mode == "default":
            try:
                selected = getattr(getattr(self.context, "provider_manager", None), "selected_default_persona", None) or {}
                target_persona = str(selected.get("name", "") or "")
                debug_persona_id = target_persona
            except Exception:
                target_persona = ""
        elif persona_mode == "current" and event is not None:
            try:
                message_obj = getattr(event, "message_obj", None)
                unified_msg_origin = getattr(event, "unified_msg_origin", None) or getattr(message_obj, "unified_msg_origin", None)
                debug_origin = str(unified_msg_origin or "")
                if unified_msg_origin:
                    conv_mgr = getattr(self.context, "conversation_manager", None)
                    if conv_mgr:
                        curr_cid = await conv_mgr.get_curr_conversation_id(unified_msg_origin)
                        debug_cid = str(curr_cid or "")
                        conversation = await conv_mgr.get_conversation(unified_msg_origin, curr_cid)
                        if conversation and getattr(conversation, "persona_id", None):
                            debug_persona_id = str(conversation.persona_id or "")
                            target_persona = debug_persona_id
                logger.info(
                    f"[video_summary] 当前人格读取 mode=current origin={debug_origin!r} curr_cid={debug_cid!r} conversation_persona={debug_persona_id!r} target={target_persona!r}"
                )
            except Exception as e:
                logger.warning(f"[video_summary] 读取当前聊天人格失败: {e}")
                target_persona = ""
        elif persona_mode == "custom":
            target_persona = str(self.config.get("persona_id", "") or "").strip()
            debug_persona_id = target_persona

        if not target_persona:
            logger.info(f"[video_summary] 人格未命中 mode={persona_mode} origin={debug_origin!r} curr_cid={debug_cid!r} conversation_persona={debug_persona_id!r}")
            return "", "未使用"

        try:
            persona_mgr = getattr(self.context, "persona_manager", None)
            if persona_mgr and hasattr(persona_mgr, "get_persona"):
                persona = await persona_mgr.get_persona(target_persona)
                if persona and getattr(persona, "system_prompt", None):
                    logger.info(f"[video_summary] 人格注入成功 persona={target_persona!r} source='persona_manager'")
                    return self._sanitize_persona_prompt(str(persona.system_prompt)), target_persona
            personas = getattr(getattr(self.context, "provider_manager", None), "personas", None) or []
            for persona in personas:
                if isinstance(persona, dict) and persona.get("name") == target_persona:
                    logger.info(f"[video_summary] 人格注入成功 persona={target_persona!r} source='provider_manager.personas'")
                    return self._sanitize_persona_prompt(str(persona.get("prompt", "") or "")), target_persona
        except Exception as e:
            logger.warning(f"[video_summary] 读取人格模板失败: {e}")
        logger.info(f"[video_summary] 人格模板未找到 persona={target_persona!r}")
        return "", "未使用"

    async def _extract_video_meta(self, url: str) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        yt_dlp = await self._get_yt_dlp()

        def _run():
            with yt_dlp.YoutubeDL({"skip_download": True, "quiet": True, "no_warnings": True}) as ydl:
                data = ydl.extract_info(url, download=False)
                return {
                    "title": data.get("title", ""),
                    "uploader": data.get("uploader", ""),
                    "duration": data.get("duration", 0),
                    "description": data.get("description", ""),
                    "webpage_url": data.get("webpage_url", url),
                    "channel": data.get("channel", ""),
                    "upload_date": data.get("upload_date", ""),
                    "categories": data.get("categories", []) or [],
                    "tags": data.get("tags", []) or [],
                }

        return await loop.run_in_executor(None, _run)

    async def _download_video(self, url: str, workdir: str) -> str:
        loop = asyncio.get_running_loop()
        output_tpl = os.path.join(workdir, "video.%(ext)s")
        yt_dlp = await self._get_yt_dlp()

        def _run():
            opts = {
                "format": "mp4/bestvideo+bestaudio/best",
                "outtmpl": output_tpl,
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "paths": {"home": workdir},
                "merge_output_format": "mp4",
                "socket_timeout": int(self.config.get("download_socket_timeout", 30) or 30),
                "retries": int(self.config.get("download_retries", 3) or 3),
                "fragment_retries": int(self.config.get("download_fragment_retries", 3) or 3),
                "extractor_retries": int(self.config.get("download_extractor_retries", 2) or 2),
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                requested = info.get("requested_downloads") or []
                for item in requested:
                    fp = item.get("filepath")
                    if fp and os.path.exists(fp):
                        return fp
                for p in Path(workdir).iterdir():
                    if p.is_file() and p.name.startswith("video."):
                        return str(p)
                raise FileNotFoundError("未找到下载后的视频文件")

        return await loop.run_in_executor(None, _run)

    async def _download_video_with_retry(self, url: str, workdir: str) -> str:
        max_attempts = max(1, int(self.config.get("download_attempts", 3) or 3))
        retry_delay = max(1, int(self.config.get("download_retry_delay_seconds", 2) or 2))
        last_error = None
        for attempt in range(1, max_attempts + 1):
            try:
                logger.info(f"[video_summary] 下载视频 attempt={attempt}/{max_attempts} url={url}")
                return await self._download_video(url, workdir)
            except Exception as e:
                last_error = e
                logger.warning(f"[video_summary] 下载视频失败 attempt={attempt}/{max_attempts}: {e}")
                if attempt < max_attempts:
                    await asyncio.sleep(retry_delay * attempt)
        if last_error:
            raise last_error
        raise RuntimeError("下载视频失败")

    async def _render_persona_failure_text(self, provider: Provider, system_prompt: str, error_type: str, error_detail: str) -> str:
        detail = str(error_detail or "").strip()[:400]
        kind_map = {
            "download_timeout": "视频下载超时/网络卡住",
            "download_failed": "视频下载失败",
            "meta_failed": "视频元信息读取失败",
            "summary_failed": "视频处理或总结失败",
        }
        kind_desc = kind_map.get(error_type, "视频处理失败")
        prompt = (
            "请把这次失败原因改写成发给用户的一小段说明。"
            "要求：严格按当前人格说话；只写 1 段，1-3 句；自然、像群里聊天；"
            "明确表达这次没成功，不能假装已经看完；不要输出技术栈术语，不要写成报错单。\n\n"
            f"失败类型：{kind_desc}\n"
            f"底层错误：{detail}\n\n"
            "如果是下载超时/网络问题，就用人话表达成“这次视频没拉下来/网卡了/没看成”这一类意思；"
            "如果是别的处理失败，也要自然说清这次没处理成功。"
        )
        try:
            resp = await provider.text_chat(system_prompt=system_prompt, prompt=prompt)
            text = self._cleanup_output((getattr(resp, "completion_text", "") or "").strip())
            if text:
                return self._post_format_output(text)
        except Exception as e:
            logger.warning(f"[video_summary] 失败提示人格化生成失败: {e}")

        if error_type == "download_timeout":
            return "网有点卡，我这次没把视频拉下来，所以还没法正常看完。你稍后再发我试一次。"
        if error_type == "download_failed":
            return "这次视频没下载成功，我还没法按完整模式看。你可以稍后再试一次。"
        if error_type == "meta_failed":
            return "这个链接我刚刚没读取成功，所以现在还没法开始总结。你可以换个链接再试试。"
        return "我这次处理视频的时候掉链子了，还没成功看完并总结出来。你稍后再让我试一次。"

    async def _extract_keyframes(self, video_path: str, workdir: str, duration: int) -> list[str]:
        frame_count = int(self.config.get("full_frame_count", 8) or 8)
        frame_count = max(4, min(frame_count, 12))
        frames_dir = Path(workdir) / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)

        effective_duration = max(int(duration or 0), 1)
        start_sec = 1
        end_sec = max(1, effective_duration - 1)

        offsets: list[int] = [start_sec]
        middle_count = max(0, frame_count - 2)
        if middle_count > 0:
            span_start = min(max(2, start_sec + 1), effective_duration)
            span_end = max(span_start, min(end_sec - 1, effective_duration))
            if span_end <= span_start:
                middle_points = [span_start] * middle_count
            else:
                middle_points = []
                for i in range(middle_count):
                    sec = round(span_start + (i + 1) * (span_end - span_start) / (middle_count + 1))
                    middle_points.append(int(sec))
            offsets.extend(middle_points)
        offsets.append(end_sec)

        deduped: list[int] = []
        seen = set()
        for sec in offsets:
            sec = min(max(1, int(sec)), effective_duration)
            if sec not in seen:
                seen.add(sec)
                deduped.append(sec)

        while len(deduped) < frame_count:
            candidate = min(max(1, len(deduped) + 1), effective_duration)
            if candidate not in seen:
                seen.add(candidate)
                deduped.append(candidate)
            else:
                break

        logger.info(f"[video_summary] 抽帧秒数 offsets={deduped}")

        results: list[str] = []
        for idx, sec in enumerate(deduped, start=1):
            out = str(frames_dir / f"frame_{idx:02d}_{sec}s.jpg")
            cmd = [
                self.ffmpeg_bin,
                "-y",
                "-ss",
                str(sec),
                "-i",
                video_path,
                "-frames:v",
                "1",
                "-q:v",
                "2",
                out,
            ]
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            await proc.communicate()
            if proc.returncode == 0 and os.path.exists(out):
                results.append(out)
        if len(results) < 3:
            raise RuntimeError("抽取关键帧失败或有效帧过少")
        return results

    def _build_evidence_prompt(self, meta: dict[str, Any], url: str) -> str:
        desc = str(meta.get("description", "") or "")[:1500]
        return (
            f"请先只做证据提取，不要直接总结视频。\n\n"
            f"视频链接: {url}\n"
            f"标题: {meta.get('title', '')}\n"
            f"简介:\n{desc}\n\n"
            "任务：基于关键帧画面、可见字幕、版面文字、场景变化和转写内容，输出三类信息："
            "1）已确认：明确能从画面/字幕/转写直接确认的内容，6-12条；"
            "2）推测：只能弱推测的内容，0-4条；"
            "3）无法确认：当前材料无法确认的点，2-6条。"
            "要求：每条尽量短，并尽量说明依据来自画面、字幕、版面文字还是转写。"
            "禁止把推测混进已确认。不要人格化，不要聊天腔。"
        )

    def _build_objective_from_evidence_prompt(self, evidence_text: str) -> str:
        return (
            "下面是一份视频证据提取结果。请严格以“已确认”为主，“推测”只能低权重参考，“无法确认”必须保留边界。\n\n"
            f"证据提取结果:\n{evidence_text}\n\n"
            "请输出一份客观视频理解结果，结构为：整体大意、主要内容、关键细节/变化、值得关注的地方、适合谁看、信息边界。"
            "禁止新增证据里没有的事实。"
        )

    def _build_normal_prompt(self, meta: dict[str, Any], url: str) -> str:
        desc = str(meta.get("description", "") or "")[:2500]
        cats = ", ".join(meta.get("categories", [])[:10]) if meta.get("categories") else ""
        tags = ", ".join(meta.get("tags", [])[:20]) if meta.get("tags") else ""
        return (
            f"请基于以下链接元信息，做保守、自然的链接解读。\n\n"
            f"视频链接: {url}\n"
            f"标题: {meta.get('title', '')}\n"
            f"作者/UP主: {meta.get('uploader', '')}\n"
            f"频道: {meta.get('channel', '')}\n"
            f"时长(秒): {meta.get('duration', 0)}\n"
            f"上传日期: {meta.get('upload_date', '')}\n"
            f"分类: {cats}\n"
            f"标签: {tags}\n"
            f"简介:\n{desc}\n\n"
            "你没有看到视频正文，也没有字幕或转写。"
            "所以不要假装看过视频，只能根据标题、简介、标签这些外层信息，简洁说清它大概率在讲什么。"
            "不要写成完整内容总结，不要编具体情节、具体机制、具体结论。"
            "请只输出一到两段自然口语，不要 markdown，不要列表，不要系统抬头。"
            f"{self._format_style_instruction()}"
        )

    def _build_normal_vision_prompt(self, meta: dict[str, Any], url: str) -> str:
        desc = str(meta.get("description", "") or "")[:1200]
        return (
            f"请根据这几个关键帧，结合基础元信息，做一个轻量、保守的普通模式视频总结。\n\n"
            f"视频链接: {url}\n"
            f"标题: {meta.get('title', '')}\n"
            f"作者/UP主: {meta.get('uploader', '')}\n"
            f"频道: {meta.get('channel', '')}\n"
            f"时长(秒): {meta.get('duration', 0)}\n"
            f"简介:\n{desc}\n\n"
            "要求：只做快速看懂版，不做完整版。"
            "严格只输出 1 段短总结，默认不要补充第二段，不要分点；"
            "只保留最核心的信息，宁可短一点，也不要展开成长文；"
            "只能写关键帧画面、可见字幕、版面文字和元信息共同支持的内容；"
            "信息不足就保守一点，不要脑补，不要写成完整详细分析。"
            "不要 markdown，不要列表，不要系统抬头，不要写成长文。"
            f"{self._format_style_instruction()}"
        )

    def _build_vision_understanding_prompt(self, meta: dict[str, Any], url: str, provider_hint: str = "") -> str:
        desc = str(meta.get("description", "") or "")[:2000]
        is_deepseek = "deepseek" in provider_hint
        is_gemini = "gemini" in provider_hint or "gpt" in provider_hint
        prompt = (
            f"请根据这组从同一个视频中抽出的关键帧，结合元信息，先做客观视频理解。\n\n"
            f"视频链接: {url}\n"
            f"标题: {meta.get('title', '')}\n"
            f"作者/UP主: {meta.get('uploader', '')}\n"
            f"频道: {meta.get('channel', '')}\n"
            f"时长(秒): {meta.get('duration', 0)}\n"
            f"简介:\n{desc}\n\n"
            "任务：根据关键帧里能直接看到的画面、字幕、版面文字、场景变化，以及转写里能确认的内容，"
            "输出一份客观理解结果。"
            "请按下面结构组织："
            "1）整体大意（2-4句）；"
            "2）主要内容（4-8条，尽量覆盖视频前中后段）；"
            "3）关键细节/变化/结论（3-6条）；"
            "4）值得关注的地方或可能影响（2-4条）；"
            "5）适合谁看（1-2句）；"
            "6）信息边界：哪些地方是能确认的，哪些地方仅凭当前画面无法确认。"
            "要求：禁止把猜测写成事实；如果某一点只是从画面推测，要明确写“推测”；"
            "如果无法确认，就明确写“无法仅凭当前画面确认”。不要使用人格口吻，不要聊天腔，不要系统抬头。"
            f"{self._format_style_instruction()}"
        )
        if is_deepseek:
            prompt += (
                "你当前使用的是容易扩写推测的模型，因此必须额外保守："
                "每条内容都要尽量绑定到画面、字幕、版面文字或转写依据；"
                "宁可少写，也不要把未明确出现的细节、因果、评价包装成事实。"
            )
        if is_gemini:
            prompt += (
                "你当前使用的是容易写得偏短的模型，因此请不要只给一小段概括，"
                "而要尽量把上面六部分都覆盖到。"
            )
        return prompt

    def _de_ai_tail(self, text: str) -> str:
        text = self._cleanup_output(text)
        if not text:
            return ""
        text = re.sub(r"(^|\n\n)如果一句话总结，就是", r"\1说白了就是", text, flags=re.M)
        text = re.sub(r"(^|\n\n)(总的来说，|总体来说，)", r"\1看下来就是，", text, flags=re.M)
        text = re.sub(r"(^|\n\n)可以看出，", r"\1看下来，", text, flags=re.M)
        text = re.sub(r"(^|\n\n)以上就是.*$", "", text, flags=re.M)
        text = re.sub(r"(^|\n\n)该视频主要", r"\1这个视频主要", text, flags=re.M)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return text

    def _build_persona_rewrite_prompt(self, objective_summary: str, provider_hint: str = "") -> str:
        is_deepseek = "deepseek" in provider_hint
        return (
            "下面是一份已经尽量客观的视频理解结果。请在不改变事实边界的前提下，"
            "把它改写成最终发给用户的自然总结。\n\n"
            f"客观理解结果:\n{objective_summary}\n\n"
            "要求：明确使用当前人格的说话方式来表达，开场、过渡、评价、收尾都可以体现人格，"
            "但事实边界不能变；自然、顺口、像这个人格真的在群里看完视频后转述；先给一段较完整的整体概括，"
            "再按需要自然补充重点，内容要比普通模式明显更充实；"
            "可以覆盖：主要内容、关键变化、值得关注的点、适合谁看、简短价值判断。"
            "不要写成审计报告，不要使用 markdown 标题/列表，不要输出系统抬头，不要把内容压缩得只剩一小段。"
            "禁止出现这些 AI 总结腔收尾：‘如果一句话总结’‘总的来说’‘总体来说’‘可以看出’‘该视频主要’‘以上就是’。"
            "结尾必须像聊天收尾，像人格自然补一句判断、感受或重点，不要做正式归纳。"
            "不要写‘能确定的几个点是’‘根据画面可知’‘从字幕可见’这类证据提取腔。"
            "如果当前人格本身风格比较明显（如可爱、傲娇、嘴贫、活泼、黏人、吐槽感），可以正常体现出来，"
            "不要只剩一点轻微口语化。"
            f"{self._format_style_instruction()}"
            + (
                "另外，你当前使用的是 DeepSeek 系模型，改写时仍要保持事实边界，不要在润色阶段新增客观理解结果里没有的细节。"
                if is_deepseek
                else ""
            )
        )

    def _extract_context_highlights(self, text: str) -> list[str]:
        pieces = self._split_sentences(text)
        return pieces[:6]

    def _build_followup_objective_prompt(self, meta: dict[str, Any], final_text: str, url: str) -> str:
        desc = str(meta.get("description", "") or "")[:1200]
        return (
            "下面是一段已经发给用户的视频总结。请把它还原成更适合后续追问使用的客观底稿。\n\n"
            f"视频链接: {url}\n"
            f"标题: {meta.get('title', '')}\n"
            f"作者/UP主: {meta.get('uploader', '')}\n"
            f"频道: {meta.get('channel', '')}\n"
            f"简介: {desc}\n\n"
            f"已发送给用户的总结:\n{final_text}\n\n"
            "请输出一份后续追问底稿，要求包含：整体大意、核心信息点、能确认的事实、暂时无法确认的边界。"
            "要求：尽量客观，禁止新增没有依据的细节，语言自然但不要人格化。"
        )

    async def _build_opening_line(self, provider: Provider, system_prompt: str) -> str:
        mode_label = "完整模式" if self._mode() == "full" else "普通模式"
        prompt = (
            f"你现在要在群聊里先回一句很短的话，表示你准备开始看这个视频。当前是{mode_label}。"
            "要求：只输出一句中文；自然、口语、像真人顺手接一句；"
            "必须明确表达你现在要开始看这个视频或这个链接，核心意思不能丢；"
            "允许保留当前人格风格，包括轻微可爱、嘴贫、傲娇、活泼等语气，但不能偏题；"
            "不要只顾着演人格，必须让人一看就知道你是要开始看视频；"
            "不要解释流程，不要长篇铺垫，不要列表，不要加引号，长度尽量控制在8到20个字。"
        )
        try:
            resp = await provider.text_chat(system_prompt=system_prompt, prompt=prompt)
            text = self._cleanup_output((getattr(resp, "completion_text", "") or "").strip())
            text = re.sub(r"\s+", "", text)
            if text:
                logger.info(f"[video_summary] 开场白生成成功: {text[:40]}")
                return text[:24]
        except Exception as e:
            logger.warning(f"[video_summary] 生成开场白失败: {e}")
        fallback = "这就去看看这个视频讲了啥。" if self._mode() != "full" else "我先认真看看这个视频。"
        logger.info(f"[video_summary] 开场白回退: {fallback}")
        return fallback

    def _extract_urls_from_text(self, text: str) -> list[str]:
        if not text:
            return []
        urls: list[str] = []
        for match in URL_RE.finditer(text):
            url = match.group(0).rstrip(")）]}>.,!?，。；;\"'")
            if url and url not in urls:
                urls.append(url)
        return urls

    def _extract_all_candidate_urls(self, event: AstrMessageEvent) -> list[str]:
        urls: list[str] = []
        raw_text = str(getattr(event, "message_str", "") or "")
        for url in self._extract_urls_from_text(raw_text):
            if url not in urls:
                urls.append(url)
        try:
            message_obj = getattr(event, "message_obj", None)
            chain = getattr(message_obj, "message", None) or []
            for seg in chain:
                for attr in ("text", "url", "file", "content"):
                    value = getattr(seg, attr, None)
                    if isinstance(value, str):
                        for url in self._extract_urls_from_text(value):
                            if url not in urls:
                                urls.append(url)
                raw = getattr(seg, "raw", None) or getattr(seg, "data", None)
                if isinstance(raw, dict):
                    for value in raw.values():
                        if isinstance(value, str):
                            for url in self._extract_urls_from_text(value):
                                if url not in urls:
                                    urls.append(url)
        except Exception:
            pass
        return urls

    def _get_session_scope_key(self, event: AstrMessageEvent) -> str:
        message_obj = getattr(event, "message_obj", None)
        session_id = getattr(message_obj, "group_id", None) or getattr(message_obj, "session_id", None) or getattr(message_obj, "conversation_id", None)
        user_id = getattr(message_obj, "sender_id", None) or getattr(message_obj, "user_id", None)
        sid = str(session_id or "global")
        uid = str(user_id or "unknown")
        return f"{sid}:{uid}"

    def _context_ttl_seconds(self) -> int:
        return max(60, int(self.config.get("context_ttl_seconds", 600) or 600))

    def _context_max_entries(self) -> int:
        return max(1, min(5, int(self.config.get("context_max_entries", 1) or 1)))

    def _followup_max_paragraphs(self) -> int:
        return max(1, min(4, int(self.config.get("followup_max_paragraphs", 2) or 2)))

    def _cleanup_context_cache(self):
        now = time.time()
        ttl = self._context_ttl_seconds()
        expired = []
        for key, value in self._recent_video_contexts.items():
            items = value.get("items") or []
            kept = [item for item in items if now - float(item.get("ts", 0) or 0) <= ttl]
            if kept:
                value["items"] = kept[-self._context_max_entries():]
            else:
                expired.append(key)
        for key in expired:
            self._recent_video_contexts.pop(key, None)

    def _save_video_context(self, event: AstrMessageEvent, payload: dict[str, Any]):
        self._cleanup_context_cache()
        key = self._get_session_scope_key(event)
        bucket = self._recent_video_contexts.setdefault(key, {"items": []})
        item = dict(payload)
        item["ts"] = time.time()
        bucket["items"].append(item)
        bucket["items"] = bucket["items"][-self._context_max_entries():]

    def _get_latest_video_context(self, event: AstrMessageEvent) -> dict[str, Any] | None:
        self._cleanup_context_cache()
        key = self._get_session_scope_key(event)
        items = (self._recent_video_contexts.get(key) or {}).get("items") or []
        return items[-1] if items else None

    async def _is_reply_to_bot(self, event: AstrMessageEvent) -> bool:
        try:
            message_obj = getattr(event, "message_obj", None)
            chain = getattr(message_obj, "message", None) or []
            reply_seg = None
            for seg in chain:
                if seg.__class__.__name__ == "Reply":
                    reply_seg = seg
                    break
            if not reply_seg:
                return False
            original_msg = await event.bot.api.call_action("get_msg", message_id=reply_seg.id)
            if not original_msg:
                return False
            self_id = str(getattr(message_obj, "self_id", "") or "")
            sender = str(original_msg.get("sender", {}).get("user_id") or original_msg.get("user_id") or "")
            return bool(self_id and sender and sender == self_id)
        except Exception as e:
            logger.debug(f"[video_summary] 判断是否回复 bot 失败: {e}")
            return False

    def _looks_like_followup_question(self, text: str) -> bool:
        content = (text or "").strip()
        if not content:
            return False
        direct_patterns = (
            "你觉得", "你看", "怎么看", "怎么样", "咋样", "好不好", "靠谱吗", "值不值得",
            "什么意思", "为啥", "为什么", "是不是", "对不对", "真的假的", "能不能", "可不可以",
            "怎么做", "怎么弄", "怎么理解", "怎么评价", "重点是什么", "讲了什么", "说了什么",
            "那我", "那这", "那这个", "所以", "然后呢", "接下来呢"
        )
        if any(x in content for x in direct_patterns):
            return True
        if any(p in content for p in ("?", "？", "吗", "呢", "么")):
            return True
        return len(content) <= 24 and any(x in content for x in ("这个", "它", "这视频", "这条", "这链接"))

    def _build_followup_prompt(self, user_text: str, ctx: dict[str, Any]) -> str:
        meta = ctx.get("meta") or {}
        objective_summary = str(ctx.get("objective_summary", "") or "").strip()
        final_summary = str(ctx.get("final_summary", "") or ctx.get("summary", "") or "").strip()
        highlights = ctx.get("highlights") or []
        highlights_text = "\n".join(f"- {item}" for item in highlights[:6]) if highlights else "- 无"
        followup_max_paragraphs = self._followup_max_paragraphs()
        return (
            "你现在处在同一个视频话题的连续对话里。\n\n"
            "下面是这个视频之前已经解析出的上下文，请把它当作当前回复的事实基础：\n"
            f"视频链接: {ctx.get('url', '')}\n"
            f"标题: {meta.get('title', '')}\n"
            f"作者/UP主: {meta.get('uploader', '')}\n"
            f"频道: {meta.get('channel', '')}\n"
            f"客观解析底稿:\n{objective_summary or final_summary}\n\n"
            f"首轮发给用户的总结:\n{final_summary}\n\n"
            f"关键点:\n{highlights_text}\n\n"
            f"用户现在的新消息是：{user_text.strip()}\n\n"
            "要求：这是连续对话，不是新任务。默认用户说的‘这个/这视频/它/这一步/他说的’都与上面这个视频有关；"
            "你的第一目标是直接回答用户这一句正在问什么，不要绕回去重新总结整条视频；"
            "除非用户明确要求‘再总结一下’，否则不要输出一整段视频总结；"
            "回答时优先依据客观解析底稿，其次参考首轮总结；"
            "如果用户问题只涉及某一部分，就只回答那一部分；"
            "如果用户是在顺着你上一次的话往下聊，也要自然接住；"
            "不要重复说你没看视频，不要重新介绍解析流程；"
            "如果信息不足以做强判断，要明确边界，但仍然尽量给出有帮助的分析、建议或看法；"
            f"输出控制在 1-{followup_max_paragraphs} 个自然段内，每段尽量短，不要写成长文。"
        )

    async def _try_handle_followup(self, event: AstrMessageEvent):
        raw_msg = str(getattr(event, "message_str", "") or "")
        if raw_msg.strip().startswith("/"):
            return
        if self._extract_all_candidate_urls(event):
            return
        ctx = self._get_latest_video_context(event)
        if not ctx:
            return
        is_reply_to_bot = await self._is_reply_to_bot(event)
        looks_like_followup = self._looks_like_followup_question(raw_msg)
        if not (is_reply_to_bot or looks_like_followup):
            return
        provider = self._pick_provider()
        if not isinstance(provider, Provider):
            return
        persona_prompt, _persona_name = await self._load_persona_prompt(event)
        extra_prompt = self._pick_prompt_suffix()
        system_prompt = "\n\n".join([p for p in [persona_prompt, extra_prompt] if p]).strip() or "请结合上下文自然回答用户问题。"
        prompt = self._build_followup_prompt(raw_msg, ctx)
        try:
            resp = await provider.text_chat(system_prompt=system_prompt, prompt=prompt)
            text = self._cleanup_output((getattr(resp, "completion_text", "") or "").strip())
            if not text:
                return
            final_text = self._post_format_output(text, max_paragraphs=self._followup_max_paragraphs())
            async for item in self._yield_segmented_text(event, final_text):
                yield item
        except Exception as e:
            logger.warning(f"[video_summary] 追问上下文回答失败: {e}")
        return

    def _is_supported_video_url(self, url: str) -> bool:
        value = (url or "").lower()
        domains = (
            "bilibili.com", "b23.tv", "douyin.com", "iesdouyin.com", "v.douyin.com",
            "xiaohongshu.com", "xhslink.com", "kuaishou.com", "chenzhongtech.com",
            "weibo.com", "weibo.cn", "youtube.com", "youtu.be", "ixigua.com",
            "haokan.baidu.com", "qq.com/x/page/", "video.weibo.com"
        )
        exts = (".mp4", ".m3u8", ".mov", ".mkv", ".webm")
        return any(domain in value for domain in domains) or value.endswith(exts)

    def _has_parse_intent(self, text: str) -> bool:
        content = (text or "").strip().lower()
        if not content:
            return False
        intent_words = (
            "看下", "看一下", "看看", "解析", "总结", "分析", "讲了啥", "讲了什么",
            "提取", "这个视频", "这个链接", "帮我看", "帮忙看"
        )
        return any(word in content for word in intent_words)

    def _is_at_bot(self, event: AstrMessageEvent) -> bool:
        if bool(getattr(event, "is_at_or_wake_command", False)):
            return True
        try:
            message_obj = getattr(event, "message_obj", None)
            chain = getattr(message_obj, "message", None) or []
            self_id = str(getattr(message_obj, "self_id", "") or "")
            for seg in chain:
                if seg.__class__.__name__.lower() != "at":
                    continue
                target = str(getattr(seg, "qq", "") or getattr(seg, "id", "") or "")
                if self_id and target and target == self_id:
                    return True
        except Exception:
            pass
        return False

    async def _extract_reply_urls(self, event: AstrMessageEvent) -> list[str]:
        try:
            message_obj = getattr(event, "message_obj", None)
            chain = getattr(message_obj, "message", None) or []
            reply_seg = None
            for seg in chain:
                if seg.__class__.__name__ == "Reply":
                    reply_seg = seg
                    break
            if not reply_seg:
                return []
            original_msg = await event.bot.api.call_action("get_msg", message_id=reply_seg.id)
            if not original_msg:
                return []
            urls: list[str] = []
            for item in original_msg.get("message") or []:
                if not isinstance(item, dict):
                    continue
                data = item.get("data") or {}
                for value in data.values():
                    if isinstance(value, str):
                        for url in self._extract_urls_from_text(value):
                            if url not in urls:
                                urls.append(url)
            return urls
        except Exception as e:
            logger.debug(f"[video_summary] 提取引用消息链接失败: {e}")
            return []

    async def _detect_natural_trigger_url(self, event: AstrMessageEvent) -> str:
        raw_msg = str(getattr(event, "message_str", "") or "")
        if raw_msg.strip().startswith("/"):
            return ""
        is_private = bool(event.is_private_chat()) if hasattr(event, "is_private_chat") else False
        at_bot = self._is_at_bot(event)
        current_urls = [u for u in self._extract_all_candidate_urls(event) if self._is_supported_video_url(u)]
        if current_urls and (at_bot or is_private):
            return current_urls[0]
        reply_urls = [u for u in await self._extract_reply_urls(event) if self._is_supported_video_url(u)]
        if reply_urls and (at_bot or is_private or self._has_parse_intent(raw_msg)):
            return reply_urls[0]
        if current_urls and self._has_parse_intent(raw_msg):
            return current_urls[0]
        return ""

    async def _run_summary(self, event: AstrMessageEvent, url: str):
        logger.info(f"[video_summary] 识别到链接 url={url}")

        try:
            meta = await self._extract_video_meta(url)
            logger.info(f"[video_summary] 元信息提取成功 title={meta.get('title', '')[:80]!r} duration={meta.get('duration', 0)}")
            max_duration = int(self.config.get("max_duration_seconds", 3600) or 3600)
            duration = int(meta.get("duration", 0) or 0)
            if duration and duration > max_duration:
                yield event.plain_result(f"视频过长：{duration} 秒，超过限制 {max_duration} 秒。")
                return
        except Exception as e:
            logger.error(f"[video_summary] 提取视频元信息失败: {e}")
            provider = self._pick_provider()
            persona_prompt, _persona_name = await self._load_persona_prompt(event)
            extra_prompt = self._pick_prompt_suffix()
            system_prompt = "\n\n".join([p for p in [persona_prompt, extra_prompt] if p]).strip() or "请客观、准确、简洁地总结视频内容。"
            if isinstance(provider, Provider):
                fail_text = await self._render_persona_failure_text(provider, system_prompt, "meta_failed", str(e))
                async for item in self._yield_segmented_text(event, fail_text):
                    yield item
            else:
                yield event.plain_result(f"视频解析失败：{e}")
            return

        provider = self._pick_provider()
        if not isinstance(provider, Provider):
            yield event.plain_result("未找到可用的 LLM Provider，请先检查插件配置或 AstrBot 默认模型。")
            return

        persona_prompt, _persona_name = await self._load_persona_prompt(event)
        extra_prompt = self._pick_prompt_suffix()
        system_prompt = "\n\n".join([p for p in [persona_prompt, extra_prompt] if p]).strip() or "请客观、准确、简洁地总结视频内容。"
        logger.info(f"[video_summary] 使用模式 mode={self._mode()} provider_hint={self._get_provider_hint(provider)}")
        opening_line = await self._build_opening_line(provider, system_prompt)
        await event.send(event.plain_result(opening_line))

        try:
            if self._mode() != "full":
                provider_hint = self._get_provider_hint(provider)
                normal_frame_count = max(3, min(4, int(self.config.get("normal_frame_count", 3) or 3)))
                supports_vision = any(x in provider_hint for x in ("gemini", "gpt", "vl", "vision"))
                logger.info(f"[video_summary] 进入普通模式 provider_hint={provider_hint} supports_vision={supports_vision} normal_frame_count={normal_frame_count}")
                if supports_vision:
                    try:
                        with tempfile.TemporaryDirectory(prefix="video_summary_normal_") as workdir:
                            video_path = await self._download_video_with_retry(url, workdir)
                            logger.info(f"[video_summary] 普通模式视频下载成功 path={video_path}")
                            old_frame_count = self.config.get("full_frame_count", 8)
                            self.config["full_frame_count"] = normal_frame_count
                            try:
                                frame_paths = await self._extract_keyframes(video_path, workdir, int(meta.get("duration", 0) or 0))
                            finally:
                                self.config["full_frame_count"] = old_frame_count
                            logger.info(f"[video_summary] 普通模式切片成功 count={len(frame_paths)}")
                            prompt = self._build_normal_vision_prompt(meta, url)
                            resp = await provider.text_chat(system_prompt=system_prompt, prompt=prompt, image_urls=frame_paths)
                            text = self._cleanup_output((getattr(resp, "completion_text", "") or "").strip())
                            if not text:
                                raise RuntimeError("普通模式轻量看切片返回为空")
                            logger.info("[video_summary] 普通模式最终总结生成成功")
                            final_text = self._post_format_output(text)
                            objective_text = self._cleanup_output(text) or final_text
                            self._save_video_context(event, {
                                "url": url,
                                "meta": meta,
                                "summary": final_text,
                                "final_summary": final_text,
                                "objective_summary": objective_text,
                                "highlights": self._extract_context_highlights(objective_text or final_text),
                                "mode": self._mode(),
                            })
                            if self._use_t2i_output():
                                try:
                                    image_bytes = await self._render_text_card(final_text, meta)
                                    yield event.chain_result([Image.fromBytes(image_bytes)])
                                except Exception as render_err:
                                    logger.warning(f"[video_summary] 普通模式 T2I 渲染失败，回退文本: {render_err}")
                                    async for item in self._yield_segmented_text(event, final_text):
                                        yield item
                            else:
                                async for item in self._yield_segmented_text(event, final_text):
                                    yield item
                            return
                    except Exception as e:
                        logger.warning(f"[video_summary] 普通模式关键帧理解失败，回退元信息模式: {e}")
                else:
                    logger.info("[video_summary] 普通模式当前 provider 不支持看切片，直接走元信息保守解析")
                prompt = self._build_normal_prompt(meta, url)
                resp = await provider.text_chat(system_prompt=system_prompt, prompt=prompt)
                text = self._cleanup_output((getattr(resp, "completion_text", "") or "").strip())
                if not text:
                    raise RuntimeError("LLM 返回为空")
                logger.info("[video_summary] 普通模式回退总结生成成功")
                final_text = self._post_format_output(text)
                objective_text = self._cleanup_output(text) or final_text
                self._save_video_context(event, {
                    "url": url,
                    "meta": meta,
                    "summary": final_text,
                    "final_summary": final_text,
                    "objective_summary": objective_text,
                    "highlights": self._extract_context_highlights(objective_text or final_text),
                    "mode": self._mode(),
                })
                if self._use_t2i_output():
                    try:
                        image_bytes = await self._render_text_card(final_text, meta)
                        yield event.chain_result([Image.fromBytes(image_bytes)])
                    except Exception as render_err:
                        logger.warning(f"[video_summary] 普通模式回退链路 T2I 渲染失败，回退文本: {render_err}")
                        async for item in self._yield_segmented_text(event, final_text):
                            yield item
                else:
                    async for item in self._yield_segmented_text(event, final_text):
                        yield item
                return

            with tempfile.TemporaryDirectory(prefix="video_summary_full_") as workdir:
                logger.info("[video_summary] 进入完整模式")
                video_path = await self._download_video_with_retry(url, workdir)
                logger.info(f"[video_summary] 完整模式视频下载成功 path={video_path}")
                frame_paths = await self._extract_keyframes(video_path, workdir, int(meta.get("duration", 0) or 0))
                logger.info(f"[video_summary] 完整模式切片成功 count={len(frame_paths)}")

                provider_hint = self._get_provider_hint(provider)
                if "deepseek" in provider_hint:
                    logger.info("[video_summary] 完整模式进入 DeepSeek 证据提取链路")
                    evidence_prompt = self._build_evidence_prompt(meta, url)
                    evidence_system = (
                        "你现在只做证据提取，不做总结，不做人格表达。"
                        "请把已确认、推测、无法确认严格分开。"
                    )
                    evidence_resp = await provider.text_chat(
                        system_prompt=evidence_system,
                        prompt=evidence_prompt,
                        image_urls=frame_paths,
                    )
                    evidence_text = self._cleanup_output((getattr(evidence_resp, "completion_text", "") or "").strip())
                    if not evidence_text:
                        raise RuntimeError("证据提取结果为空")

                    objective_prompt = self._build_objective_from_evidence_prompt(evidence_text)
                    objective_system = (
                        "你现在根据证据提取结果生成客观视频理解。"
                        "必须以已确认内容为主，禁止新增事实。"
                    )
                    objective_resp = await provider.text_chat(system_prompt=objective_system, prompt=objective_prompt)
                else:
                    logger.info("[video_summary] 完整模式进入视觉理解链路")
                    objective_prompt = self._build_vision_understanding_prompt(meta, url, provider_hint)
                    objective_system = (
                        "你现在先不要扮演人格，只做客观视频理解。"
                        "请严格基于关键帧画面、可见文字和转写内容总结，不要脑补。"
                    )
                    objective_resp = await provider.text_chat(
                        system_prompt=objective_system,
                        prompt=objective_prompt,
                        image_urls=frame_paths,
                    )
                objective_text = self._cleanup_output((getattr(objective_resp, "completion_text", "") or "").strip())
                if not objective_text:
                    raise RuntimeError("视频理解结果为空")

                final_prompt = self._build_persona_rewrite_prompt(objective_text, provider_hint)
                final_resp = await provider.text_chat(system_prompt=system_prompt, prompt=final_prompt)
                text = self._cleanup_output((getattr(final_resp, "completion_text", "") or "").strip())
                if not text:
                    raise RuntimeError("最终总结为空")
                text = self._de_ai_tail(text)
                logger.info("[video_summary] 完整模式最终总结生成成功")
                final_text = self._post_format_output(text)
                self._save_video_context(event, {
                    "url": url,
                    "meta": meta,
                    "summary": final_text,
                    "objective_summary": objective_text,
                    "final_summary": final_text,
                    "highlights": self._extract_context_highlights(objective_text or final_text),
                    "mode": self._mode(),
                })
                if self._use_t2i_output():
                    try:
                        image_bytes = await self._render_text_card(final_text, meta)
                        yield event.chain_result([Image.fromBytes(image_bytes)])
                    except Exception as render_err:
                        logger.warning(f"[video_summary] 完整模式 T2I 渲染失败，回退文本: {render_err}")
                        async for item in self._yield_segmented_text(event, final_text):
                            yield item
                else:
                    async for item in self._yield_segmented_text(event, final_text):
                        yield item
        except Exception as e:
            logger.error(f"[video_summary] 完整模式总结失败: {e}")
            err_text = str(e)
            error_type = "summary_failed"
            if "Read timed out" in err_text or "timed out" in err_text:
                error_type = "download_timeout"
            elif "download" in err_text.lower() or "未找到下载后的视频文件" in err_text:
                error_type = "download_failed"
            fail_text = await self._render_persona_failure_text(provider, system_prompt, error_type, err_text)
            async for item in self._yield_segmented_text(event, fail_text):
                yield item

    @filter.command("视频总结")
    async def summarize_video(self, event: AstrMessageEvent):
        arg = self._get_arg(getattr(event, "message_str", ""))
        logger.info(f"[video_summary] 收到命令 message={getattr(event, 'message_str', '')!r}")
        if not arg:
            yield event.plain_result("用法：/视频总结 <视频链接>")
            return
        match = URL_RE.search(arg)
        if not match:
            yield event.plain_result("未识别到有效视频链接，请检查后重试。")
            return
        async for item in self._run_summary(event, match.group(0)):
            yield item

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def natural_summarize_video(self, event: AstrMessageEvent):
        handled_followup = False
        async for item in self._try_handle_followup(event):
            handled_followup = True
            yield item
        if handled_followup:
            return
        url = await self._detect_natural_trigger_url(event)
        if not url:
            return
        logger.info(f"[video_summary] 命中自然触发 message={getattr(event, 'message_str', '')!r} url={url}")
        async for item in self._run_summary(event, url):
            yield item
