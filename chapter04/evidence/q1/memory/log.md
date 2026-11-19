# 记忆变更日志

## 2026-11-19

- 写入「登录会话测试」。
- 会话测试迁移到 tests/auth/test_session.py，运行命令改为 python -m unittest -v tests.auth.test_session（已实跑 3 项全部通过）。
- 更新「登录会话测试」，入口由 `test_session.py` 迁至 `tests/auth/test_session.py`，python -m unittest -v tests.auth.test_session 运行 3 项测试全部 OK（0.107s）。
