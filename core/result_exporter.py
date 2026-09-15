# -*- coding: utf-8 -*-
from __future__ import unicode_literals
"""
结果导出器（对齐 PHP 版 core/ResultExporter.php）

支持 text / csv / markdown / html，含行数限制（超限抛 RowLimitExceededError）。
数据统一按 UTF-8 写出。Python 2.7 兼容。
"""

import os
import csv

from .exceptions import ExportError, RowLimitExceededError


class ResultExporter(object):
    def __init__(self):
        self.currentRowCount = 0
        self.maxRows = 1000
        self.outputFormat = 'csv'
        self.filePath = ''

    def set_output_format(self, fmt):
        self.outputFormat = fmt

    def set_max_rows(self, max_rows):
        self.maxRows = int(max_rows)

    def get_current_row_count(self):
        return self.currentRowCount

    def detect_format_from_extension(self, file_path):
        ext = os.path.splitext(file_path)[1].lower().lstrip('.')
        return {
            'md': 'markdown',
            'html': 'html',
            'htm': 'html',
            'txt': 'text',
            'csv': 'csv',
            'xls': 'excel',
            'xlsx': 'excel',
        }.get(ext, 'csv')

    def check_row_limit(self, row_count=1):
        if self.currentRowCount + row_count > self.maxRows:
            raise RowLimitExceededError(
                "Row limit exceeded: %d rows written, limit is %d. File: %s. "
                "Please refine your query conditions or increase the limit."
                % (self.currentRowCount, self.maxRows, self.filePath)
            )

    def export(self, data, file_path, headers=None):
        if not self.outputFormat or self.outputFormat == 'auto':
            self.outputFormat = self.detect_format_from_extension(file_path)
        fmt = self.outputFormat
        if fmt == 'markdown':
            return self.export_to_markdown(data, file_path, headers)
        elif fmt == 'html':
            return self.export_to_html(data, file_path, headers)
        elif fmt == 'text':
            return self.export_to_text(data, file_path, headers)
        elif fmt == 'excel':
            return self.export_to_csv(data, file_path, headers)
        else:
            return self.export_to_csv(data, file_path, headers)

    # -- CSV -------------------------------------------------------------
    def export_to_csv(self, data, file_path, headers=None):
        headers = headers or []
        self.filePath = file_path
        self.currentRowCount = 0
        _ensure_dir(file_path)

        try:
            fh = open(file_path, 'wb')
        except (IOError, OSError):
            raise ExportError("无法创建文件: %s" % file_path)

        try:
            writer = _Utf8CsvWriter(fh)
            if headers:
                writer.writerow(headers)
            for row in data:
                self.check_row_limit(1)
                if headers:
                    row_data = [row.get(h, '') for h in headers]
                else:
                    row_data = list(row.values())
                writer.writerow(row_data)
                self.currentRowCount += 1
            fh.close()
            return True
        except RowLimitExceededError:
            fh.close()
            raise
        except Exception as e:
            fh.close()
            raise ExportError("导出失败: %s" % e)

    # -- Markdown --------------------------------------------------------
    def export_to_markdown(self, data, file_path, headers=None):
        self.filePath = file_path
        self.currentRowCount = 0
        _ensure_dir(file_path)

        if not headers and data:
            headers = list(data[0].keys())
        if not headers:
            _write_text(file_path, '')
            return True

        widths = [len(h) for h in headers]
        for row in data:
            for i, h in enumerate(headers):
                v = _s(row.get(h, ''))
                widths[i] = max(widths[i], len(v))

        lines = []
        try:
            header_row = '|'
            for i, h in enumerate(headers):
                header_row += ' ' + _pad(h, widths[i]) + ' |'
            lines.append(header_row)
            sep = '|'
            for i in range(len(headers)):
                sep += '-' * (widths[i] + 2) + '|'
            lines.append(sep)
            for row in data:
                self.check_row_limit(1)
                dr = '|'
                for i, h in enumerate(headers):
                    dr += ' ' + _pad(_s(row.get(h, '')), widths[i]) + ' |'
                lines.append(dr)
                self.currentRowCount += 1
        except RowLimitExceededError:
            _write_text(file_path, '\n'.join(lines) + '\n')
            raise
        _write_text(file_path, '\n'.join(lines) + '\n')
        return True

    # -- HTML ------------------------------------------------------------
    def export_to_html(self, data, file_path, headers=None):
        self.filePath = file_path
        self.currentRowCount = 0
        _ensure_dir(file_path)

        if not headers and data:
            headers = list(data[0].keys())

        buf = []
        buf.append("<!DOCTYPE html>")
        buf.append("<html>")
        buf.append("<head>")
        buf.append('    <meta charset="UTF-8">')
        buf.append("    <title>查询结果</title>")
        buf.append("    <style>")
        buf.append("        table { border-collapse: collapse; width: 100%; margin: 20px 0; }")
        buf.append("        th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }")
        buf.append("        th { background-color: #f2f2f2; font-weight: bold; }")
        buf.append("        tr:nth-child(even) { background-color: #f9f9f9; }")
        buf.append("        tr:hover { background-color: #f5f5f5; }")
        buf.append("    </style>")
        buf.append("</head>")
        buf.append("<body>")
        buf.append("    <table>")
        buf.append("        <thead>")
        buf.append("            <tr>")
        try:
            if headers:
                for h in headers:
                    buf.append("                <th>%s</th>" % _html_escape(h))
            buf.append("            </tr>")
            buf.append("        </thead>")
            buf.append("        <tbody>")
            for row in data:
                self.check_row_limit(1)
                buf.append("            <tr>")
                if headers:
                    for h in headers:
                        buf.append("                <td>%s</td>" % _html_escape(_s(row.get(h, ''))))
                else:
                    for v in row.values():
                        buf.append("                <td>%s</td>" % _html_escape(_s(v)))
                buf.append("            </tr>")
                self.currentRowCount += 1
            buf.append("        </tbody>")
            buf.append("    </table>")
            buf.append("</body>")
            buf.append("</html>")
        except RowLimitExceededError:
            _write_text(file_path, '\n'.join(buf) + '\n')
            raise
        _write_text(file_path, '\n'.join(buf) + '\n')
        return True

    # -- Text ------------------------------------------------------------
    def export_to_text(self, data, file_path, headers=None):
        self.filePath = file_path
        self.currentRowCount = 0
        _ensure_dir(file_path)

        if not headers and data:
            headers = list(data[0].keys())
        if not headers:
            _write_text(file_path, '')
            return True

        widths = [_display_width(h) for h in headers]
        for row in data:
            for i, h in enumerate(headers):
                widths[i] = max(widths[i], _display_width(_s(row.get(h, ''))))

        lines = []
        top = '+'
        for i in range(len(headers)):
            top += '-' * (widths[i] + 2) + '+'
        lines.append(top)
        try:
            hrow = '|'
            for i, h in enumerate(headers):
                hrow += ' ' + _pad_width(h, widths[i]) + ' |'
            lines.append(hrow)
            sep = '+'
            for i in range(len(headers)):
                sep += '-' * (widths[i] + 2) + '+'
            lines.append(sep)
            for row in data:
                self.check_row_limit(1)
                dr = '|'
                for i, h in enumerate(headers):
                    dr += ' ' + _pad_width(_s(row.get(h, '')), widths[i]) + ' |'
                lines.append(dr)
                self.currentRowCount += 1
            bottom = '+'
            for i in range(len(headers)):
                bottom += '-' * (widths[i] + 2) + '+'
            lines.append(bottom)
        except RowLimitExceededError:
            _write_text(file_path, '\n'.join(lines) + '\n')
            raise
        _write_text(file_path, '\n'.join(lines) + '\n')
        return True


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

