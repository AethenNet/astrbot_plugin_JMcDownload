"""
AstrBot JMComic下载器插件
基于 JMComic-Crawler-Python (https://github.com/hect0x7/JMComic-Crawler-Python)

========================================
插件行为注册说明
========================================
1. 插件元数据注册（类级别 @register）
2. 指令注册（方法级别 @filter.command）
   - /搜索本子 <车号> : 搜索本子详情，合并转发形式
   - /下载本子 <车号> : 下载本子，带密码zip发送到群内
   - /本子群名单 : 查看当前群黑白名单配置
3. 插件配置（_conf_schema.json）
   - enable_search: 搜索功能开关
   - enable_download: 下载功能开关
   - enable_search: 搜索功能开关
   - download_config: 下载功能（嵌套对象）
     - enable_download: 下载开关
   - group_filter: 群黑白名单（嵌套对象）
     - group_mode: 开关（开启=黑名单模式，关闭=白名单模式）
     - blacklist_groups: 黑名单群列表
     - whitelist_groups: 白名单群列表
   - max_download_size_mb: 下载大小限制（滑动调节1-200MB，默认10）
========================================
"""

import os
import asyncio
import shutil
from typing import Optional

# ==================== AstrBot 核心导入 ====================
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import astrbot.api.message_components as Comp


# ==================== 常量配置 ====================

# 下载文件大小上限默认值（字节），10MB（可在插件配置中调整，范围1-200MB）
DEFAULT_MAX_DOWNLOAD_SIZE = 10 * 1024 * 1024

# 下载根目录（放在 data 目录下）
DOWNLOAD_ROOT = os.path.join(os.getcwd(), "data", "jmcomic_downloads")

# 单张图片预计下载耗时（秒）
EST_SECONDS_PER_IMAGE = 0.8

# 合并转发消息中发送者显示名
FORWARD_SENDER_NAME = "JMComic 搜索"


def generate_random_password(length: int = 8) -> str:
    """生成随机密码，小写英文+数字混合"""
    import random
    import string
    chars = string.ascii_lowercase + string.digits
    return ''.join(random.choice(chars) for _ in range(length))


# ==================== 工具函数 ====================

def get_dir_size(dir_path: str) -> int:
    """递归计算目录总大小（字节）"""
    total = 0
    for root, _, files in os.walk(dir_path):
        for f in files:
            fp = os.path.join(root, f)
            if os.path.exists(fp):
                total += os.path.getsize(fp)
    return total


def format_size(size_bytes: int) -> str:
    """将字节数格式化为人类可读字符串"""
    if size_bytes < 1024:
        return f"{size_bytes}B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f}KB"
    else:
        return f"{size_bytes / 1024 / 1024:.2f}MB"


def build_jmcomic_option(download_dir: str):
    """构建 jmcomic 的 Option 对象，下载使用移动端 API 客户端。"""
    import jmcomic

    option_yaml = f"""
client:
  impl: api
  retry_times: 3
download:
  cache: false
  image:
    decode: true
    suffix: .jpg
  threading:
    image: 15
    photo: 2
dir_rule:
  rule: Bd_Pname
  base_dir: {download_dir}
"""
    return jmcomic.create_option_by_str(option_yaml)


def format_album_detail(album, show_link: bool = True) -> str:
    """
    将 JmAlbumDetail 对象格式化为用户要求的文本格式。
    严格按照以下格式，每个字段直接换行：

    🔍 搜索结果
    ────────────────
    <名称>
    <id>
    🔗<链接>（show_link=False 时不显示）
    ✍️ 作者：<作者>
    ────────────────
    📅 发布：<发布日期>
    👀 阅读：<观看>
    ❤️ 喜欢：<点赞>
    """
    lines = []

    # 标题
    lines.append("🔍 搜索结果")

    # 分隔线
    lines.append("────────────────")

    # 名称
    lines.append(album.name or "未知标题")

    # id
    lines.append(f"JM{album.album_id}")

    # 链接（无空格，show_link=False 时不显示）
    if show_link:
        lines.append(f"🔗https://18comic.vip/album/{album.album_id}/")

    # 作者（带"作者："前缀）
    author = getattr(album, "author", None) or "未知作者"
    lines.append(f"✍️ 作者：{author}")

    # 分隔线
    lines.append("────────────────")

    # 发布日期（带"发布："前缀）
    pub_date = getattr(album, "pub_date", None)
    if not pub_date or pub_date == "0" or pub_date == 0:
        pub_date = "未知"
    lines.append(f"📅 发布：{pub_date}")

    # 阅读（带"阅读："前缀）
    views = getattr(album, "views", None) or "未知"
    lines.append(f"👀 阅读：{views}")

    # 喜欢（带"喜欢："前缀）
    likes = getattr(album, "likes", None) or "未知"
    lines.append(f"❤️ 喜欢：{likes}")

    return "\n".join(lines)


