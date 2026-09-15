# -*- coding: utf-8 -*-
from __future__ import unicode_literals
"""
终端表格渲染器（对齐 PHP 版 controller/TableRenderer.php）

* 中文按宽字符对齐（宽=2）
* 自适应终端宽度：列宽按「最宽列优先」逐级收缩，任何一行绝不超出终端宽度（不折行）
* 列过多、终端放不下时自动切换为竖排「详情视图」（长值按宽折行）
* 分页翻页 / 结果按列排序 / 长值截断省略号
* 数据库字节安全解码为 UTF-8（GBK 兜底）
"""

try:
    from collections import OrderedDict as _OrderedDict
except ImportError:  # pragma: no cover
    _OrderedDict = dict

from .tui import Tui


def _uni(s):
    if s is None:
        return u''
    if isinstance(s, bytes):
        # 已是合法 UTF-8 则保持，否则按 GBK/GB18030 兜底
        try:
            return s.decode('utf-8')
        except UnicodeDecodeError:
            pass
        try:
            return s.decode('gbk')
        except UnicodeDecodeError:
            pass
        try:
            return s.decode('gb18030')
        except UnicodeDecodeError:
            return s.decode('utf-8', 'replace')
    if isinstance(s, unicode):  # noqa: F821
        return s
    return unicode(s)  # noqa: F821


def _char_width(ch):
    try:
        from unicodedata import east_asian_width
        return 2 if east_asian_width(ch) in ('W', 'F') else 1
    except Exception:
        return 1


