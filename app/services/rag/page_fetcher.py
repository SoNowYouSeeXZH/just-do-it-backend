"""URL 抓取与正文抽取。

这个文件里绝大部分代码不是"抓网页",而是**防止被抓的对象是内网**。

为什么必须防:工具的 URL 参数最终来自模型输出,而模型输出受用户输入影响。
如果不校验就抓,用户只要诱导模型去访问 `http://169.254.169.254/latest/meta-data/`
(云厂商元数据服务)或 `http://127.0.0.1:6379`,就能让服务器代替他去读
本该访问不到的东西,再把内容当"攻略正文"返回。这类漏洞叫 SSRF
(Server-Side Request Forgery),在带"抓取任意 URL"能力的服务里是头号风险。

四道防线,缺一不可:
1. 协议白名单——只允许 http/https(挡掉 file:// gopher:// 等)
2. 目标 IP 校验——解析域名后逐个检查,禁掉私网/回环/链路本地等地址段
3. 重定向逐跳校验——公网域名 302 到 127.0.0.1 是最经典的绕过手法
4. 体积与时间上限——防止一个巨大响应或慢连接拖住服务
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from urllib.parse import urljoin, urlparse

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# 允许的协议。file/ftp/gopher 这些一旦放开就是直接读本地文件或打内网服务。
_ALLOWED_SCHEMES = {"http", "https"}

# 重定向跟随上限。给 3 次是因为正常站点的 http→https、加 www、加尾斜杠
# 加起来最多也就两三跳;再多通常是重定向环或刻意绕过。
_MAX_REDIRECTS = 3

# 只接受 HTML/纯文本。图片、PDF、视频抓回来也没法当正文用,
# 而且它们体积大,白白消耗带宽和超时预算。
_ALLOWED_CONTENT_PREFIXES = ("text/html", "application/xhtml", "text/plain")

# 响应体大小上限(字节)。按截断长度放宽一些:HTML 标签占了大头,
# 2MB 的 HTML 抽出来的正文通常远超 8000 字。
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class FetchError(RuntimeError):
    """抓取失败。

    和 SearchError 同理:不是 AppError,不会变成 5xx。
    工具层把它转成 observation 交给模型,让模型换一个来源。
    """


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """判断一个 IP 是否属于不该被服务器主动访问的地址段。

    `is_private` 已经覆盖了 10/8、172.16/12、192.168/16、fc00::/7,
    但**不覆盖**下面这些,必须显式加上:
    - is_loopback:127.0.0.1 / ::1,本机上的 Redis、Adminer 都在这
    - is_link_local:169.254.0.0/16,云厂商元数据服务就在 169.254.169.254,
      能读出实例角色的临时凭证,是 SSRF 里危害最大的目标
    - is_reserved / is_multicast / is_unspecified:0.0.0.0 这类特殊地址
    """
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _resolve_and_check(host: str) -> None:
    """解析域名并校验**所有**返回的 IP。

    两个容易踩的点:

    1. 必须检查全部结果而不是第一个。一个域名可以同时解析出公网 IP 和
       127.0.0.1,只查第一条就可能放过去。
    2. 这里存在理论上的 TOCTOU(检查时和使用时解析结果不同,即 DNS rebinding)。
       彻底解决要接管连接、把已校验的 IP 直接传给 socket。当前量级下
       先做到"域名解析层面拦住",并明确记下这个残留风险,而不是假装没有。
    """
    # 主机名本身就是 IP 字面量时,不需要 DNS
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if _is_blocked_ip(literal):
            raise FetchError("目标地址属于内网或保留地址段,已拒绝")
        return

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise FetchError(f"域名无法解析({host})") from exc

    if not infos:
        raise FetchError(f"域名无法解析({host})")

    for info in infos:
        addr = info[4][0]
        try:
            parsed = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _is_blocked_ip(parsed):
            raise FetchError("目标地址属于内网或保留地址段,已拒绝")


def _validate_url(url: str) -> str:
    """校验单个 URL(含重定向的每一跳),返回规范化后的 URL。"""
    parsed = urlparse(url)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise FetchError(f"只允许 http/https,收到:{parsed.scheme or '空'}")
    if not parsed.hostname:
        raise FetchError("URL 缺少主机名")
    _resolve_and_check(parsed.hostname)
    return url


_SCRIPT_STYLE = re.compile(
    r"<(script|style|noscript|template)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_TAG = re.compile(r"<[^>]+>")
_BLANK_LINES = re.compile(r"\n{3,}")
_SPACES = re.compile(r"[ \t\u00a0]+")

# 常见 HTML 实体。不引入 html.unescape 之外的依赖,标准库够用。
_BLOCK_TAGS = re.compile(
    r"</?(p|div|br|li|tr|h[1-6]|section|article|blockquote)\b[^>]*>",
    re.IGNORECASE,
)


def extract_text(html: str) -> str:
    """从 HTML 里抽出可读正文。

    用正则而不是 BeautifulSoup:少一个依赖,而这里的目标不是"完美还原页面",
    只是"给模型一段够用的文字"。代价要说清楚——正则抽正文抽不掉导航栏、
    页脚、广告文案,所以结果里会混入噪声。模型对噪声有一定容忍度,
    真正影响答案质量时再换成 trafilatura / readability 这类正文抽取库。

    顺序很重要:必须先整段删掉 script/style 的**内容**,再删标签。
    反过来先删标签的话,JS 代码会变成正文里的乱码。
    """
    import html as html_module

    text = _SCRIPT_STYLE.sub(" ", html)
    # 块级标签换成换行,否则段落会全部黏成一行,模型很难读
    text = _BLOCK_TAGS.sub("\n", text)
    text = _TAG.sub(" ", text)
    text = html_module.unescape(text)
    text = _SPACES.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = _BLANK_LINES.sub("\n\n", text)
    return text.strip()


async def fetch_page(url: str, *, max_chars: int | None = None) -> str:
    """抓取一个网页并返回截断后的正文。

    手动跟随重定向而不是用 httpx 的 follow_redirects=True:
    自动跟随时中间跳转的地址不经过我们的校验,公网域名 302 到 127.0.0.1
    就直接打进内网了。这是 SSRF 防护里最容易漏的一环。
    """
    limit = max_chars or settings.rag_fetch_max_chars
    current = _validate_url(url)

    try:
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=settings.rag_fetch_timeout_seconds,
            headers={"User-Agent": "JustDoItBot/1.0 (+game guide assistant)"},
        ) as client:
            for _ in range(_MAX_REDIRECTS + 1):
                response = await client.get(current)

                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise FetchError("重定向响应缺少 Location 头")
                    # 相对跳转要先拼成绝对地址再校验
                    current = _validate_url(urljoin(current, location))
                    continue

                if response.status_code >= 400:
                    raise FetchError(f"页面返回 {response.status_code}")

                content_type = response.headers.get("content-type", "").lower()
                if content_type and not content_type.startswith(
                    _ALLOWED_CONTENT_PREFIXES
                ):
                    raise FetchError(f"不支持的内容类型:{content_type.split(';')[0]}")

                body = response.content[:_MAX_RESPONSE_BYTES]
                text = extract_text(body.decode(response.encoding or "utf-8", "replace"))
                if not text:
                    raise FetchError("页面没有可提取的正文")
                logger.info("抓取完成 url=%s chars=%d", current, len(text))
                return text[:limit]

            raise FetchError(f"重定向次数超过上限({_MAX_REDIRECTS})")
    except FetchError:
        raise
    except (httpx.HTTPError, asyncio.TimeoutError) as exc:
        # 同样不把原始异常文本外传:httpx 的报错会带上完整 URL,
        # 而 URL 里可能有 query 参数形式的敏感信息。
        raise FetchError(f"抓取失败({type(exc).__name__})") from exc
