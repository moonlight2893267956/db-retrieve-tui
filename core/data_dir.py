# -*- coding: utf-8 -*-
from __future__ import unicode_literals
"""
基准数据目录（批量值清单文件的默认位置）

约定：项目根下 `data/` 用来存放值清单文件（CSV/文本）。
dbr 会扫描该目录，让用户从列表里选文件，无需每次输入绝对路径。

  resolve(path, root_dir, data_dir=None)
      把用户给的路径解析成实际文件路径：
        * 绝对路径            -> 原样
        * 含 / 的相对路径      -> 相对项目根
        * 纯文件名            -> 到基准目录（默认 <root>/data）下查找
  scan(data_dir, exts=None)
      列出基准目录下可用的候选文件（按名称排序）。

Python 2.7 兼容；不引入第三方依赖。
"""

import os

DEFAULT_DIR_NAME = 'data'

# 视为「值清单文件」的扩展名（小写）；空表示不限制
DEFAULT_EXTS = ('.csv', '.txt', '.tsv', '.list', '.dat')


def get_data_dir(root_dir, data_dir=None):
    """返回基准目录的绝对路径（不创建）。"""
    if data_dir:
        d = data_dir
        if not os.path.isabs(d):
            d = os.path.join(root_dir, d)
        return os.path.normpath(d)
    return os.path.normpath(os.path.join(root_dir, DEFAULT_DIR_NAME))


def ensure_data_dir(root_dir, data_dir=None):
    """确保基准目录存在，返回其绝对路径。创建失败时抛 OSError。"""
    d = get_data_dir(root_dir, data_dir)
    if not os.path.isdir(d):
        os.makedirs(d)
    return d


def scan(data_dir, exts=DEFAULT_EXTS):
    """
    扫描基准目录下可用的值清单文件。

    返回 list[str]，元素为「文件名」（不含目录），按名称排序。
    只看普通文件；exts 非空时只保留匹配扩展名的文件。
    """
    if not data_dir or not os.path.isdir(data_dir):
        return []
    names = []
    try:
        entries = os.listdir(data_dir)
    except (IOError, OSError):
        return []
    for name in entries:
        if name.startswith('.'):
            continue
        full = os.path.join(data_dir, name)
        if not os.path.isfile(full):
            continue
        if exts:
            low = name.lower()
            if not any(low.endswith(e) for e in exts):
                continue
        names.append(name)
    names.sort()
    return names


def resolve(path, root_dir, data_dir=None):
    """
    把用户输入的路径解析成实际文件路径。

      * 绝对路径            -> 原样返回
      * 含 / 或 \\ 的相对路径 -> 相对 root_dir
      * 纯文件名            -> 优先基准目录，其次 root_dir（兼容旧习惯）

    返回绝对路径（不保证存在，存在性由调用方校验）。
    """
    if not path:
        return path
    if os.path.isabs(path):
        return os.path.normpath(path)
    # 含目录分隔符：相对项目根
    if ('/' in path) or ('\\' in path):
        return os.path.normpath(os.path.join(root_dir, path))
    # 纯文件名：先看基准目录
    base = get_data_dir(root_dir, data_dir)
    candidate = os.path.join(base, path)
    if os.path.isfile(candidate):
        return os.path.normpath(candidate)
    # 再退回项目根（兼容直接放在项目根的旧用法）
    fallback = os.path.join(root_dir, path)
    if os.path.isfile(fallback):
        return os.path.normpath(fallback)
    # 都不存在：按基准目录下的路径返回（报错信息里显示更直观）
    return os.path.normpath(candidate)
