# -*- coding: utf-8 -*-
from __future__ import unicode_literals
"""
终端交互原语（对齐 PHP 版 controller/Tui.php）

提供：ANSI 颜色、清屏（滚动模式）、数字/方向键菜单、文本输入、
      隐藏输入、确认、以及库/表专用的关键字过滤 + 模板直达菜单（filterMenu）。

Python 2.7 兼容；非 TTY 时自动降级（无色、普通输入）。
"""

import os
import re
import sys
import threading

# stdin 结束（如管道耗尽）时统一返回的哨兵
SENTINEL_EOF = u'\x01EOF\x01'

# ANSI 转义序列：CSI（方向键/功能键/颜色等）与常见 ESC 引导序列
_ANSI_ESCAPE_RE = re.compile(
    u'\x1b\\[[0-9;?]*[ -/]*[@-~]'      # CSI ... 字母
    u'|\x1b[()][0-9A-Za-z]'            # ESC ( / ESC )
    u'|\x1b[@-Z\\\\\\-_]'              # 单字符 ESC 序列
)


def _to_text(s):
    if isinstance(s, bytes):
        return s.decode('utf-8', 'replace')
    return s


class Tui(object):
    # 是否彩色（默认开；非 TTY 自动关）
    _color = True
    # 是否「滚动模式」（默认 True：不清屏，向下滚动展示）
    _scroll = True
    # 步骤切换时是否清屏重画（默认开；非 TTY 自动失效）
    _clear = True
    # 终端宽度（用于分隔线自适应；探测失败回退 80）
    _width = 0

    # -- 终端能力 --------------------------------------------------------
    @classmethod
    def is_tty(cls):
        try:
            return sys.stdin.isatty()
        except Exception:
            return False

    @classmethod
    def term_width(cls):
        if cls._width:
            return cls._width
        w = 80
        try:
            import fcntl
            import termios
            import struct
            fd = sys.stdout.fileno()
            data = fcntl.ioctl(fd, termios.TIOCGWINSZ, b'0000')
            rows, cols = struct.unpack('hh', data)
            if 40 <= cols <= 300:
                w = cols
        except Exception:
            try:
                import os
                w = int(os.environ.get('COLUMNS', '80')) or 80
            except Exception:
                w = 80
        cls._width = w
        return w

    @classmethod
    def set_color(cls, on):
        cls._color = bool(on)

    @classmethod
    def set_scroll(cls, on):
        cls._scroll = bool(on)

    @classmethod
    def set_clear(cls, on):
        cls._clear = bool(on)

    @classmethod
    def clear_enabled(cls):
        # 非 TTY（管道/脚本）一律不清屏：保证输出完整、可回溯、可重定向
        return bool(cls._clear) and cls.is_tty()

    # -- 颜色 ------------------------------------------------------------
    # 设计取向：几乎无色。仅用「加粗」做强调、「灰」做次要；颜色只留给错误。
    @classmethod
    def paint(cls, text, code):
        text = _to_text(text)
        if cls._color and cls.is_tty():
            return u'\033[%sm%s\033[0m' % (code, text)
        return text

    @classmethod
    def red(cls, t):    return cls.paint(t, '31')      # 仅错误
    @classmethod
    def green(cls, t):  return cls.paint(t, '32')      # 仅成功（克制使用）
    @classmethod
    def yellow(cls, t): return cls.paint(t, '33')      # 仅警告（克制使用）
    @classmethod
    def blue(cls, t):   return cls.paint(t, '34')
    @classmethod
    def cyan(cls, t):   return cls.paint(t, '36')
    @classmethod
    def bold(cls, t):   return cls.paint(t, '1')
    @classmethod
    def dim(cls, t):    return cls.paint(t, '2')
    @classmethod
    def accent(cls, t): return cls.paint(t, '1')       # 主强调 = 加粗，无色

    # -- 屏幕 ------------------------------------------------------------
    @classmethod
    def clear(cls):
        # 兼容旧调用：不带摘要的全屏清空（仅在清屏开启且 TTY 时生效）
        cls.clear_screen()

    @classmethod
    def clear_screen(cls):
        """清屏并把光标归位。非 TTY 或未开启清屏时为安全空操作。"""
        if cls.clear_enabled():
            cls._out('\033[2J\033[3J\033[H')
        return

    @classmethod
    def anchor(cls, title=u'dbr', sub=u'MySQL 分库分表查询', crumbs=None, summary=None):
        """
        步骤切换锚点：清屏 + 重画「头部 + 面包屑 + 上一步摘要」。

        这是「清屏但不丢上下文」的核心：只在步骤切换时调用，历史列表类输出被清掉，
        关键上下文以一行面包屑 + 若干摘要行的形式保留在顶部。
        非 TTY（脚本/管道）时完全跳过，输出按原样顺序追加，不影响可回溯性。
        """
        if not cls.clear_enabled():
            # 非 TTY：用一个空行做区隔，保持输出可读
            cls._out(u'\n')
            return
        cls.clear_screen()
        cls.header(title, sub)
        if crumbs:
            cls.breadcrumb(*crumbs)
        if summary:
            for line in summary:
                cls._out(u'  ' + cls.dim(line) + u'\n')
        cls._out(u'\n')

    @classmethod
    def rule(cls, indent=2):
        """一条细线（─），宽度固定舒适值，不铺满整屏。"""
        w = min(max(20, cls.term_width() - indent - 2), 72)
        cls._out(u' ' * indent + cls.dim(u'─' * w) + '\n')

    @classmethod
    def hr(cls, char='='):
        cls.rule()

    @classmethod
    def header(cls, title, sub=None):
        """轻量头部：加粗标题（无色）+ 灰副信息 + 一条细线。"""
        cls._out(u'\n  ' + cls.bold(title))
        if sub:
            cls._out(u'  ' + cls.dim(sub))
        cls._out(u'\n')
        cls.rule()

    @classmethod
    def breadcrumb(cls, *parts):
        """面包屑：主路径加粗、次级灰；分隔用 ·"""
        segs = [p for p in parts if p]
        if not segs:
            return
        sep = cls.dim(u'  ·  ')
        rendered = [cls.bold(segs[0])] + [cls.dim(x) for x in segs[1:]]
        cls._out(u'\n  ' + sep.join(rendered) + u'\n')

    @classmethod
    def hint(cls, text):
        """次要提示行（灰）。"""
        cls._out(u'  ' + cls.dim(text) + u'\n')

    @classmethod
    def _cmd_hint(cls, n_groups, n_singles):
        """库/表菜单的一行灰提示：数量概要 + 可用操作。"""
        parts = []
        if n_groups:
            parts.append(u'%d 组' % n_groups)
        if n_singles:
            parts.append(u'%d 单库/单表' % n_singles)
        parts.append(u'可输关键字过滤 / 模板直达')
        cls._out(u'  ' + cls.dim(u'   '.join(parts)) + u'\n')

    @classmethod
    def title(cls, title):
        cls._out(u'\n  ' + cls.bold(title) + u'\n')
        cls.rule()

    @classmethod
    def ok(cls, text):
        cls._out(u'  ' + cls.green(u'✓ ') + text + u'\n')

    @classmethod
    def warn(cls, text):
        cls._out(u'  ' + cls.yellow(u'! ') + text + u'\n')

    @classmethod
    def err(cls, text):
        cls._out(u'  ' + cls.red(u'✗ ') + text + u'\n')

    @classmethod
    def keyvals(cls, pairs, indent=2):
        """对齐的键值展示：  key   value"""
        if not pairs:
            return
        w = max(len(_to_text(k)) for k, _ in pairs)
        pad = u' ' * indent
        for k, v in pairs:
            cls._out(pad + cls.dim(_to_text(k).ljust(w)) + u'   ' + _to_text(v) + '\n')

    # -- 输入 ------------------------------------------------------------
    @staticmethod
    def sanitize_line(line):
        """
        清洗输入行：剥离 ANSI 转义序列（方向键/功能键残留）、按退格语义归约、
        丢弃其余控制字符。避免「nnnv」这类转义/退格残渣被当成真实输入。
        """
        if line is None or line == SENTINEL_EOF:
            return line
        s = _ANSI_ESCAPE_RE.sub(u'', _to_text(line))
        out = []
        for ch in s:
            if ch in (u'\x7f', u'\x08'):      # DEL / BS：删除前一个字符
                if out:
                    out.pop()
                continue
            if ch == u'\t':
                out.append(u' ')
                continue
            if ord(ch) < 0x20:                # 其余控制字符直接丢弃
                continue
            out.append(ch)
        return u''.join(out)

    @staticmethod
    def readline(prompt):
        """读一行；EOF 返回 SENTINEL_EOF。TTY 下用 readline 提升体验。"""
        prompt = _to_text(prompt)
        if Tui.is_tty():
            try:
                import readline as _rl
                # Python 2 + GNU readline 会把提示串按默认编码(C 即 ascii)转换，
                # 含中文的提示会抛 UnicodeEncodeError。这里直接传 UTF-8 字节，
                # 绕过编码转换（与其余输出一致）。
                raw_prompt = prompt
                if sys.version_info[0] == 2 and isinstance(prompt, unicode):  # noqa: F821
                    try:
                        raw_prompt = prompt.encode('utf-8')
                    except Exception:
                        raw_prompt = prompt
                line = raw_input(raw_prompt)  # noqa: F821 (Python 2)
                return Tui.sanitize_line(_to_text(line))
            except EOFError:
                return SENTINEL_EOF
            except ImportError:
                pass
            except KeyboardInterrupt:
                raise
            except Exception:
                # 其它读取异常（编码/终端能力等）不应中断整个程序：
                # 降级为无 readline 的逐行读取。
                pass
        Tui._out(prompt)
        try:
            line = sys.stdin.readline()
        except (EOFError, KeyboardInterrupt):
            return SENTINEL_EOF
        if line == '':
            return SENTINEL_EOF
        return Tui.sanitize_line(_to_text(line).rstrip('\r\n'))

    @classmethod
    def ask(cls, prompt, default=''):
        suffix = (' [%s]' % default) if default != '' else ''
        line = cls.readline(prompt + suffix + ': ')
        if line == SENTINEL_EOF:
            return SENTINEL_EOF
        if line == '':
            return default
        return line

    @classmethod
    def ask_hidden(cls, prompt):
        """隐藏输入（密码）。TTY 下用 termios 关回显。"""
        cls._out(prompt)
        if cls.is_tty():
            try:
                import termios
                import tty
                fd = sys.stdin.fileno()
                old = termios.tcgetattr(fd)
                try:
                    tty.setraw(fd)
                    line = sys.stdin.readline()
                finally:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old)
                cls._out('\n')
                return cls.sanitize_line(_to_text(line).rstrip('\r\n'))
            except Exception:
                pass
        line = sys.stdin.readline()
        if line == '':
            return SENTINEL_EOF
        return cls.sanitize_line(_to_text(line).rstrip('\r\n'))

    @classmethod
    def confirm(cls, prompt, default_yes=True):
        hint = '[Y/n]' if default_yes else '[y/N]'
        while True:
            raw = cls.ask(prompt + ' ' + hint, 'y' if default_yes else 'n')
            if raw == SENTINEL_EOF:
                return False
            ans = raw.strip().lower()
            if ans in ('y', 'yes'):
                return True
            if ans in ('n', 'no'):
                return False
            cls.warn(u'请输入 y 或 n')

    @classmethod
    def shortcut(cls, prompt, options, default=None):
        """
        单字符快捷指令：在提示行直接输入字母，不弹菜单、不重画。
        options: {key: 描述}，如 {'s': '排序', 'e': '导出', 'q': '返回'}
        返回命中的 key（小写）；回车返回 default；EOF 返回 None。
        """
        parts = [u'%s %s' % (k, options[k]) for k in options]
        keys = cls.dim(u' · '.join(parts))
        tail = (u'  ' + cls.dim(u'回车 %s' % default)) if default else u''
        line = cls.readline(u'  ' + cls.dim(u'›') + u' ' + keys + tail + u': ')
        if line == SENTINEL_EOF:
            return None
        ans = line.strip().lower()
        if ans == '':
            return default
        if ans in options:
            return ans
        # 单词前缀（如 quit -> q）
        for k in options:
            if ans == k or ans.startswith(k):
                return k
        # 容错：残留多余字符时（如 'nnnv'），若恰好含唯一有效键则按该键处理
        hits = [k for k in options if k in ans]
        if len(hits) == 1:
            return hits[0]
        cls._err(u'无效输入：%s' % ans)
        return None

    # -- 菜单 ------------------------------------------------------------
    @classmethod
    def _render_menu(cls, title, items, visible, page_start, page_size, allow_back,
                     filterable, total_all):
        """统一菜单绘制：标题 + 对齐序号列表 + 单行底部导航。"""
        if title != '':
            cls._out(u'  ' + cls.bold(title) + u'\n')
        total = len(visible)
        page_end = min(page_start + page_size, total)
        pad = len(str(page_end)) if page_end >= 10 else 1
        for i in range(page_start, page_end):
            num = cls.dim(str(i + 1).rjust(pad))
            cls._out(u'  ' + num + u'  ' + items[visible[i]] + u'\n')

        # 底部：单行导航，灰
        nav = []
        if total < total_all:
            nav.append(u'过滤 %d/%d' % (total, total_all))
        if allow_back:
            nav.append(u'0 返回')
        if page_start > 0:
            nav.append(u'p 上页')
        if page_end < total:
            nav.append(u'n 下页')
        nav.append(u'q 退出')
        cls._out(u'  ' + cls.dim(u'   '.join(nav)) + u'\n')
        return page_end

    @classmethod
    def _read_choice(cls, page_start, page_end, total):
        rng = u'%d-%d/%d' % (page_start + 1, page_end, total)
        return cls.readline(u'  ' + cls.dim(u'选择 ') + u'(' + rng + u'): ')

    @classmethod
    def menu(cls, title, items, allow_back=True, per_page=20):
        """数字菜单。返回选中 0-based 索引；-1 表示返回/取消。"""
        items = list(items)
        total = len(items)
        if total == 0:
            cls._out(u'  ' + cls.yellow(u'（无可用选项）') + u'\n')
            return -1 if allow_back else 0

        page_start = 0
        page_size = max(2, int(per_page))

        while True:
            page_end = cls._render_menu(title, items, list(range(total)),
                                        page_start, page_size, allow_back,
                                        False, total)
            try:
                line = cls._read_choice(page_start, page_end, total)
            except KeyboardInterrupt:
                return -1
            except Exception:
                return -1
            if line == SENTINEL_EOF:
                return -1
            in_ = line.strip().lower()

            if in_ in ('p', 'prev'):
                if page_start > 0:
                    page_start = max(0, page_start - page_size)
                continue
            if in_ in ('n', 'next'):
                if page_end < total:
                    page_start = page_end
                continue
            if in_ in ('q', 'quit', 'exit'):
                return -1
            if allow_back and in_ in ('0', 'b', 'back'):
                return -1
            if in_ == '':
                if page_end < total:
                    page_start = page_end
                else:
                    return -1
                continue
            if not in_.isdigit():
                cls._err(u'请输入序号或 p/n')
                continue
            idx = int(in_) - 1
            if idx < 0 or idx >= total:
                cls._err(u'序号超出 %d~%d' % (page_start + 1, page_end))
                continue
            return idx

    @classmethod
    def filter_menu(cls, title, items, allow_back, per_page=20):
        """
        增强菜单：支持关键字过滤与模板直达（库/表选择专用）。
        返回 (code, value)：
          code=-1 取消/返回；code=0..n-1 选中原始索引；code=-2 模板直达(value=raw)。
        """
        items = list(items)
        visible = list(range(len(items)))
        page_size = max(2, int(per_page))
        page_start = 0

        while True:
            total = len(visible)
            if total == 0:
                cls._out(u'  ' + cls.yellow(u'（无匹配项）') + u'\n')
                return (-1, None)

            page_end = cls._render_menu(title, items, visible, page_start,
                                        page_size, allow_back, True, len(items))
            try:
                line = cls._read_choice(page_start, page_end, total)
            except KeyboardInterrupt:
                return (-1, None)
            except Exception:
                return (-1, None)
            if line == SENTINEL_EOF:
                return (-1, None)
            in_ = line.strip()
            low = in_.lower()

            if low in ('p', 'prev'):
                if page_start > 0:
                    page_start = max(0, page_start - page_size)
                continue
            if low in ('n', 'next'):
                if page_end < total:
                    page_start = page_end
                continue
            if low in ('q', 'quit', 'exit'):
                return (-1, None)
            if allow_back and low in ('0', 'b', 'back'):
                return (-1, None)
            if in_ == '':
                if page_end < total:
                    page_start = page_end
                else:
                    return (-1, None)
                continue
            # 模板直达：含 { 或 .
            if ('{' in in_) or ('.' in in_):
                return (-2, in_)
            # 数字序号
            if in_.isdigit():
                idx = int(in_) - 1
                if idx < 0 or idx >= total:
                    cls._err(u'序号超出 1~%d' % total)
                    continue
                return (visible[idx], None)
            # 关键字过滤
            matched = [oi for oi in visible if low in items[oi].lower()]
            if not matched:
                cls._err(u'无匹配项：%s' % in_)
                continue
            visible = matched
            page_start = 0

    @classmethod
    def _err(cls, text):
        cls.warn(text)

    @staticmethod
    def display_width_safe(s):
        """显示宽度（中文=2），供 spinner 计算清行空格数；不依赖 TableRenderer。"""
        s = _to_text(s)
        try:
            from unicodedata import east_asian_width
            w = 0
            for ch in s:
                w += 2 if east_asian_width(ch) in ('W', 'F') else 1
            return w
        except Exception:
            return len(s)

    # -- Loading 动画 ------------------------------------------------------
    @classmethod
    def spinner(cls, text, detail_fn=None, min_seconds=0.4, interval=0.1):
        """
        原地刷新的单行 loading（阶段文字 + 已耗时）。返回上下文管理器：

            with Tui.spinner(u'正在展开表…', detail_fn=lambda: u'%d 个库' % n):
                slow_call()

        * 仅 TTY 且未关闭动画时生效；非 TTY（管道/重定向）完全静默，不写任何字节。
        * 小于 min_seconds 的短操作不会闪现（延迟启动）。
        * 退出时清除该行，不污染后续输出。
        detail_fn：可选，实时返回动态描述（如已处理数量）。
        """
        return _Spinner(text, detail_fn, min_seconds, interval)

    # -- 输出 ------------------------------------------------------------
    @staticmethod
    def _out(s):
        s = _to_text(s)
        try:
            sys.stdout.write(s)
            sys.stdout.flush()
        except UnicodeEncodeError:
            # stdout 仍是 ascii/字节流：直接写 UTF-8 字节
            try:
                buf = getattr(sys.stdout, 'buffer', None)
                if buf is not None:
                    buf.write(s.encode('utf-8'))
                    buf.flush()
                else:
                    sys.stdout.write(s.encode('utf-8'))
                    sys.stdout.flush()
            except Exception:
                pass
        except Exception:
            pass


