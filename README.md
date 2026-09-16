# dbr - MySQL 分库分表交互查询工具（Python 版 CLI REPL）

> 本项目是 PHP/HHVM 版 `db-retrieval-cli` 的 **Python 2.7 重构版**：解决「上线后目标机器没有 HHVM，
> 只有系统自带 Python 2.7.5」无法运行的问题。功能对齐 PHP 版：向导式查询、自动识别分库分表、
> 智能缩表、终端表格直显、排序/分页/导出；并新增 **复用测试机已配好的 mysql alias** 作为数据源。
>
> 📖 **使用手册（含示例数据的逐步案例）见 [`docs/使用手册.md`](docs/使用手册.md)。**

## 为什么能用 Python 2.7 跑（关键设计）

目标机器的 Python 2.7.5 **没有 MySQLdb / pymysql 驱动，也没有 pip**，但系统自带
`/bin/mysql` 客户端（MariaDB 5.5）。因此本工具：

- **不依赖任何第三方 Python 模块**，只用 Python 2.7 标准库；
- 数据库访问通过 `subprocess` 调用 `mysql` 客户端（`-B` 批处理 TSV 输出，`--default-character-set=utf8`）；
- 密码通过环境变量 `MYSQL_PWD` 传入（不进 argv/`ps`）。

## 运行环境

- Python 2.7（测试机实测 `/usr/bin/python` = 2.7.5）。
- 系统 `mysql` 客户端（`/bin/mysql` 或 PATH 中）。
- UTF-8 终端（数据以 UTF-8 展示与导出；GBK 字节流自动兜底转码）。

## 快速开始

**默认就用你终端里已有的 mysql alias，无需任何配置：**

```bash
/usr/bin/python dbr.py       # 数据源 = 当前 shell 的 mysql alias（paybase / cifdb / vipcifdb ...）
./dbr.sh                     # 或用启动脚本（自动探测 Python 2.7）

/usr/bin/python dbr.py --help
```

数据源直接取 shell 里形如下面的 alias，**名字即数据源名**，密码沿用 alias 里已有的：

```bash
alias paybase='mysql -A -h10.0.0.10 -P3306 -udb_user -p<YOUR_PASSWORD>'
alias cifdb='mysql -ucif_user -p'\''<YOUR_PASSWORD>'\'' -h10.0.0.10 -P3307'
alias vipcifdb='mysql -h10.0.0.10 -ucif_rw -p'\''...'\'' -P 3307 --database db_vip_demo'
```

启动后进入交互，**选库与选表分离（两步）**，界面为极简无色风格（加粗/灰分层、细线分隔、列表对齐）：

界面采用**步骤切换清屏 + 上下文锚点**：每进入新步骤会清屏并重画「头部 + 面包屑 + 上一步摘要」，
界面始终聚焦当前步骤，同时保留关键上下文（数据源/库/表/目标数/缩表结论/上次结果）。
默认在 TTY 下开启；**输出重定向或管道运行时自动关闭**（保证完整可回溯），也可用 `--no-clear` 关闭。

```text
  dbr  MySQL 分库分表查询
  ─────────────────────────────────────────────
  paybase  ·  demo_db_78  ·  t_user_setting_78_0  ·  [预览]
  目标  1 张表  ·  单表                          ← 上一步关键上下文摘要（清屏后保留）
  SELECT * FROM demo_db_78.t_user_setting_78_0 WHERE F_user_id='1000000003' LIMIT 100
  › 回车执行 · n 取消:
```

各步骤（面包屑逐步生长、摘要常驻）：

```text
  paybase  ·  [选库]                     ← 菜单选择后进入下一步
  paybase  ·  demo_db_78  ·  [选表]
  paybase  ·  demo_db_78  ·  t_user_setting_78_0  ·  [条件]
  ...  ·  [预览]   →   ...  ·  [执行]   →   ...  ·  [结果]
```

每一步的完整内容示例：