class TableRenderer(object):
    def __init__(self):
        self.pageSize = 30          # 横排分页大小
        self.verticalPageSize = 5   # 竖排（详情视图）分页大小
        self.maxColumnWidth = 40    # 单列自然宽度上限
        self.minColumnWidth = 4     # 单列收缩下限
        self.verticalCap = 300      # 竖排视图单值上限（超出截断，完整内容走导出）
        self.tailKeepColumns = set()  # 「尾部才是区分位」的列名（如 table）：截断时保留尾部

    def set_page_size(self, n):
        self.pageSize = max(1, int(n))

    # -- 编码/宽度工具 ---------------------------------------------------
    @staticmethod
    def safe_decode(value):
        return _uni(value)

    @staticmethod
    def display_width(s):
        s = _uni(s)
        w = 0
        for ch in s:
            w += _char_width(ch)
        return w

    @staticmethod
    def truncate(s, width):
        s = _uni(s)
        if TableRenderer.display_width(s) <= width:
            return s
        if width <= 1:
            return u'…'
        result = []
        cur = 0
        for ch in s:
            w = _char_width(ch)
            if cur + w > width - 1:
                break
            result.append(ch)
            cur += w
        return u''.join(result) + u'…'

    @staticmethod
    def truncate_tail(s, width):
        """保留尾部的截断：…setting_78_0。用于表名/路径这类区分位在尾部的值。"""
        s = _uni(s)
        if TableRenderer.display_width(s) <= width:
            return s
        if width <= 1:
            return u'…'
        keep = []
        cur = 0
        for ch in reversed(s):
            w = _char_width(ch)
            if cur + w > width - 1:
                break
            keep.append(ch)
            cur += w
        keep.reverse()
        return u'…' + u''.join(keep)

    @staticmethod
    def truncate_middle(s, width):
        """中间省略：保留首尾（如 SQL 的 SELECT 头 + 表名/子句尾）。"""
        s = _uni(s)
        if TableRenderer.display_width(s) <= width:
            return s
        if width <= 1:
            return u'…'
        avail = width - 1  # 一个 … 占位
        head_w = avail // 2
        tail_w = avail - head_w
        head = TableRenderer.truncate(s, head_w + 1)
        if head.endswith(u'…'):
            head = head[:-1]
        tail = TableRenderer.truncate_tail(s, tail_w + 1)
        if tail.startswith(u'…'):
            tail = tail[1:]
        return head + u'…' + tail

    @staticmethod
    def pad_width(s, width):
        s = _uni(s)
        cur = TableRenderer.display_width(s)
        if cur >= width:
            return s
        return s + (u' ' * (width - cur))

    @staticmethod
    def wrap_display(s, width):
        """按显示宽度把字符串切成多段（竖排视图长值折行用）。"""
        s = _uni(s)
        if width < 4:
            width = 4
        lines = []
        cur = []
        cur_w = 0
        for ch in s:
            w = _char_width(ch)
            if cur_w + w > width:
                lines.append(u''.join(cur))
                cur = []
                cur_w = 0
            cur.append(ch)
            cur_w += w
        if cur or not lines:
            lines.append(u''.join(cur))
        return lines

    # -- 宽度预算 --------------------------------------------------------
    def _natural_widths(self, headers, rows):
        """各列自然宽度（表头与数据中最大值，单列封顶 maxColumnWidth）。"""
        widths = [TableRenderer.display_width(h) for h in headers]
        for row in rows:
            for i, h in enumerate(headers):
                val = row.get(h, '')
                w = TableRenderer.display_width(val)
                if w > widths[i]:
                    widths[i] = w
        for i in range(len(widths)):
            if widths[i] > self.maxColumnWidth:
                widths[i] = self.maxColumnWidth
        return widths

    def _layout_widths(self, headers, rows):
        """
        列宽预算：行格式 = 2空格缩进 + 「 内容 」各列 + 「│」分隔。
        总预算 = term - 2(缩进) - 2n(列内边距) - (n-1)(分隔符) - 1(安全余量)。
        超预算时反复收窄当前最宽的一列（不低于各列下限），保证单行不折行。
        放不下（列太多/终端太窄）返回 None，调用方切换竖排视图。
        """
        n = len(headers)
        if n == 0:
            return []
        widths = self._natural_widths(headers, rows)
        budget = Tui.term_width() - 3 * n - 3
        if budget < n * self.minColumnWidth:
            return None
        # 每列下限：短表头按其自身宽度，长表头按 10（可截断成 F_reserve…）
        floors = []
        for i, h in enumerate(headers):
            hw = TableRenderer.display_width(h)
            f = hw if hw < self.minColumnWidth else min(max(self.minColumnWidth, min(hw, 10)), hw)
            floors.append(f)
        if sum(floors) > budget:
            return None
        total = sum(widths)
        while total > budget:
            best = -1
            best_v = 0
            for i, w in enumerate(widths):
                if w > floors[i] and w > best_v:
                    best = i
                    best_v = w
            if best < 0:
                return None
            widths[best] -= 1
            total -= 1
        return widths

    # -- 渲染 ------------------------------------------------------------
    def render(self, headers, rows, title=''):
        if not rows:
            Tui._out(u'  ' + Tui.dim(u'· 无数据') + u'\n')
            return
        widths = self._layout_widths(headers, rows)
        if widths is None:
            Tui.hint(u'列数过多、终端过窄，按详情视图逐行展示')
            self.render_vertical(headers, rows)
            return
        self._print_table(headers, rows, widths)

    def render_paged(self, headers, rows, title=''):
        total = len(rows)
        if total == 0:
            Tui._out(u'  ' + Tui.dim(u'· 无数据') + u'\n')
            return
        widths = self._layout_widths(headers, rows)
        if widths is None:
            Tui.hint(u'列数过多、终端过窄，按详情视图逐行展示')
            self._render_paged_vertical(headers, rows)
            return
        if total <= self.pageSize:
            self._print_table(headers, rows, widths)
            return

        page_start = 0
        while page_start < total:
            page_end = min(page_start + self.pageSize, total)
            Tui._out(u'  ' + Tui.dim(u'第 %d-%d / 共 %d 行' %
                                     (page_start + 1, page_end, total)) + u'\n')
            sub_rows = rows[page_start:page_end]
            self._print_table(headers, sub_rows, widths)

            if page_end >= total:
                go = Tui.ask('回车返回 / r 重看', 'r')
                if go == '' or go is None:
                    break
                if str(go).strip().lower() != 'r':
                    break
                page_start = 0
                continue

            go = Tui.readline(u'  ' + Tui.dim(u'回车下一页 / q 返回') + u': ')
            if str(go).strip().lower() in ('q', 'quit'):
                break
            page_start = page_end

    # -- 竖排详情视图 ------------------------------------------------------
    def render_vertical(self, headers, rows, start_idx=0):
        """每行一张卡片：左键右值，长值按终端宽度折行（可读 JSON/长串）。"""
        term = Tui.term_width()
        keyw = 0
        for h in headers:
            keyw = max(keyw, TableRenderer.display_width(h))
        keyw = min(keyw, 24)
        valw = max(10, term - keyw - 8)
        for i, row in enumerate(rows):
            Tui._out(u'  ' + Tui.dim(u'── #%d ' % (start_idx + i + 1) + u'─' * 14) + u'\n')
            for h in headers:
                v = TableRenderer.truncate(row.get(h, ''), self.verticalCap)
                key = TableRenderer.pad_width(TableRenderer.truncate(h, keyw), keyw)
                chunks = TableRenderer.wrap_display(v, valw)
                Tui._out(u'    ' + Tui.dim(key) + u'  ' + chunks[0] + u'\n')
                for cont in chunks[1:]:
                    Tui._out(u'    ' + u' ' * keyw + u'  ' + cont + u'\n')

    def _render_paged_vertical(self, headers, rows):
        total = len(rows)
        page_size = max(2, self.verticalPageSize)
        page_start = 0
        while page_start < total:
            page_end = min(page_start + page_size, total)
            Tui._out(u'  ' + Tui.dim(u'第 %d-%d / 共 %d 行' %
                                     (page_start + 1, page_end, total)) + u'\n')
            self.render_vertical(headers, rows[page_start:page_end], page_start)
            if page_end >= total:
                break
            go = Tui.readline(u'  ' + Tui.dim(u'回车下一页 / q 返回') + u': ')
            if str(go).strip().lower() in ('q', 'quit'):
                break
            page_start = page_end

    # -- 横排表格 ----------------------------------------------------------
    def _print_table(self, headers, rows, widths):
        self._current_headers = headers
        # 极简风格：列间细竖线，表头下一行细横线，无满格边框
        self._print_row(headers, widths, True)
        self._print_separator(widths)
        for row in rows:
            self._print_row(row, widths, False)

    @staticmethod
    def _print_separator(widths):
        # 与行几何严格一致：单元格左右各 1 空格，交界处单宽度 ┼
        seg = []
        for w in widths:
            seg.append(u'─' * (w + 2))
        Tui._out(u'  ' + Tui.dim(u'┼'.join(seg)) + u'\n')

    def _print_row(self, row, widths, is_header):
        if is_header:
            values = list(row)
        else:
            values = []
            for i, h in enumerate(self._current_headers):
                if isinstance(row, dict):
                    values.append(row.get(h, ''))
                else:
                    values.append(row[i] if i < len(row) else '')
        cells = []
        for i, val in enumerate(values):
            w = widths[i] if i < len(widths) else 0
            if (not is_header and self.tailKeepColumns
                    and i < len(self._current_headers)
                    and self._current_headers[i] in self.tailKeepColumns):
                v = TableRenderer.truncate_tail(val, w)
            else:
                v = TableRenderer.truncate(val, w)
            aligned = (' ' + TableRenderer.pad_width(v, w) + ' ')
            cells.append(Tui.bold(aligned) if is_header else aligned)
        line = u'  ' + u'│'.join(cells)
        Tui._out(line + u'\n')

    # 供 _print_row 取列顺序（渲染时设置）
    _current_headers = []

    # -- 排序 ------------------------------------------------------------
    @staticmethod
    def sort_rows(rows, field, desc=False):
        if field == '' or not rows:
            return rows
        found = any(field in r for r in rows)
        if not found:
            return None

        def cmp(a, b):
            va = a.get(field, '')
            vb = b.get(field, '')
            fa = _is_number(va)
            fb = _is_number(vb)
            if fa and fb:
                na, nb = float(va), float(vb)
                if na == nb:
                    return 0
                return -1 if na < nb else 1
            sa, sb = _uni(va), _uni(vb)
            if sa == sb:
                return 0
            return -1 if sa < sb else 1

        sorted_rows = sorted(rows, cmp=cmp)
        if desc:
            sorted_rows.reverse()
        return sorted_rows

    # -- 规范化 ----------------------------------------------------------
    @staticmethod
    def normalize_data(headers, rows):
        out = []
        for row in rows:
            norm = _OrderedDict()
            for h in headers:
                norm[h] = TableRenderer.safe_decode(row.get(h, ''))
            out.append(norm)
        return out


def _is_number(v):
    if v is None or v == '':
        return False
    try:
        float(v)
        return True
    except (ValueError, TypeError):
        return False