class _Spinner(object):
    """后台线程绘制旋转帧；上下文退出时停线程并清行。Python 2.7 兼容。"""

    _FRAMES = (u'⠋', u'⠙', u'⠹', u'⠸', u'⠼', u'⠴', u'⠦', u'⠧', u'⠇', u'⠏')

    def __init__(self, text, detail_fn=None, min_seconds=0.4, interval=0.1):
        self.text = text
        self.detail_fn = detail_fn
        self.min_seconds = min_seconds
        self.interval = interval
        self._thread = None
        self._stop = False
        self._active = False
        import time as _t
        self._time = _t
        self._t0 = 0.0
        self._last_len = 0

    def __enter__(self):
        # 非 TTY：完全静默
        if not Tui.is_tty():
            return self
        self._t0 = self._time.time()
        self._thread = threading.Thread(target=self._run)
        self._thread.daemon = True
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._stop = True
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        if self._active:
            # 用空格覆盖整行后回车，清干净
            Tui._out(u'\r' + u' ' * self._last_len + u'\r')
        return False

    def _run(self):
        i = 0
        while not self._stop:
            elapsed = self._time.time() - self._t0
            if elapsed >= self.min_seconds:
                self._draw(i, elapsed)
                self._active = True
                i = (i + 1) % len(self._FRAMES)
            self._time.sleep(self.interval)

    def _draw(self, idx, elapsed):
        parts = [u'%s %s' % (self._FRAMES[idx], self.text)]
        if self.detail_fn is not None:
            try:
                d = self.detail_fn()
                if d:
                    parts.append(d)
            except Exception:
                pass
        parts.append(u'%.1fs' % elapsed)
        line = u'  ' + u' · '.join(parts)
        pad = max(0, self._last_len - Tui.display_width_safe(line))
        Tui._out(u'\r' + line + u' ' * pad)
        self._last_len = Tui.display_width_safe(line)


def _sleep(seconds):
    import time
    time.sleep(seconds)