```text
  选择数据源
  1  cifdb     10.0.0.10:3307  cif_user
  2  paybase   10.0.0.10:3306  db_user
  0 返回   q 退出
  选择 (1-2/2):                          ✓ 已连接 paybase

  paybase  ·  选库
  6 组   14 单库/单表   可输关键字过滤 / 模板直达
  1  手动输入库名/模板
  2  demo_db_{db}                ← 回车=遍历整组，或输 00 缩到具体库
  3  demo_db_withdraw_{db}
  ...
  8  更多数据库…
  0 返回   q 退出

  paybase  ·  demo_db_00  ·  选表
  3 组   可输关键字过滤 / 模板直达
  1  手动输入表名/模板
  2  t_user_setting_{db}_{table}  ← 回车=遍历整组，或输 3 缩到具体表
  0 返回   q 退出

  SELECT 字段 [*]:                                  ← 回车用默认 *
  where 条件（回车=不加）:  F_user_id='1000000003'   ← 回车=不加 where
  LIMIT 行数（NONE=不限制） [100]:                   ← 回车用默认 100
  › 高级子句（回车=跳过；o 排序 / g 分组 / h HAVING）:  ← 回车跳过即不加（见下）

  目标   1 张表  ·  单表
  SELECT * FROM demo_db_00.t_user_setting_00_3 WHERE F_user_id='1000000003' LIMIT 100
  › 回车执行 · n 取消:
  ✓ 执行完成  ·  成功 1  ·  耗时 0.02s

  查询结果  1 行 × 3 列
  ─────────────────────────────────────────────
  来源  t_user_setting_00_3        ← 单表结果：标识列不进表格，顶部一行注明来源
   F_id │ F_user_id  │ F_enabled
  ──────┼────────────┼──────────
   1    │ 1000000003 │ 1

  › q 返回 · s 排序 · v 行详情 · e 导出  回车 q:
```

- 表格宽度**自适应终端**，每行绝不折行；超宽列截断为 `…`，来源列类（`table`）保留尾部（`…ing_78_0`）。
- 列太多/终端太窄时自动切换**竖排详情视图**（逐行卡片，长值自动折行）；横排时输入 `v` + 行号可随时查看某行完整字段。
- 跨库遍历展开表清单用 `information_schema` **一条 SQL 取齐**（非逐库 `SHOW TABLES`），百库展开从数秒降到 1 秒内；不可用时自动回退逐库读取。选表屏会提示「将遍历 N 个库」。
- 多来源结果（整组遍历）自动加一个 `table` 列；单一来源则隐藏 `db_index/table_index/table_name`，顶部注明来源表。
- 「条件」屏输入 `?` 可就地查看所选表的**字段结构**（字段名/类型/允许空/键/默认值/注释），看完自动回到原提示继续输入，无需另开窗口查表结构。

通用键位：`0` 或 `b`=返回上一步，`q`=退出，`p`/`n`=翻页，`回车`=接受默认/继续，`?`=在「条件」屏查看表结构。

一次性非交互查询（熟练用户/脚本）：

```bash
python dbr.py --db=paybase --query='demo_db_00.t_user_setting_00_3 F_user_id=1000000003'
python dbr.py --db=paybase --query='demo_db_{db}.t_x_{db}_{table} F_id,F_user_id | id=1 | 50'
# 分组聚合（多表遍历会跨分表合并为全局结果）
python dbr.py --db=paybase --query='demo_db_{db}.t_user_setting_{db}_{table} F_user_id, COUNT(*) AS c | | NONE | group by F_user_id | order by c DESC'
```

批量值清单查询（值文件 + 字段 → 自动分批 `IN (...)`；交互里在「查询方式」提示处输 `i` 亦可）：

```bash
# 把清单文件放进 <dbr>/data/ 后，直接写文件名即可
python dbr.py --db=paybase \
    --query='demo_db_00.t_user_setting_{db}_{table} F_user_id' \
    --field=F_user_id --file=user_ids.csv
# [信息] 批量值清单：2 个值 · 1 批（每批 ≤1000）· 目标 10 张表 · 共 10 次查询
#   输入 2 值  ·  命中 2  ·  未命中 0  ·  结果 2 行
```

**值清单文件目录**：默认基准目录为 `<dbr>/data/`。交互进入批量模式时会**自动扫描该目录并列成菜单**，
选一个即可，无需输入完整路径（也可在菜单里选手动输入）。`--file=` 写**纯文件名**时也会优先到该目录查找。
用 `--data-dir=<目录>` 可指定其它基准目录。

值文件每行一个值；有无表头自动识别（`--has-header`/`--no-header` 可强制）；`--batch-size` 控每批数量；
多列文件按字段名自动定位列（或用 `--column` 取列）。详见 [`docs/使用手册.md`](docs/使用手册.md) 案例 8。

## 数据源与密码

**默认：直接复用 shell alias**（推荐，最简）。工具会读取当前登录 shell 的 alias
（`~/.bash_profile` / `~/.bashrc`），解析其中所有 `mysql ...` 连接，作为数据源。
密码沿用 alias 里已有的，**不需要再配置任何 password**。

**可选：`--config` 补充**（一般用不到）。仅当你需要「智能缩表」的分库分表规则，
或某些环境没有 alias、想手写数据源时，才用 `--config`：

```bash
/usr/bin/python dbr.py --config=config/example_config.py   # 只加载规则/自定义数据源
/usr/bin/python dbr.py --no-alias --config=xxx.py           # 完全不用 alias
/usr/bin/python dbr.py --no-clear                           # 关闭步骤切换清屏（保留全部滚动历史）
```

`--config` 的 Python 配置（示例见 `config/example_config.py`；同名数据源以 `--config` 为准）：

