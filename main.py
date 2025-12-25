import os
import re
import sys
import uuid
from typing import List

import asyncio
import mistune
from playwright.async_api import async_playwright

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.message import AssistantMessageSegment, UserMessageSegment
from astrbot.core.message.components import Image, Plain
from astrbot.core.provider.entities import LLMResponse, ProviderRequest
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.star.star_tools import StarTools


async def markdown_to_image_playwright(
    md_text: str,
    output_image_path: str,
    scale: int = 2,
    width: int = None
):
    """
    使用 Playwright 将包含 LaTeX 的 Markdown 转换为图片。

    :param md_text: Markdown 格式的字符串。
    :param output_image_path: 图片输出路径。
    :param scale: 渲染的缩放因子。大于 1 的值可以有效提升清晰度和抗锯齿效果。
    :param width: 图片内容的固定宽度（单位：像素）。如果为 None，则宽度自适应内容。
    """
    # 1. 根据是否提供了 width 参数，动态生成 body 的宽度样式
    width_style = ""
    if width:
        # box-sizing: border-box 可确保 padding 包含在设定的 width 内
        width_style = f"width: {width}px; box-sizing: border-box;"

    # 2. 改进的 HTML 模板，为 body 样式增加了一个占位符 {width_style}
    html_template = """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>Markdown Render</title>
        <style>
            body {{
                {width_style} /* 宽度样式将在这里注入 */
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif, "Apple Color Emoji", "Segoe UI Emoji";
                padding: 25px;
                display: inline-block; /* 让截图尺寸自适应内容 */
                font-size: 16px;

                /* 开启更平滑的字体渲染 */
                -webkit-font-smoothing: antialiased;
                -moz-osx-font-smoothing: grayscale;
                text-rendering: optimizeLegibility;
            }}
            /* 为代码块添加一些样式 */
            pre {{
                background-color: #f6f8fa;
                border-radius: 6px;
                padding: 16px;
                overflow: auto;
                font-size: 85%;
                line-height: 1.45;
            }}
            code {{
                font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
            }}
        </style>
        <script type="text/javascript" async
            src="https://cdnjs.cloudflare.com/ajax/libs/mathjax/2.7.7/MathJax.js?config=TeX-MML-AM_CHTML">
        </script>
        <script type="text/x-mathjax-config">
            MathJax.Hub.Config({{
                tex2jax: {{
                    inlineMath: [['$','$']],
                    displayMath: [['$$','$$']],
                }},
                "HTML-CSS": {{
                    scale: 100,
                    linebreaks: {{ automatic: true }}
                }},
                SVG: {{ linebreaks: {{ automatic: true }} }}
            }});
        </script>
    </head>
    <body>
        {content}
    </body>
    </html>
    """

    # 3. 将 Markdown 转换为 HTML
    html_content = mistune.html(md_text)

    # 4. 填充 HTML 模板，同时传入内容和宽度样式
    full_html = html_template.format(
        content=html_content, width_style=width_style)

    # 5. 使用 Playwright 进行截图
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context(
            device_scale_factor=scale
        )
        page = await context.new_page()

        await page.set_content(full_html, wait_until="networkidle")

        # 更稳健地等待 MathJax 渲染完成
        try:
            await page.evaluate("MathJax.Hub.Queue(['Typeset', MathJax.Hub])")
            await page.wait_for_function("typeof MathJax.Hub.Queue.running === 'undefined' || MathJax.Hub.Queue.running === 0")
        except Exception as e:
            print(f"等待 MathJax 时出错 (可能是页面加载太快): {e}")

        element_handle = await page.query_selector('body')
        if not element_handle:
            raise Exception("无法找到 <body> 元素进行截图。")

        await element_handle.screenshot(path=output_image_path)
        await browser.close()
        print(f"图片已保存到: {output_image_path}")