# ==================== 插件主类 ====================

@register("astrbot_plugin_jmdownload", "SummerDew", "JMComic下载器 - 禁漫天堂搜索与下载插件", "1.0.0")
class JMComicPlugin(Star):
    """
    JMComic 禁漫天堂搜索与下载插件

    注册的指令：
      - /搜索本子 <id> : 搜索本子详情，合并转发形式
      - /下载本子 <id> : 下载本子，带密码zip发送到群内
      - /本子群名单 : 查看当前群黑白名单配置

    插件配置（_conf_schema.json）：
      - enable_blacklist: 开关，关闭=白名单模式，开启=黑名单模式
      - group_list: 群号列表
    """

    def __init__(self, context: Context, config: dict = None):
        """
        插件初始化。
        AstrBot 会自动将 _conf_schema.json 定义的配置通过 config 参数传入。
        """
        super().__init__(context)

        # 保存插件配置（兼容多种传入方式）
        self.plugin_config = config or getattr(self, 'config', None) or {}

        # 下载互斥锁
        self._download_lock = asyncio.Lock()

        # 当前正在下载的本子 ID
        self._current_downloading_id: Optional[str] = None

        # 确保下载根目录存在
        os.makedirs(DOWNLOAD_ROOT, exist_ok=True)

        logger.info(f"[JMComic] 插件初始化完成，下载目录: {DOWNLOAD_ROOT}")
        logger.info(f"[JMComic] 当前配置: {self.plugin_config}")

    # ===================================================================
    # 群权限检查（类方法，从插件配置读取）
    # ===================================================================
    def check_group_permission(self, event: AstrMessageEvent) -> tuple:
        """
        检查当前群是否有权限使用插件。
        返回 (allowed: bool, reason: str)。
        私聊消息默认允许。

        配置说明：
          - group_mode=True (开启) : 黑名单模式，黑名单群列表中的群不可用
          - group_mode=False (关闭) : 白名单模式，仅白名单群列表中的群可用
          - 私聊消息不受限制
        """
        # 获取群号
        group_id = None
        try:
            group_id = getattr(event.message_obj, "group_id", None)
            if group_id is None:
                group_id = getattr(event, "group_id", None)
        except Exception:
            pass

        # 私聊消息（无群号）不受限制
        if not group_id:
            return True, "私聊消息"

        group_id_str = str(group_id)

        # 从插件配置读取（group_filter 嵌套对象）
        group_filter_config = self.plugin_config.get("group_filter", {})
        group_mode = group_filter_config.get("group_mode", True)  # True=黑名单模式，False=白名单模式
        blacklist_groups = group_filter_config.get("blacklist_groups", [])
        whitelist_groups = group_filter_config.get("whitelist_groups", [])

        # 确保群号都是字符串
        blacklist_str = [str(g) for g in blacklist_groups]
        whitelist_str = [str(g) for g in whitelist_groups]

        if group_mode:
            # 黑名单模式：黑名单中的群不可用，其他群可用
            if group_id_str in blacklist_str:
                return False, "当前群已被加入黑名单，无法使用本插件功能。"
            return True, "黑名单外"
        else:
            # 白名单模式：仅白名单中的群可用
            if group_id_str in whitelist_str:
                return True, "白名单内"
            return False, "当前群不在白名单中，无法使用本插件功能。请在插件配置中添加本群号。"

    def check_download_group_permission(self, event: AstrMessageEvent) -> tuple:
        """
        检查当前群是否有权限使用下载功能（下载功能独立的群过滤）。
        返回 (allowed: bool, reason: str)。
        私聊消息默认允许。

        配置说明（嵌套在 download_config 下）：
          - group_mode=True (开启) : 黑名单模式，黑名单群列表中的群不可下载
          - group_mode=False (关闭) : 白名单模式，仅白名单群列表中的群可下载
        """
        # 获取群号
        group_id = None
        try:
            group_id = getattr(event.message_obj, "group_id", None)
            if group_id is None:
                group_id = getattr(event, "group_id", None)
        except Exception:
            pass

        # 私聊消息（无群号）不受限制
        if not group_id:
            return True, "私聊消息"

        group_id_str = str(group_id)

        # 从插件配置读取（download_config 嵌套对象）
        download_config = self.plugin_config.get("download_config", {})
        group_mode = download_config.get("group_mode", True)  # True=黑名单模式，False=白名单模式
        blacklist_groups = download_config.get("blacklist_groups", [])
        whitelist_groups = download_config.get("whitelist_groups", [])

        # 确保群号都是字符串
        blacklist_str = [str(g) for g in blacklist_groups]
        whitelist_str = [str(g) for g in whitelist_groups]

        if group_mode:
            # 黑名单模式：黑名单中的群不可下载，其他群可下载
            if group_id_str in blacklist_str:
                return False, "当前群已被加入下载黑名单，无法使用下载功能。"
            return True, "下载黑名单外"
        else:
            # 白名单模式：仅白名单中的群可下载
            if group_id_str in whitelist_str:
                return True, "下载白名单内"
            return False, "当前群不在下载白名单中，无法使用下载功能。请在插件配置中添加本群号。"

    def check_search_link_permission(self, event: AstrMessageEvent) -> bool:
        """
        检查当前群是否允许在搜索结果中显示链接。
        返回 True=显示链接，False=不显示链接。
        私聊消息默认显示链接。

        配置说明（嵌套在 search_link_config 下）：
          - enable_link=True (开启) : 允许发送链接
          - group_mode=True (开启) : 黑名单模式，黑名单群不发送链接
          - group_mode=False (关闭) : 白名单模式，仅白名单群发送链接
        """
        # 获取群号
        group_id = None
        try:
            group_id = getattr(event.message_obj, "group_id", None)
            if group_id is None:
                group_id = getattr(event, "group_id", None)
        except Exception:
            pass

        # 从插件配置读取（search_config 嵌套对象）
        link_config = self.plugin_config.get("search_config", {})
        enable_link = link_config.get("enable_link", True)

        # 链接发送总开关关闭 → 不显示链接
        if not enable_link:
            return False

        # 私聊消息（无群号）不受限制 → 显示链接
        if not group_id:
            return True

        group_id_str = str(group_id)
        group_mode = link_config.get("group_mode", True)
        blacklist_groups = link_config.get("blacklist_groups", [])
        whitelist_groups = link_config.get("whitelist_groups", [])

        # 确保群号都是字符串
        blacklist_str = [str(g) for g in blacklist_groups]
        whitelist_str = [str(g) for g in whitelist_groups]

        if group_mode:
            # 黑名单模式：黑名单中的群不显示链接，其他群显示
            return group_id_str not in blacklist_str
        else:
            # 白名单模式：仅白名单中的群显示链接
            return group_id_str in whitelist_str

    # ===================================================================
    # 指令：搜索本子
    # ===================================================================
    @filter.command("搜索本子")
    async def search_album(self, event: AstrMessageEvent, jm_id: str):
        """搜索本子详情，以合并转发形式发送。用法：/搜索本子 <车号>"""
        # ---------- 搜索功能开关检查（嵌套在 search_config 下） ----------
        if not self.plugin_config.get("search_config", {}).get("enable_search", True):
            yield event.plain_result("⛔ 搜索功能已关闭，请在插件配置中开启。")
            return

        # ---------- 群权限检查 ----------
        allowed, reason = self.check_group_permission(event)
        if not allowed:
            yield event.plain_result(f"⛔ {reason}")
            return

        # ---------- 参数校验 ----------
        jm_id = jm_id.strip()
        if not jm_id.isdigit():
            yield event.plain_result("❌ 请输入有效的纯数字车号，例如：/搜索本子 350234")
            return

        try:
            # ---------- 获取本子详情（网页端客户端） ----------
            album = await asyncio.to_thread(self._fetch_album_detail, jm_id)

            if album is None:
                yield event.plain_result(f"❌ 未找到车号为 {jm_id} 的本子，请检查车号是否正确。")
                return

            # ---------- 格式化并发送 ----------
            # 检查搜索链接发送权限
            show_link = self.check_search_link_permission(event)
            detail_text = format_album_detail(album, show_link=show_link)

            bot_self_id = getattr(event.message_obj, "self_id", "10000")
            try:
                bot_self_id = int(bot_self_id)
            except (ValueError, TypeError):
                bot_self_id = 10000

            node = Comp.Node(
                uin=bot_self_id,
                name=FORWARD_SENDER_NAME,
                content=[Comp.Plain(detail_text)],
            )

            yield event.chain_result([node])
            logger.info(f"[JMComic] 搜索本子 {jm_id} 成功: {album.name}")

        except Exception as e:
            logger.error(f"[JMComic] 搜索本子 {jm_id} 失败: {e}", exc_info=True)
            yield event.plain_result(
                f"❌ 搜索本子时出错：{str(e)[:200]}\n"
                f"可能原因：网络问题、车号错误、或禁漫服务器暂时不可用。"
            )

    def _fetch_album_detail(self, jm_id: str):
        """
        同步方法：获取本子详情。
        混合策略：先用移动端 API（兼容性好），如果日期/页数获取失败再尝试网页端。
        """
        import jmcomic

        # 第一步：使用移动端 API 获取基本信息（兼容性好，不需要登录）
        api_option = jmcomic.create_option_by_str(
            """
client:
  impl: api
  retry_times: 3
"""
        )
        api_client = api_option.build_jm_client()
        album = api_client.get_album_detail(jm_id)

        # 第二步：检查日期和页数，如果是 '0' 或空，尝试用网页端补充
        pub_date = getattr(album, "pub_date", None)
        page_count = getattr(album, "page_count", None)

        need_html_fallback = (
            not pub_date or pub_date == "0" or pub_date == 0 or
            not page_count or page_count == "0" or page_count == 0
        )

        if need_html_fallback:
            try:
                # 尝试用网页端获取完整信息
                html_option = jmcomic.create_option_by_str(
                    """
client:
  impl: html
  retry_times: 2
"""
                )
                html_client = html_option.build_jm_client()
                html_album = html_client.get_album_detail(jm_id)

                # 用网页端的数据补充移动端缺失的字段
                if (not pub_date or pub_date == "0" or pub_date == 0):
                    html_pub_date = getattr(html_album, "pub_date", None)
                    if html_pub_date and html_pub_date != "0":
                        album.pub_date = html_pub_date

                if (not page_count or page_count == "0" or page_count == 0):
                    html_page_count = getattr(html_album, "page_count", None)
                    if html_page_count and html_page_count != "0":
                        album.page_count = html_page_count

            except Exception:
                # 网页端获取失败，保持移动端的数据
                pass

        return album

    # ===================================================================
    # 指令：下载预览
    # ===================================================================
    @filter.command("下载预览")
    async def preview_download(self, event: AstrMessageEvent, jm_id: str):
        """预览下载信息：页数、预估时间。用法：/下载预览 <车号>"""
        # ---------- 参数校验 ----------
        jm_id = jm_id.strip()
        if not jm_id.isdigit():
            yield event.plain_result("❌ 请输入有效的纯数字车号，例如：/下载预览 350234")
            return

        # ---------- 获取本子详情 ----------
        try:
            album = await asyncio.to_thread(self._fetch_album_detail, jm_id)
            page_count = getattr(album, "page_count", 0)
            album_name = getattr(album, "name", "未知")
        except Exception as e:
            yield event.plain_result(f"❌ 获取本子信息失败：{e}")
            return

        # 页数处理
        try:
            page_count = int(page_count)
        except (ValueError, TypeError):
            page_count = 0

        # 预估下载时间
        if page_count > 0:
            est_seconds = max(5, int(page_count * EST_SECONDS_PER_IMAGE))
        else:
            est_seconds = 30

        # 读取最大下载页数配置
        max_pages = self.plugin_config.get("max_download_pages", 150)
        try:
            max_pages = int(max_pages)
        except (ValueError, TypeError):
            max_pages = 150
        max_pages = max(5, min(1000, max_pages))

        # 构建回复
        reply = (
            f"🛡️ 正在评估… {jm_id}\n"
            f"────────────────\n"
            f"{album_name}\n"
            f"📄 共 {page_count} 页\n"
            f"⏱️ 预计需要 {est_seconds}秒"
        )

        # 页数超限检查
        if page_count > max_pages:
            reply += f"\n❌ 当前 {jm_id} 内容超过设定值暂不支持下载"

        yield event.plain_result(reply)

    # ===================================================================
    # 指令：下载本子
    # ===================================================================
    @filter.command("下载本子")
    async def download_album(self, event: AstrMessageEvent, jm_id: str):
        """下载本子，带密码zip发送到群内。用法：/下载本子 <车号>"""
        # ---------- 下载功能开关检查（嵌套在 download_config 下） ----------
        download_config = self.plugin_config.get("download_config", {})
        if not download_config.get("enable_download", True):
            yield event.plain_result("⛔ 下载功能已关闭，请在插件配置中开启。")
            return

        # ---------- 下载功能独立的群过滤检查 ----------
        download_allowed, download_reason = self.check_download_group_permission(event)
        if not download_allowed:
            yield event.plain_result(f"⛔ {download_reason}")
            return

        # ---------- 全局群权限检查 ----------
        allowed, reason = self.check_group_permission(event)
        if not allowed:
            yield event.plain_result(f"⛔ {reason}")
            return

        # ---------- 参数校验 ----------
        jm_id = jm_id.strip()
        if not jm_id.isdigit():
            yield event.plain_result("❌ 请输入有效的纯数字车号，例如：/下载本子 350234")
            return

        # ---------- 下载互斥检查 ----------
        if self._download_lock.locked():
            current_id = self._current_downloading_id or "未知"
            yield event.plain_result(
                f"⚠️ 当前已有下载任务进行中（车号：{current_id}），\n"
                f"请等待当前任务完成后再发起新的下载。"
            )
            return

        # ---------- 获取锁并执行下载 ----------
        async with self._download_lock:
            self._current_downloading_id = jm_id

            try:
                # 预获取本子详情
                try:
                    album = await asyncio.to_thread(self._fetch_album_detail, jm_id)
                    page_count = getattr(album, "page_count", 0)
                    album_name = getattr(album, "name", "未知")
                except Exception:
                    page_count = 0
                    album_name = "未知"

                # 页数处理
                try:
                    page_count = int(page_count)
                except (ValueError, TypeError):
                    page_count = 0

                # 读取最大下载页数配置
                max_pages = self.plugin_config.get("max_download_pages", 150)
                try:
                    max_pages = int(max_pages)
                except (ValueError, TypeError):
                    max_pages = 150
                max_pages = max(5, min(1000, max_pages))

                # 页数超限检查
                if page_count > max_pages:
                    yield event.plain_result(
                        f"❌ 当前 {jm_id} 内容超过设定值暂不支持下载\n"
                        f"📄 共 {page_count} 页，限制为 {max_pages} 页"
                    )
                    return

                # 估算下载时间
                if page_count > 0:
                    est_seconds = max(5, int(page_count * EST_SECONDS_PER_IMAGE))
                    est_text = f"{est_seconds}秒"
                else:
                    est_text = "约30秒"

                # 回复"正在下载"
                yield event.plain_result(
                    f"📥 正在下载 JM{jm_id}\n"
                    f"📖 《{album_name}》\n"
                    f"📄 共 {page_count} 页\n"
                    f"⏱️ 预计需要 {est_text}"
                )

                # 执行下载+打包
                result = await asyncio.to_thread(self._do_download, jm_id)

                if result["success"]:
                    # 发送完成提示
                    archive_format = result.get("archive_format", "zip")
                    password = result.get("password", "jmcomic")
                    yield event.plain_result(
                        f"✅ 下载完成，正在发送文件...\n"
                        f"📖 《{album_name}》\n"
                        f"📦 解压后大小：{format_size(result['total_size'])}\n"
                        f"🗜️ 压缩格式：{archive_format.upper()}\n"
                        f"🔐 压缩包密码：{password}\n"
                        f"⏱️ 耗时：{result['duration']:.1f}秒"
                    )

                    # 发送压缩包文件
                    archive_file = Comp.File(
                        file=result["archive_path"],
                        name=result["archive_name"],
                    )
                    yield event.chain_result([archive_file])

                    # 清理本地文件
                    if os.path.exists(result["task_dir"]):
                        shutil.rmtree(result["task_dir"], ignore_errors=True)
                    if os.path.exists(result["archive_path"]):
                        os.remove(result["archive_path"])

                    logger.info(
                        f"[JMComic] 下载本子 {jm_id} 成功并已发送，"
                        f"大小: {format_size(result['total_size'])}, "
                        f"耗时: {result['duration']:.1f}秒"
                    )
                else:
                    yield event.plain_result(result["message"])
                    logger.warning(f"[JMComic] 下载本子 {jm_id} 未完成: {result['message']}")

            except Exception as e:
                logger.error(f"[JMComic] 下载本子 {jm_id} 异常: {e}", exc_info=True)
                yield event.plain_result(
                    f"❌ 下载过程中出错：{str(e)[:200]}\n"
                    f"请检查网络连接或稍后重试。"
                )
            finally:
                self._current_downloading_id = None

    def _do_download(self, jm_id: str) -> dict:
        """同步方法：下载本子并打包为带密码的压缩包（支持zip/7z，支持随机/自定义密码）。"""
        import time
        import jmcomic
        import subprocess

        # 从插件配置读取压缩包配置
        archive_config = self.plugin_config.get("archive_config", {})
        archive_format = archive_config.get("archive_format", "zip")
        use_random_password = archive_config.get("random_password", True)
        custom_password = archive_config.get("custom_password", "jmcomic")

        # 确定密码
        if use_random_password:
            password = generate_random_password(8)
        else:
            password = custom_password or "jmcomic"

        # 根据格式确定文件扩展名
        ext = "7z" if archive_format == "7z" else "zip"
        archive_path = os.path.join(DOWNLOAD_ROOT, f"JM{jm_id}.{ext}")
        archive_name = f"JM{jm_id}.{ext}"
        task_dir = os.path.join(DOWNLOAD_ROOT, f"JM{jm_id}")

        # 清理旧文件
        if os.path.exists(task_dir):
            shutil.rmtree(task_dir, ignore_errors=True)
        if os.path.exists(archive_path):
            os.remove(archive_path)
        os.makedirs(task_dir, exist_ok=True)

        start_time = time.time()

        try:
            # 下载本子
            option = build_jmcomic_option(task_dir)
            result = jmcomic.download_album(jm_id, option)

            # 检查部分失败
            dler = result.downloader
            if dler.has_download_failures:
                failed_count = len(dler.download_failed_image) + len(dler.download_failed_photo)
                logger.warning(f"[JMComic] 下载有 {failed_count} 个部分失败")

            # 计算大小
            total_size = get_dir_size(task_dir)

            # 从插件配置读取下载大小限制（MB），范围1-200，默认15MB
            max_size_mb = self.plugin_config.get("max_download_size_mb", 15)
            try:
                max_size_mb = int(max_size_mb)
            except (ValueError, TypeError):
                max_size_mb = 15
            # 限制范围1-200MB
            max_size_mb = max(1, min(200, max_size_mb))
            max_download_size = max_size_mb * 1024 * 1024

            # 大小限制检查
            if total_size > max_download_size:
                shutil.rmtree(task_dir, ignore_errors=True)
                return {
                    "success": False,
                    "message": (
                        f"⚠️ 下载文件总大小为 {format_size(total_size)}，"
                        f"超过限制 {format_size(max_download_size)}（{max_size_mb}MB），已自动删除。\n"
                        f"请尝试页数更少的本子。"
                    ),
                    "task_dir": "",
                    "archive_path": "",
                    "archive_name": "",
                    "password": "",
                    "total_size": total_size,
                    "archive_size": 0,
                    "duration": time.time() - start_time,
                }

            # 根据格式打包
            if archive_format == "7z":
                # 7z 压缩：7z a -p密码 -mhe=on 输出文件 .
                archive_cmd = ['7z', 'a', f'-p{password}', '-mhe=on', '-y', archive_path, '.']
            else:
                # zip 压缩：zip -P 密码 -r -q 输出文件 .
                archive_cmd = ['zip', '-P', password, '-r', '-q', archive_path, '.']

            subprocess.run(
                archive_cmd,
                cwd=task_dir,
                check=True,
                capture_output=True,
            )

            archive_size = os.path.getsize(archive_path)

            return {
                "success": True,
                "message": "下载成功",
                "task_dir": task_dir,
                "archive_path": archive_path,
                "archive_name": archive_name,
                "password": password,
                "archive_format": archive_format,
                "total_size": total_size,
                "archive_size": archive_size,
                "duration": time.time() - start_time,
            }

        except Exception as e:
            if os.path.exists(task_dir):
                shutil.rmtree(task_dir, ignore_errors=True)
            if os.path.exists(archive_path):
                os.remove(archive_path)
            raise e

    # ===================================================================
    # 指令：本子群名单（查看当前配置）
    # ===================================================================
    @filter.command("本子群名单")
    async def group_list_info(self, event: AstrMessageEvent):
        """
        查看当前群黑白名单配置。
        配置在 AstrBot WebUI 插件设置中管理：
          - 搜索功能开关
          - 下载功能开关
          - 群黑白名单开关
          - 黑名单群列表
          - 白名单群列表
        """
        # 从插件配置读取
        # search_config 嵌套对象
        search_config = self.plugin_config.get("search_config", {})
        enable_search = search_config.get("enable_search", True)
        enable_link = search_config.get("enable_link", True)
        search_group_mode = search_config.get("group_mode", True)
        search_blacklist = search_config.get("blacklist_groups", [])
        search_whitelist = search_config.get("whitelist_groups", [])
        # download_config 嵌套对象
        download_config = self.plugin_config.get("download_config", {})
        enable_download = download_config.get("enable_download", True)
        # group_filter 嵌套对象
        group_filter_config = self.plugin_config.get("group_filter", {})
        group_mode = group_filter_config.get("group_mode", True)
        blacklist_groups = group_filter_config.get("blacklist_groups", [])
        whitelist_groups = group_filter_config.get("whitelist_groups", [])
        # 下载大小限制
        max_size_mb = self.plugin_config.get("max_download_size_mb", 10)
        try:
            max_size_mb = int(max_size_mb)
        except (ValueError, TypeError):
            max_size_mb = 10
        max_size_mb = max(1, min(200, max_size_mb))

        # 功能开关状态
        search_status = "✅ 开启" if enable_search else "❌ 关闭"
        download_status = "✅ 开启" if enable_download else "❌ 关闭"
        filter_status = "黑名单模式" if group_mode else "白名单模式"

        # 黑名单列表
        if blacklist_groups:
            blacklist_text = "\n".join(f"  • {g}" for g in blacklist_groups)
        else:
            blacklist_text = "  （空）"

        # 白名单列表
        if whitelist_groups:
            whitelist_text = "\n".join(f"  • {g}" for g in whitelist_groups)
        else:
            whitelist_text = "  （空）"

        # 搜索功能群模式
        search_filter_status = "黑名单模式" if search_group_mode else "白名单模式"
        # 下载功能群模式
        download_group_mode = download_config.get("group_mode", True)
        download_filter_status = "黑名单模式" if download_group_mode else "白名单模式"
        download_blacklist = download_config.get("blacklist_groups", [])
        download_whitelist = download_config.get("whitelist_groups", [])

        yield event.plain_result(
            f"📋 JMComic下载器 插件配置\n"
            f"────────────────\n"
            f"🔍 搜索功能：{search_status}\n"
            f"  └ 链接发送：{'✅ 开启' if enable_link else '❌ 关闭'}\n"
            f"  └ 搜索群过滤：{search_filter_status}\n"
            f"  └ 搜索黑名单：{len(search_blacklist)}个群\n"
            f"  └ 搜索白名单：{len(search_whitelist)}个群\n"
            f"📥 下载功能：{download_status}\n"
            f"  └ 下载群过滤：{download_filter_status}\n"
            f"  └ 下载黑名单：{len(download_blacklist)}个群\n"
            f"  └ 下载白名单：{len(download_whitelist)}个群\n"
            f"🚦 全局群过滤：{filter_status}\n"
            f"📦 下载大小限制：{max_size_mb}MB\n"
            f"────────────────\n"
            f"⚫ 全局黑名单群列表：\n{blacklist_text}\n"
            f"────────────────\n"
            f"⚪ 全局白名单群列表：\n{whitelist_text}\n"
            f"────────────────\n"
            f"💡 配置管理：请在 AstrBot WebUI → 插件设置 → JMComic下载器 中修改"
        )

    # ===================================================================
    # 插件生命周期：terminate
    # ===================================================================
    async def terminate(self):
        """插件卸载时调用。"""
        logger.info("[JMComic] 插件正在卸载...")

        if self._current_downloading_id:
            logger.warning(
                f"[JMComic] 卸载时仍有下载任务进行中: {self._current_downloading_id}"
            )

        logger.info("[JMComic] 插件卸载完成")
