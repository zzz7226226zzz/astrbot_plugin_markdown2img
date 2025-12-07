import os
import re
import uuid
import traceback
import sys
from typing import List

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.core.message.components import Image, Plain
from astrbot.core.star.star_tools import StarTools

# 确保你已经安装了 mistune 和 playwright
import mistune
import asyncio
from playwright.async_api import async_playwright


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
    "tosaki",
    "使用 /md 指令调用 LLM 并将结果渲染为 Markdown 图片",
    "1.1.0",
)
class MarkdownConverterPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.DATA_DIR = os.path.normpath(StarTools.get_data_dir())
        # 创建一个专门用于存放生成图片的缓存目录
        self.IMAGE_CACHE_DIR = os.path.join(self.DATA_DIR, "md2img_cache")
        # Playwright 是否已就绪的标志
        self._playwright_ready = False
        self._playwright_installing = False

    async def initialize(self):
        """初始化插件，确保图片缓存目录存在，并在后台安装 Playwright"""
        try:
            os.makedirs(self.IMAGE_CACHE_DIR, exist_ok=True)
            logger.info("Markdown 转图片插件已初始化")
            logger.info(f"图片缓存目录: {self.IMAGE_CACHE_DIR}")
            
            # 在后台启动 Playwright 安装任务，不阻塞插件加载
            asyncio.create_task(self._install_playwright_background())
            
        except Exception as e:
            logger.error(f"插件初始化过程中发生错误: {e}")

    async def _install_playwright_background(self):
        """后台安装 Playwright 浏览器"""
        if self._playwright_installing:
            return
        self._playwright_installing = True
        
        try:
            logger.info("正在后台检查并安装 Playwright Chromium 浏览器...")
            
            # 安装 Chromium 浏览器
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "playwright", "install", "chromium",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            
            if process.returncode == 0:
                logger.info("Playwright Chromium 浏览器安装/检查完成")
                self._playwright_ready = True
            else:
                error_msg = stderr.decode('utf-8', errors='ignore') if stderr else "未知错误"
                logger.error(f"Playwright 安装失败: {error_msg}")
                
        except Exception as e:
            logger.error(f"Playwright 后台安装异常: {e}")
        finally:
            self._playwright_installing = False

    async def _ensure_playwright_ready(self) -> bool:
        """确保 Playwright 已就绪，如果未就绪则等待安装完成"""
        if self._playwright_ready:
            return True
            
        # 如果正在安装，等待安装完成（最多等待 120 秒）
        if self._playwright_installing:
            for _ in range(120):
                await asyncio.sleep(1)
                if self._playwright_ready:
                    return True
            return False
        
        # 如果既没就绪也没在安装，尝试安装
        await self._install_playwright_background()
        return self._playwright_ready

    async def terminate(self):
        """插件停用时调用"""
        logger.info("Markdown 转图片插件已停止")

    def _get_full_text_input(self, event: AstrMessageEvent, cmd_prefix: str = "") -> str:
        """手动提取文本，支持图文混排，防止被截断"""
        full_text = ""
        if hasattr(event, 'message_obj') and event.message_obj:
            for component in event.message_obj.message:
                if isinstance(component, Plain):
                    full_text += component.text
        full_text = full_text.strip()
        if cmd_prefix and full_text.startswith(cmd_prefix):
            full_text = full_text[len(cmd_prefix):].strip()
        return full_text

    def _extract_markdown_robust(self, text: str) -> str:
        """
        强壮的 Markdown 提取逻辑：
        1. 尝试匹配 <md>...</md>
        2. 尝试匹配 <md>... (直到文本结束，处理 AI 忘写闭合标签的情况)
        3. 如果都没找到，假定整段文本都是 Markdown (兜底策略)
        """
        # 模式：匹配 <md>...</md> 或 <md>...(到文本结束)
        pattern = r"<md>(.*?)(?:</md>|$)"
        match = re.search(pattern, text, re.DOTALL)
        
        if match:
            content = match.group(1).strip()
            if content:
                return content
        
        # 兜底：如果没找到标签，直接返回原文本
        logger.warning("未检测到 <md> 标签，将渲染全部文本。")
        return text

    @filter.command("md")
    async def cmd_md(self, event: AstrMessageEvent, ctx: Context = None):
        """
        指令: /md <内容>
        说明: 让 LLM 回答并强制渲染为 Markdown 图片
        """
        # 1. 获取用户输入
        query = self._get_full_text_input(event, "/md")
        if not query:
            yield event.plain_result("请在 /md 后输入你想问的内容，例如：/md 帮我写一个快速排序算法")
            return

        # 2. 获取当前会话的 Provider (LLM)
        provider = self.context.get_using_provider()
        if not provider:
            yield event.plain_result("未找到可用的 LLM Provider。")
            return

        yield event.plain_result("✨ 正在生成 Markdown 渲染结果，请稍候...")

        # 3. 构造系统提示词 (强约定版本)
        instruction_prompt = """
【强制任务指令 - 必须严格遵守】
用户希望你以 Markdown 格式回答问题，该回答将被直接渲染为图片。

【格式要求 - 极其重要】
1. 你必须使用丰富的 Markdown 语法（LaTeX公式、代码块、表格、列表）来优化排版。
2. 【最重要】你必须将你的全部回答内容包裹在 <md> 开始标签和 </md> 结束标签之间。
3. 【警告】不要忘记写结束标签 </md>！缺少结束标签会导致渲染失败！
4. 标签外不要有任何内容，所有回答都必须在标签内。

【正确格式示例】
<md>
# 标题

这里是正文内容...

## 代码示例
```python
print("Hello World")
```

## 数学公式
$$ E = mc^2 $$

</md>

【错误示例 - 禁止这样做】
❌ 忘记写 </md> 结束标签
❌ 在 <md> 标签外写内容
❌ 使用其他格式的标签

现在请严格按照上述格式回答用户的问题。
"""

        # 4. 调用 LLM
        try:
            # 直接调用 provider.text_chat，传入 prompt 和 system_prompt
            response = await provider.text_chat(
                prompt=query,
                system_prompt=instruction_prompt
            )
            
            if not response or not response.completion_text:
                yield event.plain_result("❌ LLM 未返回任何内容。")
                return
                
            raw_text = response.completion_text

            # 5. 提取 Markdown 内容（带容错）
            md_content = self._extract_markdown_robust(raw_text)

            # 6. 确保 Playwright 已就绪
            if not await self._ensure_playwright_ready():
                yield event.plain_result("❌ Playwright 浏览器尚未安装完成，请稍后重试。")
                return

            # 7. 渲染图片
            image_filename = f"{uuid.uuid4()}.png"
            output_path = os.path.join(self.IMAGE_CACHE_DIR, image_filename)
            
            await markdown_to_image_playwright(
                md_text=md_content,
                output_image_path=output_path,
                scale=2,
                width=600
            )

            # 8. 发送结果
            if os.path.exists(output_path):
                yield event.image_result(output_path)
            else:
                yield event.plain_result(f"❌ 渲染失败，原始文本：\n{md_content}")

        except Exception as e:
            logger.error(f"MD渲染插件异常: {e}")
            logger.error(traceback.format_exc())
            yield event.plain_result(f"❌ 处理请求时发生错误: {e}")
    

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
