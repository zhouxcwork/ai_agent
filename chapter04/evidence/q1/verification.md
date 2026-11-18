# 第一题补充验收

三张截图和终端日志来自手动运行。以下检查在提交代码的临时副本中完成。

## 独立构建与测试

| 命令 | 结果 |
| --- | --- |
| `npm ci --no-audit --no-fund` | 成功 |
| `npm run build` | 成功 |
| `python -m unittest -v test_session.py` | 3 项通过 |
| 迁移后 `python3 -m unittest -v tests.auth.test_session` | 3 项通过 |
| 迁移后 `python3 -m unittest discover -s tests -v` | 3 项通过 |

## 项目隔离与删除

使用提交版扩展和 DeepSeek Flash，将手动验收的落盘记忆复制到原项目临时副本。每一步都启动新的 pi 进程。

| 场景 | 工具动作 | 实际状态 |
| --- | --- | --- |
| 原项目读取 | `read` | `found` |
| 另一项目读取 | `read` | `not_found` |
| 原项目删除 | `delete` | `deleted` |
| 全新进程读回删除结果 | `read` | `not_found` |

另一项目仍为空库。删除后原测试记忆文件和索引链接均已移除，变更日志保留删除记录。
