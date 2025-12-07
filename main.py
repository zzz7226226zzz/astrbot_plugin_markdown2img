import os
import re
import uuid
import sys
import asyncio
import importlib
from typing import List

import mistune
from playwright.async_api import async_playwright

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
try:
    from astrbot.core.message.message import AstrMessage  # <--- 新增这行
except Exception:
    # 回退实现：在没有 astrbot 包的开发/静态检查环境中使用一个轻量级替代品
    class AstrMessage:
        def __init__(self, chain=None):
            self.chain = chain or []
from astrbot.core.message.components import Image, Plain
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.star.star_tools import StarTools

# 尝试导入 GreedyStr，兼容环境
try:
    _command_module = importlib.import_module("astrbot.core.star.filter.command")
    GreedyStr = getattr(_command_module, "GreedyStr")
except Exception:
    class GreedyStr(str): pass

# --- Playwright 渲染核心逻辑 (保持不变) ---
async def markdown_to_image_playwright(
    md_text: str,
    output_image_path: str,
    scale: int = 2,
    width: int = None
):
    """(这部分代码与你提供的一致，为节省篇幅省略，请保留原有的 HTML 模板和逻辑)"""
    # ... 请保留你原代码中的 html_template 和渲染逻辑 ...
    # 为了完整运行，这里我只写核心部分，实际使用时请将你原代码的 markdown_to_image_playwright 完整粘贴在这里
    
    width_style = f"width: {width}px; box-sizing: border-box;" if width else ""
    
    html_template = """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <style>
            body {{
                {width_style}
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
                padding: 20px;
                display: inline-block;
                font-size: 16px;
                line-height: 1.6;
            }}
            pre {{ background: #f6f8fa; padding: 16px; border-radius: 6px; overflow: auto; }}
            img {{ max-width: 100%; }}
            table {{ border-collapse: collapse; width: 100%; }}
            th, td {{ border: 1px solid #dfe2e5; padding: 6px 13px; }}
            tr:nth-child(2n) {{ background-color: #f6f8fa; }}
        </style>
        <script type="text/javascript" async src="https://cdnjs.cloudflare.com/ajax/libs/mathjax/2.7.7/MathJax.js?config=TeX-MML-AM_CHTML"></script>
        <script type="text/x-mathjax-config">
            MathJax.Hub.Config({{
                tex2jax: {{ inlineMath: [['$','$']], displayMath: [['$$','$$']] }},
                "HTML-CSS": {{ linebreaks: {{ automatic: true }} }},
                SVG: {{ linebreaks: {{ automatic: true }} }}
            }});
        </script>
    </head>
    <body>{content}</body>
    </html>
    """
    
    html_content = mistune.html(md_text)
    full_html = html_template.format(content=html_content, width_style=width_style)

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context(device_scale_factor=scale)
        page = await context.new_page()
        await page.set_content(full_html, wait_until="networkidle")
        try:
            await page.evaluate("MathJax.Hub.Queue(['Typeset', MathJax.Hub])")
            # 简单等待一下 MathJax
            await page.wait_for_timeout(1000) 
        except Exception:
            pass
        
        element = await page.query_selector('body')
        if element:
            await element.screenshot(path=output_image_path)
        await browser.close()


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
        self.IMAGE_CACHE_DIR = os.path.join(self.DATA_DIR, "md2img_cache")

    async def initialize(self):
        """初始化：检查目录和 Playwright 环境"""
        os.makedirs(self.IMAGE_CACHE_DIR, exist_ok=True)
        # 这里保留你的 Playwright 自动安装逻辑，非常棒
        # ... (为简洁省略，请保留原代码中的 initialize 内容) ...
        # 如果需要，可以将原代码的 install 逻辑放回这里
        pass

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

    @filter.command("md")
    async def cmd_md(self, event: AstrMessageEvent):
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

        # 3. 构造系统提示词 (System Prompt)
        # 关键点：不需要告诉它何时使用，而是强制它必须使用。
        instruction_prompt = """
【任务指令】
用户希望你以 Markdown 格式回答问题，该回答将被直接渲染为图片。
1. 请使用丰富的 Markdown 语法（LaTeX公式、代码块、表格、列表）来优化排版。
2. 即使是纯文本回答，也请尽量分段使其美观。
3. 【重要】为了确保渲染器识别，请务必将你的核心回答内容包裹在 <md> 和 </md> 标签中。
   例如：
   <md>
   # 标题
   这里是正文...
   $$ E=mc^2 $$
   </md>
"""

        # 4. 手动构造请求并调用 LLM
        # 这样可以只针对这一次请求注入 System Prompt，而不污染全局对话
        try:
            # Step A: 将字符串 query 包装成 AstrBot 的消息对象
            # 这是一个包含单个纯文本组件的消息链
            message_obj = AstrMessage(chain=[Plain(query)])
            
            # Step B: 实例化 ProviderRequest
            # 这里的参数名必须是 message_obj，不能是 text
            req = ProviderRequest(
                message_obj=message_obj,
                session=event.session
            )
            
            # Step C: 手动注入本次专用的系统提示词后缀
            req.system_prompt_suffix = instruction_prompt

            # 调用 LLM
            response = await provider.text_chat(req)
            
            if not response or not response.completion_text:
                yield event.plain_result("❌ LLM 未返回任何内容。")
                return
                
            raw_text = response.completion_text

            # 5. 解析和容错处理 (核心改进点)
            md_content = self._extract_markdown_robust(raw_text)

            # 6. 渲染图片
            image_filename = f"{uuid.uuid4()}.png"
            output_path = os.path.join(self.IMAGE_CACHE_DIR, image_filename)
            
            await markdown_to_image_playwright(
                md_text=md_content,
                output_image_path=output_path,
                scale=2,
                width=600 # 手机端阅读体验较好的宽度
            )

            # 7. 发送结果
            if os.path.exists(output_path):
                yield event.image_result(output_path)
            else:
                yield event.plain_result(f"❌ 渲染失败，原始文本：\n{md_content}")

        except Exception as e:
            logger.error(f"MD渲染插件异常: {e}")
            import traceback
            logger.error(traceback.format_exc()) # 打印详细堆栈以便排查
            yield event.plain_result(f"❌ 处理请求时发生错误: {e}")

    def _extract_markdown_robust(self, text: str) -> str:
        """
        强壮的 Markdown 提取逻辑：
        1. 尝试匹配 <md>...</md>
        2. 尝试匹配 <md>... (直到文本结束，处理 AI 忘写闭合标签的情况)
        3. 如果都没找到，假定整段文本都是 Markdown (兜底策略)
        """
        # 模式解释：
        # <md>      : 匹配开始标签
        # (.*?)     : 非贪婪匹配内容
        # (?:</md>|$) : 匹配结束标签 OR 字符串末尾 ($)
        # re.DOTALL : 让 . 可以匹配换行符
        pattern = r"<md>(.*?)(?:</md>|$)"
        match = re.search(pattern, text, re.DOTALL)
        
        if match:
            content = match.group(1).strip()
            # 如果标签内没内容（极少见），可能匹配错位，走兜底
            if content:
                return content
        
        # 兜底：如果没找到标签，或者标签是空的，直接返回原文本。
        # 因为用户既然用了 /md 指令，就是希望渲染，与其报错不如直接渲染。
        logger.warning("未检测到 <md> 标签，将渲染全部文本。")
        return text