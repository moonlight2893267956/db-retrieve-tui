# 值清单文件目录

把批量查询用的值清单文件（`.csv` / `.txt` / `.tsv` / `.list` / `.dat`）放在这里。

在 dbr 里进入「值清单批量 IN」模式时，会**自动扫描本目录并列成菜单**，选一个即可，
无需输入完整路径。

非交互用法中 `--file=` 也可以只写文件名，dbr 会优先到本目录查找：

```bash
dbr --db=paybase --field=F_user_id --file=ids.csv \
    --query='demo_db_00.t_user_setting_{db}_{table} F_user_id'
```

文件格式：每行一个值；第一行若是表头会自动识别并跳过（也可用 `--has-header` / `--no-header` 指定）。
多列文件可按字段名自动定位列，或用 `--column=<N>` 指定。

> 本目录内容默认不纳入版本库（见 `.gitignore`）。用 `--data-dir=<目录>` 可改用其它基准目录。