```python
CONFIG = {
    'databaseConfigs': {   # 可选：无 alias 时手写数据源
        # 'paybase': {'host': '10.0.0.10', 'port': 3306, 'username': 'db_user', 'password': '...'},
    },
    'shardingRules': {     # 可选：仅服务「智能缩表」
        'paybase_user_setting': {
            'shardingType': 'numeric',
            'dbPrefix': 'demo_db_',
            'tablePrefix': 't_user_setting_',
            'routingField': 'F_user_id',
            'routingMethod': 'substring',
            'routingParams': {'dbIndexStart': -3, 'dbIndexLength': 2, 'tableIndexStart': -1},
        },
    },
}
```

**shell alias 是默认数据源**：解析当前 shell 中形如
`alias paybase='mysql -hHOST -PPORT -uUSER -pPASSWORD'` 的连接串，
数据源名即 alias 名（`paybase`、`cifdb`、`vipcifdb` …）。无需配置文件、无需再配密码。

> 安全：不打印密码；建议使用只读账号。

## 命令 / 交互说明

- 通用键位：菜单输入**序号 + 回车**；`0` 或 `b` 返回上一步；`p`/`n` 翻页；`q`/`exit` 取消；`回车` 接受默认。
- **选库 → 选表 两步分离**：选库只列库（逻辑库组 + 单库），选表只列该库的表（逻辑表组 + 单表）；
  各自可输**关键字过滤**、或直接输**库/表名或模板**（含 `{` 或 `.`）直达；
  选中逻辑组后回车=整组遍历，或输入库/表索引（如 `00`、`3`）缩小范围。
- **分步填条件**：`SELECT 字段`（默认 `*`）→ `where 条件`（回车=不加）→ `LIMIT`（默认 100，`NONE` 不限制）→ **高级子句**（回车跳过）。
- **高级子句（排序 / 分组 / 聚合）**：在提示行输入 `o`/`g`/`h` 分别编辑 `ORDER BY` / `GROUP BY` / `HAVING`，可连续编辑，回车结束。
  - `SELECT` 允许完整表达式（含聚合与别名），如 `F_user_id, COUNT(*) AS c`。
  - **多表遍历时自动跨分表合并为全局聚合**：`COUNT/SUM` 相加、`MAX/MIN` 取极值、`AVG` 按 `SUM/COUNT` 加权；`ORDER BY` 在合并后**全局排序**；`HAVING` 在合并后判定。
  - `GROUP_CONCAT`、`COUNT(DISTINCT ...)`、`STDDEV` 等**无法跨表合并**，多表时明确拒绝并提示缩到单表（选具体库/表索引，或配 `routingField` 规则自动缩表）。
  - 提示：多表时 `LIMIT` 为**每表上限**（非全局），预览区会标注。
- **预览即执行**：回车执行，`n` 取消；若 where 命中路由字段，自动缩表并在预览标注。
- **结果操作**：快捷指令 `s` 排序（提示可选列）/ `v` 行详情（竖排看完整字段）/ `e` 导出（含来源列全量）/ `q` 返回。
- 占位符 `{db}` / `{table}` 展开范围 = 实际 `SHOW DATABASES` / `SHOW TABLES` 的真实集合。
- 目标数 > 200 时明显警示。

## 一键部署（install.sh）

```bash
./install.sh                          # 交互式引导（探测 Python 2.7 / 建软链 ~/bin/dbr / 校验 / PATH 保障）
DBR_PYTHON=/路径/python ./install.sh   # 非交互
./install.sh --help
```

部署后 `dbr` 即作为**系统指令**在任意目录可用（`dbr.sh` 内部用 `readlink -f` 解析软链真实路径）。

install.sh 的 PATH 保障逻辑：

- 软链默认放在 `~/bin/dbr`；若该目录**已在 PATH**（含 `~/.bash_profile`/`~/.bashrc` 里的持久配置），直接用 `dbr` 即可；
- 若不在 PATH，自动向 shell 启动文件追加 `export PATH="<目录>:$PATH"`（幂等，不重复追加），并提示重开终端生效；
- 兼容 `$HOME` 未设置的环境（自动用 passwd 记录兜底）。

若中途对软链位置做了改动，重新执行 `./install.sh` 并接受默认值即可恢复为 `~/bin/dbr`。

## 目录结构

