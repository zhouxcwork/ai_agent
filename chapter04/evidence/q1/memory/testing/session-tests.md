---
type: concept
title: 登录会话测试
description: 登录会话测试的入口、运行命令与覆盖范围
tags: [login, session, testing]
generated:
  by: project-memory/1.0.0
  at: "2026-11-19T01:40:33.769Z"
---

# 登录会话测试

## 测试入口

<code>tests/auth/test_session.py</code>

## 运行命令

在 login_demo 根目录执行。首次下载后按 TESTING.md 安装依赖并构建。

```bash
python -m unittest -v tests.auth.test_session
```

## 覆盖范围

- 登录成功后携带 accessToken 访问 /api/profile 返回 200 且 userId=u_1001
- 无令牌访问 /api/profile 返回 401
- 错误密码登录 /api/login 返回 401

## 最近验证

python -m unittest -v tests.auth.test_session 运行 3 项测试全部 OK（0.107s）

## 来源

- <code>tests/auth/test_session.py</code>
- <code>TESTING.md</code>
- <code>tests/__init__.py</code>
- <code>tests/auth/__init__.py</code>

<!-- project-memory: {"testPath":"tests/auth/test_session.py","command":"python -m unittest -v tests.auth.test_session","sources":["tests/auth/test_session.py","TESTING.md","tests/__init__.py","tests/auth/__init__.py"],"coverage":["登录成功后携带 accessToken 访问 /api/profile 返回 200 且 userId=u_1001","无令牌访问 /api/profile 返回 401","错误密码登录 /api/login 返回 401"],"verification":"python -m unittest -v tests.auth.test_session 运行 3 项测试全部 OK（0.107s）"} -->
