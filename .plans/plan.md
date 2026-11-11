# 计划：queuelib 离线检查与挽救工具（已完成）

## 背景理解
- `FifoDiskQueue`：目录内 `q00000`、`q00001`… 块文件，记录格式为 4 字节大端无符号长度头 + 数据；`info.json` 保存 `chunksize/size/tail[num,cnt,offset]/head[num,pos]`。
- `LifoDiskQueue`：单文件，首 4 字节为计数，之后每条 `data + 4字节长度头`。
- 断电后果：尾部截断、info.json 过期（完整尾记录未被元数据引用）、块缺失等。
- 约束：只读检查原目录；绝不猜测字节；完整记录才可导出；可疑区域显式列出；正常队列完全无损。

## 步骤
1. [完成] 新建 `queuelib/recovery.py`：数据类 + FIFO/LIFO 只读扫描器 + 报告渲染 + 导出函数。
2. [完成] 新建 CLI 入口 `queuelib/__main__.py`：`inspect` 与 `recover` 子命令。
3. [完成] 新建 `queuelib/tests/test_recovery.py`：48 个测试覆盖全部故障副本与无损约束。
4. [完成] 更新 `README.rst` 与 `NEWS`。
5. [完成] pytest（354 通过）、ruff、mypy 全部通过。

## 关键决策
- 扫描严格按长度头顺序解析；任何剩余字节无法构成完整记录即标记 `truncated`，停止猜测。
- 导出只写“完整记录”；未被元数据引用的完整尾记录同样可导出（标记来源），截断区与缺失块之后的块记录仍可挽救但报告标注缺失。
- 校验原目录与目标不同路径，且不修改原目录任何字节（测试用快照哈希验证）。