@register(
    "astrbot_plugin_md2img",
    "tosaki",  # Or your name
    "允许LLM将Markdown文本转换为图片发送",
    "1.0.0",
)
class MarkdownConverterPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.DATA_DIR = os.path.normpath(StarTools.get_data_dir())
        # 创建一个专门用于存放生成图片的缓存目录
        self.IMAGE_CACHE_DIR = os.path.join(self.DATA_DIR, "md2img_cache")

    async def initialize(self):
        """初始化插件，确保图片缓存目录和 Playwright 浏览器存在 (异步版本)"""
        try:
            # os.makedirs is synchronous, but it's extremely fast and not a bottleneck.
            # For a simple, one-off operation like this, it's fine to keep it.
            os.makedirs(self.IMAGE_CACHE_DIR, exist_ok=True)

            logger.info("正在异步检查并安装 Playwright 浏览器依赖...")
            
            # This function starts a subprocess without blocking the event loop.

            # Helper function to run a command and log its output
            async def run_playwright_command(command: list, description: str):
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )

                # Await the process to complete and capture the output
                stdout, stderr = await process.communicate()

                if process.returncode != 0:
                    logger.error(
                        f"自动安装 Playwright {description} 失败，返回码: {process.returncode}")
                    if stderr:
                        logger.error(
                            f"错误输出: \n{stderr.decode('utf-8', errors='ignore')}")
                    return False
                else:
                    output = stdout.decode('utf-8', errors='ignore')
                    # Only log if there's meaningful output (e.g., not just "up to date")
                    if "up to date" not in output:
                        logger.info(
                            f"Playwright {description} 安装/更新完成。\n{output}")
                    else:
                        logger.info(f"Playwright {description} 已是最新，无需下载。")
                    return True

            # Command to install chromium browser
            install_browser_cmd = [sys.executable, "-m",
                                   "playwright", "install", "chromium"]
            await run_playwright_command(install_browser_cmd, "Chromium 浏览器")

            # Command to install system dependencies
            install_deps_cmd = [sys.executable,
                                "-m", "playwright", "install-deps"]
            await run_playwright_command(install_deps_cmd, "系统依赖")

            logger.info("Markdown 转图片插件已初始化")

        except FileNotFoundError:
            # This error happens if 'python -m playwright' cannot be run
            logger.error(
                "无法执行 Playwright 安装命令。请检查 Playwright Python 包是否已正确安装。")
        except Exception as e:
            logger.error(f"插件初始化过程中发生未知错误: {e}")

    async def terminate(self):
        """插件停用时调用"""
        logger.info("Markdown 转图片插件已停止")

    async def _ensure_conversation_id(self, event: AstrMessageEvent) -> str | None:
        """获取当前会话对应的 conversation_id（不存在则创建）。"""
        umo = getattr(event, "unified_msg_origin", None)
        if not umo:
            return None

        cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
        if cid:
            return cid

        # 从参考核心代码看，platform_id 可从 umo 的第 0 段拿到
        platform_id = None
        try:
            platform_id = umo.split(":", 1)[0]
        except Exception:
            platform_id = None

        return await self.context.conversation_manager.new_conversation(
            unified_msg_origin=umo,
            platform_id=platform_id,
        )

    async def _append_user_assistant_to_conversation(
        self,
        *,
        event: AstrMessageEvent,
        user_text: str,
        assistant_text: str,
    ) -> None:
        """方案B：手动把 user/assistant 写入当前会话的 conversation。"""
        cid = await self._ensure_conversation_id(event)
        if not cid:
            return

        await self.context.conversation_manager.add_message_pair(
            cid=cid,
            user_message=UserMessageSegment(content=user_text),
            assistant_message=AssistantMessageSegment(content=assistant_text),
        )

    @filter.command("md")
    async def cmd_md(self, event: AstrMessageEvent, content: GreedyStr = ""):
        """处理 /md 指令，将后续内容发送给 LLM 并将回复转换为图片
        
        用法：/md <你的问题>
        示例：/md 请用Python写一个快速排序算法
        """
        # 标记该事件需要进行 md2img 处理
        event.set_extra("md2img_enabled", True)

        content = (content or "").strip()
        if not content:
            yield event.plain_result(
                "请在 /md 后面输入你的问题\n\n用法：/md <你的问题>\n示例：/md 请用Python写一个快速排序算法",
            )
            return

        # 记录原始用户输入（保留 /md 前缀），方便会话还原真实提问
        event.set_extra("md2img_user_input", f"/md {content}".strip())

        # 将用户的问题发送给 LLM
        yield event.request_llm(
            prompt=content,
            func_tool_manager=self.context.get_llm_tool_manager(),
        )

    @filter.on_llm_request()
    async def on_llm_req(self, event: AstrMessageEvent, req: ProviderRequest):
        """向 LLM 注入使用 Markdown 转图片功能的指令（仅在 /md 指令触发时）"""
        # 只有在 /md 指令触发时才注入 prompt
        if not event.get_extra("md2img_enabled"):
            return
        
        instruction_prompt = """
当你需要发送包含复杂格式（如代码块、表格、嵌套列表等）的内容时，为了获得更好的显示效果，你可以将这部分内容渲染成一张图片。

使用规则：
1. 将你需要转换为图片的 Markdown 全文内容包裹在 `<md>` 和 `</md>` 标签之间。
2. LLM应自行判断何时使用该功能，通常用于格式复杂、纯文本难以阅读的场景。
3. 标签内的内容应为完整的、格式正确的 Markdown 文本。
4. **必须严格确保标签闭合**：必须有且仅有一对 `<md>` 和 `</md>` 标签。
5. **标签必须独占一行**：`<md>` 和 `</md>` 标签应各自单独占据一行。
6. **标签内不能嵌套**：`<md>` 标签内部不能再包含 `<md>` 或 `</md>` 标签。

正确示例：
<md>
# 这是一个标题

这是一个列表:
- 列表项 1
- 列表项 2

这是一个代码块:
```python
def hello_world():
    print("Hello, World!")
```
</md>
"""
        # 仅在 /md 触发时附加指令，保持系统人设不被覆盖，将说明前置到 user prompt
        instruction_prompt = instruction_prompt.strip()
        req.user_prompt = f"{instruction_prompt}\n\n{(req.user_prompt or '').strip()}".strip()

    @filter.on_llm_response()
    async def on_llm_resp(self, event: AstrMessageEvent, resp: LLMResponse):
        """将原始响应暂存（仅在 /md 指令触发时）"""
        # 只有在 /md 指令触发时才处理
        if not event.get_extra("md2img_enabled"):
            return
            
        # 保存 LLM 的原始响应
        event.set_extra("raw_llm_completion_text", resp.completion_text)

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        """在最终消息链生成阶段，解析并替换 <md> 标签（仅在 /md 指令触发时）"""
        # 只有在 /md 指令触发时才处理
        if not event.get_extra("md2img_enabled"):
            return
            
        result = event.get_result()
        # 检查 result 是否存在且有 chain
        if result is None or not result.chain:
            return
            
        chain = result.chain
        new_chain = []
        # 收集用于 conversation 写入的最终文本，确保图片已替换
        conversation_text_parts = []
        for item in chain:
            # 我们只处理纯文本部分
            if isinstance(item, Plain):
                # 调用核心处理函数
                components = await self._process_text_with_markdown(item.text)
                new_chain.extend(components)
                conversation_text_parts.append(self._components_to_plaintext(components))
            else:
                new_chain.append(item)
                conversation_text_parts.append(self._components_to_plaintext([item]))
        result.chain = new_chain

        # 在 Markdown 替换为图片后再写入 conversation
        if not event.get_extra("md2img_conversation_logged"):
            user_text = (event.get_extra("md2img_user_input") or "").strip()
            assistant_text = "".join(conversation_text_parts).strip()
            if user_text and assistant_text:
                try:
                    await self._append_user_assistant_to_conversation(
                        event=event,
                        user_text=user_text,
                        assistant_text=assistant_text,
                    )
                    event.set_extra("md2img_conversation_logged", True)
                except Exception as e:
                    logger.error(f"写入 conversation 失败: {e}")

    def _components_to_plaintext(self, components: List) -> str:
        """将最终组件序列转为可写入会话的文本。"""
        parts = []
        for comp in components:
            if isinstance(comp, Plain):
                parts.append(comp.text)
            elif isinstance(comp, Image):
                parts.append("[MD图片]")
            else:
                parts.append(str(comp))
        return "".join(parts)

    def _clean_unclosed_md_tags(self, text: str) -> str:
        """
        清理未闭合的 md 标签，将其转换为可读的文本格式，避免显示格式错误。
        """
        # 尝试修复：将未闭合的 <md> 替换为可读提示
        # 首先找到所有完整闭合的标签对
        closed_pattern = r'<md>(.*?)</md>'
        
        # 保护已闭合的标签，暂时替换为占位符
        placeholders = {}
        counter = [0]
        
        def save_closed(match):
            key = f"__MD_PLACEHOLDER_{counter[0]}__"
            placeholders[key] = match.group(0)
            counter[0] += 1
            return key
        
        protected_text = re.sub(closed_pattern, save_closed, text, flags=re.DOTALL)
        
        # 清理剩余的未闭合标签
        # 替换孤立的 <md> 为 "[Markdown开始]"
        protected_text = re.sub(r'<md>', '[Markdown内容开始]', protected_text)
        # 替换孤立的 </md> 为 "[Markdown结束]"
        protected_text = re.sub(r'</md>', '[Markdown内容结束]', protected_text)
        
        # 恢复已闭合的标签
        for key, value in placeholders.items():
            protected_text = protected_text.replace(key, value)
        
        return protected_text

    async def _process_text_with_markdown(self, text: str) -> List:
        """
        处理包含 <md>...</md> 标签的文本。
        将其分割成 Plain 和 Image 组件的列表。
        增加了强约束校验，确保标签完美闭合。
        """
        components = []
        
        # 先进行标签校验
        open_count = len(re.findall(r'<md>', text))
        close_count = len(re.findall(r'</md>', text))
        
        # 如果标签数量不匹配，直接返回原文本，不进行处理
        if open_count != close_count:
            logger.warning(f"Markdown 标签不匹配: <md> 出现 {open_count} 次, </md> 出现 {close_count} 次。将原样输出文本。")
            # 移除未闭合的标签，避免显示格式错误
            cleaned_text = self._clean_unclosed_md_tags(text)
            return [Plain(cleaned_text)]
        
        # 检查是否存在嵌套标签（简化检测：检查标签之间是否有另一个开始标签）
        nested_pattern = r'<md>[^<]*<md>'
        if re.search(nested_pattern, text, flags=re.DOTALL):
            logger.warning("检测到嵌套的 <md> 标签，将原样输出文本。")
            cleaned_text = self._clean_unclosed_md_tags(text)
            return [Plain(cleaned_text)]
        
        # 使用更严格的正则表达式来匹配完整闭合的标签
        # 确保 <md> 和 </md> 是完整的标签
        pattern = r"(<md>.*?</md>)"
        parts = re.split(pattern, text, flags=re.DOTALL)

        for part in parts:
            part = part.strip()
            if not part:
                continue

            # 检查当前部分是否是 <md> 标签
            if part.startswith("<md>") and part.endswith("</md>"):
                # 提取标签内的 Markdown 内容
                md_content = part[4:-5].strip()
                if not md_content:
                    continue

                # 生成一个唯一的图片文件名
                image_filename = f"{uuid.uuid4()}.png"
                output_path = os.path.join(self.IMAGE_CACHE_DIR, image_filename)

                try:
                    # 调用库函数生成图片
                    await markdown_to_image_playwright(
                        md_text=md_content,
                        output_image_path=output_path,
                        scale=2,  # 2倍缩放以获得更高清的图片
                        width=600  # 固定宽度为600px，内容过长会自动换行
                    )

                    if os.path.exists(output_path):
                        # 如果图片成功生成，则添加到组件列表中
                        components.append(Image.fromFileSystem(output_path))
                    else:
                        # 如果生成失败，则将原始 Markdown 内容作为纯文本发送
                        logger.error(f"Markdown 图片生成失败，但文件未找到: {output_path}")
                        components.append(Plain(f"--- Markdown 渲染失败 ---\n{md_content}"))
                except Exception as e:
                    logger.error(f"调用 sync_markdown_to_image_playwright 异常: {e}")
                    # 如果转换过程中发生异常，也回退到纯文本
                    components.append(Plain(f"--- Markdown 渲染异常 ---\n{md_content}"))
            else:
                # 如果不是标签，就是普通文本
                components.append(Plain(part))

        return components