class _Utf8CsvWriter(object):
    """写出 UTF-8 CSV（Python 2 下把 unicode 编码为 utf-8 bytes，避免 ascii 报错）。"""
    def __init__(self, fh):
        self._fh = fh

    def writerow(self, fields):
        # 先在 unicode 层做 CSV 转义，再统一编码为 utf-8 bytes 写出
        cells = [_s(f) for f in fields]
        line = _csv_join(cells)
        self._fh.write(line.encode('utf-8') + b'\n')


def _csv_join(fields):
    out = []
    for f in fields:
        f = f.replace(u'"', u'""')
        if any(ch in f for ch in (u',', u'"', u'\n', u'\r')):
            f = u'"' + f + u'"'
        out.append(f)
    return u','.join(out)



import sys as _sys

_IS_PY2 = (_sys.version_info[0] == 2)
if _IS_PY2:
    _TEXT_TYPES = (unicode,)  # noqa: F821
else:
    _TEXT_TYPES = (str,)


def _s(v):
    """值转 unicode 字符串（Python 2/3 兼容）。"""
    if v is None:
        return u''
    if isinstance(v, _TEXT_TYPES):
        return v
    if isinstance(v, bytes):
        try:
            return v.decode('utf-8')
        except UnicodeDecodeError:
            try:
                return v.decode('gbk')
            except UnicodeDecodeError:
                return v.decode('utf-8', 'replace')
    if _IS_PY2:
        return unicode(v)  # noqa: F821
    return str(v)


def _ensure_dir(file_path):
    d = os.path.dirname(file_path)
    if d and not os.path.isdir(d):
        try:
            os.makedirs(d)
        except OSError:
            pass


def _write_text(file_path, text):
    fh = open(file_path, 'wb')
    try:
        fh.write(text.encode('utf-8'))
    finally:
        fh.close()


def _html_escape(s):
    return (s.replace('&', '&amp;')
             .replace('<', '&lt;')
             .replace('>', '&gt;')
             .replace('"', '&quot;'))


def _pad(s, width):
    if len(s) >= width:
        return s
    return s + ' ' * (width - len(s))


def _display_width(s):
    try:
        from unicodedata import east_asian_width
        w = 0
        for ch in s:
            w += 2 if east_asian_width(ch) in ('W', 'F') else 1
        return w
    except Exception:
        return len(s)


def _pad_width(s, width):
    cur = _display_width(s)
    if cur >= width:
        return s
    return s + ' ' * (width - cur)
