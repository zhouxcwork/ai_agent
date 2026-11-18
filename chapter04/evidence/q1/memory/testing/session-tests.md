---
type: concept
title: 登录会话测试
description: 登录会话测试的入口、运行命令与覆盖范围
tags: [login, session, testing]
generated:
  by: project-memory/evidence.0.0
  at: "2026-11-18T01:34:06.930Z"
---

# 登录会话测试

## 测试入口

<code>tests/auth/test_session.py</code>

## 运行命令

在 login_demo 根目录执行。首次下载后按 TESTING.md 安装依赖并构建。

```bash
python3 -m unittest -v tests.auth.test_session
```

## 覆盖范围

- 正确登录后用 accessToken 访问 /api/profile 返回 200 且 userId 为 u_1001
- 无令牌访问 /api/profile 返回 401
- 错误密码登录 /api/login 返回 401

## 最近验证

先 npm run build 成功；python3 -m unittest -v tests.auth.test_session 输出 Ran 3 tests in 0.144s, OK（3 项全通过）；python3 -m unittest discover -s tests -v 也通过（Ran 3 tests in 0.109s）

## 来源

- <code>tests/auth/test_session.py</code>
- <code>TESTING.md</code>
- <code>package.json</code>

<!-- project-memory: {"testPath":"tests/auth/test_session.py","command":"python3 -m unittest -v tests.auth.test_session","sources":["tests/auth/test_session.py","TESTING.md","package.json"],"coverage":["正确登录后用 accessToken 访问 /api/profile 返回 200 且 userId 为 u_1001","无令牌访问 /api/profile 返回 401","错误密码登录 /api/login 返回 401"],"verification":"先 npm run build 成功；python3 -m unittest -v tests.auth.test_session 输出 Ran 3 tests in 0.144s, OK（3 项全通过）；python3 -m unittest discover -s tests -v 也通过（Ran 3 tests in 0.109s）"} -->
