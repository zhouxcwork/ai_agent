# 登录会话测试

从项目根目录安装依赖并构建应用：

```bash
npm ci
npm run build
```

测试入口为 `test_session.py`，在项目根目录运行：

```bash
python -m unittest -v test_session.py
```

测试会启动本项目的 Express 应用，覆盖正确登录后访问个人资料、无令牌访问被拒绝、错误密码登录被拒绝三个场景。
