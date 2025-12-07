import os
import re
import uuid
from typing import List


from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.core.message.components import Image, Plain
from astrbot.core.provider.entities import LLMResponse, ProviderRequest
from astrbot.core.star.star_tools import StarTools

# 确保你已经安装了 mistune 和 playwright
import mistune
import asyncio
from playwright.async_api import async_playwright
import uuid

import subprocess
import sys


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


# --- 示例 ---
markdown_string = """
# Playwright 渲染测试

这是一个宽度被设置为 600px 的示例。当文本内容足够长时，它会自动换行以适应设定的宽度。

行内公式 $a^2 + b^2 = c^2$。

独立公式：
$$
\\int_0^\\infty e^{-x^2} dx = \\frac{\\sqrt{\pi}}{2}
$$

以及一段 C++ 代码:
```cpp
#include <iostream>

int main() {
    // 这是一段注释，用来增加代码块的宽度，以测试在固定宽度下的显示效果。
    std::cout << "Hello, C++! This is a longer line to demonstrate wrapping or scrolling." << std::endl;
    return 0;
}
"""

@register(
    "astrbot_plugin_md2img",
    "tosaki",  # Or your name
    "使用 /md 指令调用 LLM 并将结果渲染为 Markdown 图片",
    "1.1.0",
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
    # ========== 仅通过 /md 指令触发的逻辑 ==========

    def _get_full_text_input(self, event: AstrMessageEvent, cmd_prefix: str = "") -> str:
        """提取纯文本消息，支持图文混排，去掉命令前缀"""
        full_text = ""
        if hasattr(event, "message_obj") and event.message_obj:
            for comp in event.message_obj.message:
                if isinstance(comp, Plain):
                    full_text += comp.text
        full_text = full_text.strip()
        if cmd_prefix and full_text.startswith(cmd_prefix):
            full_text = full_text[len(cmd_prefix):].strip()
        return full_text

    @filter.command("md")
    async def cmd_md(self, event: AstrMessageEvent, *_):
        """/md 指令：调用 LLM，并将回答渲染为 Markdown 图片

        使用可变参数 *_ 以兼容 AstrBot 对命令处理函数的额外位置参数传入，
        避免出现“takes 2 positional arguments but 3 were given”的错误。
        """

        query = self._get_full_text_input(event, "/md")
        if not query:
            yield event.plain_result("请在 /md 后输入你想问的内容，例如：/md 帮我写一个快速排序算法")
            return

        provider = self.context.get_using_provider()
        if not provider:
            yield event.plain_result("未找到可用的 LLM Provider。")
            return

        yield event.plain_result("✨ 正在生成 Markdown 渲染结果，请稍候...")

        # 加强版提示词：强制使用 <md> 包裹，且必须闭合
        instruction_prompt = """
【任务指令】
你现在处于一个“Markdown 转图片”专用模式中，请严格按照以下规范回答：

1. 你的核心回答内容必须完整地包裹在一对且仅一对 <md> 和 </md> 标签中，并且标签必须闭合：
   - 开头必须是单独一行的：<md>
   - 结尾必须是单独一行的：</md>
2. <md> 与 </md> 中间的内容必须是合法的 Markdown，可以包含：标题、列表、表格、代码块、以及 LaTeX 公式等。
3. 不要在 </md> 之后再追加其它解释性文本或额外内容。
4. 如果你需要给出示例，也必须放在同一对 <md>...</md> 中，而不是额外再写一对标签。

输出格式示例（务必遵守）：
<md>
# 标题

这里是正文内容，可以包含列表、表格、代码块、以及公式：

```python
def hello():
    print("hello markdown")
```

行内公式示例：$E=mc^2$

独立公式示例：
$$
\int_0^\infty e^{-x^2} dx = \frac{\sqrt{\pi}}{2}
$$
</md>

【特别重要】如果你不能保证标签完整闭合，请直接拒绝回答并说明原因。
"""

        try:
            # 这里仍沿用 text 字段来传递用户问题，由底层 AstrBot 负责构造消息
            req = ProviderRequest(
                text=query,
                session=event.session,
                system_prompt_suffix=instruction_prompt,
            )

            resp: LLMResponse = await provider.text_chat(req)
            if not resp or not resp.completion_text:
                yield event.plain_result("❌ LLM 未返回任何内容。")
                return

            raw_text = resp.completion_text

            # 解析 <md> 标签（容错：没闭合或没写标签也尽量兜底）
            md_content = self._extract_markdown_robust(raw_text)

            image_filename = f"{uuid.uuid4()}.png"
            output_path = os.path.join(self.IMAGE_CACHE_DIR, image_filename)

            await markdown_to_image_playwright(
                md_text=md_content,
                output_image_path=output_path,
                scale=2,
                width=600,
            )

            if os.path.exists(output_path):
                yield event.image_result(output_path)
            else:
                yield event.plain_result(f"❌ 渲染失败，原始文本：\n{md_content}")

        except Exception as e:
            logger.error(f"MD 渲染插件异常: {e}")
            import traceback
            logger.error(traceback.format_exc())
            yield event.plain_result(f"❌ 处理请求时发生错误: {e}")

    def _extract_markdown_robust(self, text: str) -> str:
        """更强壮的 <md> 标签内容提取逻辑。

        1. 优先匹配完整的 <md>...</md>（非贪婪）
        2. 若找不到闭合标签，则匹配从 <md> 到文本结束
        3. 若连 <md> 都没有，则直接将整个文本当作 Markdown 返回
        """

        # 优先匹配完整的 <md>...</md>
        pattern_full = r"<md>(.*?)(?:</md>)"
        m = re.search(pattern_full, text, flags=re.DOTALL)
        if m:
            content = m.group(1).strip()
            if content:
                return content

        # 其次：只找到 <md>，但没写 </md>，那就从 <md> 一直到结尾
        pattern_start_only = r"<md>(.*)$"
        m2 = re.search(pattern_start_only, text, flags=re.DOTALL)
        if m2:
            content = m2.group(1).strip()
            if content:
                logger.warning("检测到 <md> 但缺少 </md>，已自动截取到文本末尾。")
                return content

        # 兜底：没有任何 <md> 标签，就直接返回原始文本
        logger.warning("未检测到 <md> 标签，将把完整回答当作 Markdown 渲染。")
        return text
    

if __name__ == "__main__":
    # 生成一个固定宽度的图片
    output_file_fixed_width = f"markdown_width_{uuid.uuid4().hex[:6]}.png"
    asyncio.run(markdown_to_image_playwright(
        markdown_string,
        output_file_fixed_width,
        scale=2,
        width=1000  # 设置宽度为 600px
    ))

    # 生成一个自适应宽度的图片(不设置 width 参数)
    output_file_auto_width = f"markdown_auto_{uuid.uuid4().hex[:6]}.png"
    asyncio.run(markdown_to_image_playwright(
        markdown_string,
        output_file_auto_width,
        scale=2
    ))