```
db-retrieval-cli-py/
├── dbr.py                   # 入口（默认用 alias；--config 可选）
├── dbr_bootstrap.py         # 引导（供脚本复用）
├── dbr.sh                   # 启动脚本（探测 Python 2.7）
├── install.sh               # 交互式部署脚本
├── config/example_config.py # 可选配置示例（shardingRules / 自定义数据源）
├── data/                    # 值清单文件目录（批量 IN 查询默认扫描此处）
├── core/
│   ├── exceptions.py        # DbrError/QueryError/ExportError/RowLimitExceededError
│   ├── sql_client.py        # subprocess 调 mysql 客户端（latin1/GBK -> UTF-8）
│   ├── password_reader.py   # 读密码文件首行
│   ├── sharding_config.py   # 配置容器
│   ├── config_loader.py     # 加载配置 + alias 解析
│   ├── sharding_router.py   # 分库分表路由（numeric/date/智能路由）
│   ├── route_computation.py # 业务值 -> 库表
│   ├── query_result.py      # 结果封装
│   ├── sql_builder.py       # 唯一 SQL 生成器（QueryParts/build_sql/顶层逗号切分）
│   ├── aggregate_merger.py  # 跨分表聚合合并（COUNT/SUM/MAX/MIN/AVG + HAVING/排序）
│   ├── query_executor.py    # 单/批量执行、占位符渲染、重试（含值清单 where_list）
│   ├── value_list_loader.py # 批量值清单加载（表头自动识别/去重/多列取列）
│   ├── data_dir.py          # 值清单基准目录（扫描/路径解析）
│   ├── in_batcher.py        # 值清单 -> WHERE 字段 IN (...) 分批（字段校验+转义）
│   ├── data_aggregator.py   # sum/count/avg/collect
│   ├── result_exporter.py   # text/csv/markdown/html + 行数限制
│   ├── csv_parser.py        # CSV 解析
│   └── batch_query_executor.py # CSV 批量查询
└── controller/
    ├── tui.py               # 终端原语（颜色/菜单/面包屑/隐藏输入/关键字过滤/快捷指令）
    ├── table_renderer.py    # 终端对齐表格/分页/排序/转码
    ├── db_connector.py      # 连接生命周期 + 密码引导
    └── repl.py              # 交互主循环（选库 → 选表 → 字段/where/LIMIT → 预览执行 → 结果快捷操作）
```

## 与 PHP 版的差异

- 运行时从 HHVM/PHP CLI 换成 **系统 Python 2.7**，无需 HHVM/PHP。
- 数据库访问从 `mysqli` 长连接改为 **`subprocess` 调 `mysql` 客户端**（每次查询一个子进程）。
- **数据源默认直接复用 shell alias**，无需配置文件、无需再配密码。
- 配置（可选）从 PHP 数组改为 **Python dict**；**不支持直接 `--config=*.php`**（Python 无法 `require` PHP）。
- 其余行为（路由算法、占位符展开、智能缩表、分组选择、终端表格、导出四格式、行数限制）保持一致。

## 端到端实测用例（测试机 paybase 实测通过）

环境：`10.0.0.10:3306`（`db_user`），Python 2.7.5。

| F_user_id | 库.表 |
|-----------|-------|
| 1000000003 | demo_db_00.t_user_setting_00_3 |
| 1000000015 | demo_db_01.t_user_setting_01_5 |
| 1000000035 | demo_db_03.t_user_setting_03_5 |

- **CASE 1**（单库 + 表占位符 + where 精确 + 智能缩表）：命中 `00_3`，1 行 ✓
- **CASE 2**（`demo_db_0{db}` 跨 10 库 × 10 表 = 100 目标）：智能缩表命中 `03_5` ✓；
  拒绝缩表时全量遍历 100 表，成功 100/失败 0，约 2.5s ✓
- **CASE 3**（单库单表）：命中 `01_5`，1 行 ✓
- **导出**：csv/md/html/txt 四格式写出 UTF-8，列序正确 ✓
- **排序**：按列升/降序 ✓
- **表结构**：条件屏 `SELECT 字段`/`where 条件` 处输 `?` 展示所选表字段（字段/类型/允许空/键/默认/注释，含中文注释）；表组 10 张分表结构一致时只展示一次 ✓
- **数据源**：默认解析测试机 5 个 mysql alias（paybase/cifdb/vipcifdb/statdb/miscdb）并成功连接 ✓
- **错误路径**：错误密码 → `连接失败 (...): ERROR 1045 Access denied` ✓

## FAQ

### 报「未找到可用的 mysql 客户端」
安装 mysql/mariadb 客户端，或将其加入 PATH；确认 `/bin/mysql` 可执行。

### 报「未在当前 shell 中找到 mysql alias 数据源」
说明当前 shell 没有可解析的 `mysql` alias。请在 `~/.bash_profile` 加一行
`alias paybase='mysql -hHOST -PPORT -uUSER -pPASSWORD'` 后重开终端；或用 `--config` 指定数据源。

### 中文显示乱码
工具默认以 `--default-character-set=utf8` 连接并做 GBK 兜底转码；若仍乱码，请确认终端为 UTF-8。

### `--config` 指向 .php 报错
Python 版不解析 PHP 配置，请改写为 Python dict 配置（示例 `config/example_config.py`），或直接用 alias。
