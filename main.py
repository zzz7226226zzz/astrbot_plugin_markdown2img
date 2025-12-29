import os
import re
import uuid
import json
import hashlib
from typing import List


from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.core.message.components import Image, Plain
from astrbot.core.provider.entities import LLMResponse, ProviderRequest
from astrbot.core.star.filter.command import GreedyStr
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
            async def run_playwright_command(command: list, description: str, *, timeout_sec: int = 600):
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )

                # Await the process to complete and capture the output
                try:
                    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_sec)
                except asyncio.TimeoutError:
                    try:
                        process.kill()
                    except Exception:
                        pass
                    logger.error(f"Playwright {description} 安装超时（{timeout_sec}s），已终止。")
                    return False

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
            # install-deps 在不少部署环境（容器/无 root 权限/只读系统）会失败。
            # 这里降级为“尽力而为”：失败只警告，不阻断插件加载。
            install_deps_cmd = [sys.executable, "-m", "playwright", "install-deps"]
            ok = await run_playwright_command(install_deps_cmd, "系统依赖")
            if not ok:
                logger.warning("Playwright 系统依赖安装失败/跳过：插件仍会加载，但首次渲染可能失败。请参考 Playwright 文档手动安装依赖。")

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

    @filter.command("md")
    async def md(self, event: AstrMessageEvent, prompt: GreedyStr = ""):
        """仅在本次请求中开启 <md> 渲染规则注入，用法：/md <你的问题/内容>"""
        # 防止某些平台/适配器在同一条消息上触发两次命令处理
        if event.get_extra("_md2img_cmd_processed", False):
            yield event.plain_result("(md2img) 本条 /md 指令已处理，忽略重复触发。").stop_event()
            return
        event.set_extra("_md2img_cmd_processed", True)

        event.should_call_llm(False)  # 禁用默认 LLM 请求，改由此指令主动发起

        prompt_text = str(prompt).strip()
        if not prompt_text:
            yield event.plain_result("用法：/md <你的问题/内容>\n例如：/md 请用表格总结以下内容…").stop_event()
            return

        # 标记本次 LLM 请求需要注入 <md> 使用规则
        event.set_extra("_md2img_inject", True)

        # 将这次 /md 的请求与回复记录到 AstrBot 的 conversation 中。
        # 注意：这里写入的是 user prompt（不包含 /md），assistant 的内容会在 on_decorating_result 阶段
        # 被替换为“文本 + 图片(image_url parts)”的最终可见版本。
        conversation = None
        cid = None
        try:
            conv_mgr = getattr(self.context, "conversation_manager", None)
            if conv_mgr is not None:
                cid = await conv_mgr.get_curr_conversation_id(event.unified_msg_origin)
                if not cid:
                    platform_id = None
                    try:
                        if hasattr(event, "get_platform_id"):
                            platform_id = event.get_platform_id()
                    except Exception:
                        platform_id = None

                    cid = await conv_mgr.new_conversation(
                        event.unified_msg_origin,
                        platform_id=platform_id,
                    )

                # 获取对话对象（不同 AstrBot 版本的 get_conversation 参数可能不同，这里尽量只用必需参数）
                conversation = await conv_mgr.get_conversation(
                    event.unified_msg_origin,
                    cid,
                )
        except Exception as e:
            logger.warning(f"获取/创建对话失败，将不会记录 /md 到 conversation: {e}")

        if cid:
            event.set_extra("_md2img_conversation_id", cid)
            event.set_extra("_md2img_prompt_text", prompt_text)

        # 重要：结束默认流水线，避免“命令处理 + 普通消息处理”两条链路都跑
        # 注意：必须在 md() 方法体内
        yield event.request_llm(prompt=prompt_text, conversation=conversation).stop_event()

    @filter.on_llm_request()
    async def on_llm_req(self, event: AstrMessageEvent, req: ProviderRequest):
        """向 LLM 注入使用 Markdown 转图片功能的指令"""
        if not event.get_extra("_md2img_inject", False):
            return

        instruction_prompt = """
当你需要发送包含复杂格式（如代码块、表格、嵌套列表等）的内容时，为了获得更好的显示效果，你可以将这部分内容渲染成一张图片。

使用规则：
1. 将你需要转换为图片的 Markdown 全文内容包裹在 `<md>` 和 `</md>` 标签之间。
2. LLM应自行判断何时使用该功能，通常用于格式复杂、纯文本难以阅读的场景。
3. 标签内的内容应为完整的、格式正确的 Markdown 文本,`<md>` 和 `</md>`标签必须成对出现且不能嵌套。

例如：
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
        # 将指令添加到 system prompt 的末尾
        req.system_prompt += f"\\n\\n{instruction_prompt}"

    @filter.on_llm_response()
    async def on_llm_resp(self, event: AstrMessageEvent, resp: LLMResponse):
        """将原始响应暂存，以便后续处理"""
        if not event.get_extra("_md2img_inject", False):
            return
        # 这一步是为了将 LLM 的原始响应（可能包含<md>标签）保存到事件上下文中
        event.set_extra("raw_llm_completion_text", resp.completion_text)

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        """在最终消息链生成阶段，解析并替换 <md> 标签"""
        if not event.get_extra("_md2img_inject", False):
            return

        # 防重复：同一事件的 decorating 可能被框架触发多次（例如重试/二次装饰）。
        # 若已完成一次渲染，就直接跳过，避免重复截图。
        if event.get_extra("_md2img_decorated", False):
            return
        event.set_extra("_md2img_decorated", True)

        result = event.get_result()
        if result is None or not getattr(result, "chain", None):
            return
        chain = result.chain
        new_chain = []
        for item in chain:
            # 我们只处理纯文本部分
            if isinstance(item, Plain):
                # 调用核心处理函数
                components = await self._process_text_with_markdown(item.text)
                new_chain.extend(components)
            else:
                new_chain.append(item)
        result.chain = new_chain

    # /md 触发的请求：把 conversation 中最后一条 assistant 消息更新为“装饰后”的内容
    # （Plain -> text part，Image -> image_url part），用于对话记录展示/导出。

        cid = event.get_extra("_md2img_conversation_id")
        if not cid:
            return

        conv_mgr = getattr(self.context, "conversation_manager", None)
        if conv_mgr is None:
            return

        # 组装 OpenAI-style content parts
        parts: list[dict] = []
        for comp in result.chain:
            if isinstance(comp, Plain):
                if comp.text:
                    parts.append({"type": "text", "text": comp.text})
            elif isinstance(comp, Image):
                # 注意：register_to_file_service() 使用的是一次性 token（被访问后会失效），
                # 不适合持久化到 conversation 历史中。
                # 这里使用 base64 data URI，保证对话历史可长期回看。
                try:
                    bs64 = await comp.convert_to_base64()
                    if bs64:
                        url = f"data:image/png;base64,{bs64}"
                        parts.append({"type": "image_url", "image_url": {"url": url}})
                except Exception:
                    pass

        if not parts:
            return

        try:
            conv = await conv_mgr.get_conversation(event.unified_msg_origin, cid)
            if not conv or not getattr(conv, "history", None):
                return

            history = json.loads(conv.history)
            if not isinstance(history, list) or not history:
                return

            prompt_text = event.get_extra("_md2img_prompt_text")

            def _match_user_prompt(rec: dict) -> bool:
                if not prompt_text:
                    return False
                if rec.get("role") != "user":
                    return False
                content = rec.get("content")
                if content == prompt_text:
                    return True
                if isinstance(content, list):
                    # OpenAI parts
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "text" and item.get("text") == prompt_text:
                            return True
                return False

            # 优先定位“本次 user prompt”的下一个 assistant
            user_index = None
            for i in range(len(history) - 1, -1, -1):
                rec = history[i]
                if isinstance(rec, dict) and _match_user_prompt(rec):
                    user_index = i
                    break

            updated = False
            if user_index is not None:
                for j in range(user_index + 1, len(history)):
                    rec = history[j]
                    if isinstance(rec, dict) and rec.get("role") == "assistant":
                        rec["content"] = parts
                        updated = True
                        break

            # 回退：从后往前找到最后一条 assistant
            if not updated:
                for i in range(len(history) - 1, -1, -1):
                    rec = history[i]
                    if isinstance(rec, dict) and rec.get("role") == "assistant":
                        rec["content"] = parts
                        updated = True
                        break

            if not updated:
                return

            await conv_mgr.update_conversation(
                event.unified_msg_origin,
                cid,
                history=history,
            )
        except Exception as e:
            logger.warning(f"更新 conversation 的 /md 装饰后内容失败: {e}")

    async def _process_text_with_markdown(self, text: str) -> List:
        """
        处理包含 <md>...</md> 标签的文本。
        将其分割成 Plain 和 Image 组件的列表。
        """
        components = []
        # 使用 re.split 来分割文本，保留分隔符（<md>...</md>）
        # re.DOTALL 使得 '.' 可以匹配包括换行符在内的任意字符
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

                # 基于内容的缓存：同样的 md_content 不重复渲染
                md_hash = hashlib.sha256(md_content.encode("utf-8")).hexdigest()
                output_path = os.path.join(self.IMAGE_CACHE_DIR, f"{md_hash}.png")

                try:
                    # 如果缓存已存在且非空，直接复用
                    if not (os.path.exists(output_path) and os.path.getsize(output_path) > 0):
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
