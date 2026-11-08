# 登录逻辑说明文档

> 项目：login-demo（Express + TypeScript 演示项目）
> 目标：说明 `POST /api/login` 的完整登录流程、各层职责、Token 机制以及可改进的风险点。

## 1. 整体调用链

```
POST /api/login                       src/routes/login.ts
  └─ authService.login(email, password)        src/services/auth-service.ts
       ├─ userRepository.findByEmail(email)    src/repositories/user-repository.ts
       │    └─ queryOne("users", { email })    src/db/database.ts
       ├─ verifyPassword(password, hash)       src/utils/password.ts
       └─ issueAccessToken({ userId, role })   src/utils/token.ts
```

登录成功后返回 `accessToken`；后续受保护接口（如 `GET /api/profile`）通过
`authMiddleware`（`src/middleware/auth-middleware.ts`）解析 `Authorization: Bearer <token>`。

## 2. 应用装配：`src/app.ts` / `src/server.ts`

- `createApp()` 注册 `express.json()` 中间件。
- 以 `/api` 前缀挂载 `loginRouter` 和 `profileRouter`。
- 另有 `GET /health` 健康检查。
- `src/server.ts` 在 3001 端口启动服务。

## 3. 路由层：`src/routes/login.ts`

- 定义 `loginRouter.post("/login")`，结合前缀得到实际路径 `POST /api/login`。
- 从请求体解构 `email`、`password`。
- **入参校验**：任意一项缺失 → `400 { "error": "email and password are required" }`。
- 调用 `authService.login(email, password)`：
  - 成功 → `200`，响应体为 `LoginResult`。
  - 抛异常 → `401 { "error": <异常 message> }`。

## 4. 业务层：`src/services/auth-service.ts`

`AuthService.login(email, password)` 的执行步骤：

1. `userRepository.findByEmail(email)` 查询用户。
2. 用户不存在 → 抛出 `Error("User not found")`。
3. `verifyPassword(password, user.passwordHash)` 校验密码。
4. 密码不匹配 → 抛出 `Error("Invalid password")`。
5. 校验通过 → `issueAccessToken({ userId: user.id, role: user.role })` 签发访问令牌。
6. 返回：

```ts
{
  accessToken,
  user: { id, email, displayName, role }   // 刻意不带 passwordHash
}
```

`authService` 为单例导出（`export const authService = new AuthService()`）。

## 5. 数据访问层

### `src/repositories/user-repository.ts`

```ts
async findByEmail(email: string) {
  return queryOne("users", { email });
}
```

职责单一：从 `users` 表按 `email` 取出用户记录。

### `src/db/database.ts`

- 内存模拟数据库，仅支持 `users` 表。
- `queryOne(table, where)` 用 `Object.entries(where).every(...)` 做全等匹配，返回第一条命中记录。
- 种子数据：

| id | email | passwordHash | displayName | role |
|---|---|---|---|---|
| u_1001 | demo@example.com | hash:demo123 | Demo User | member |
| u_1002 | admin@example.com | hash:admin123 | Admin User | admin |

## 6. 密码校验：`src/utils/password.ts`

```ts
export async function verifyPassword(plainTextPassword: string, passwordHash: string) {
  return passwordHash === `hash:${plainTextPassword}`;
}
```

即"存储值 = `hash:` + 明文密码"的字符串拼接比对，**并非真实哈希**（无 bcrypt/argon2、无盐、非常量时间比较），仅用于演示。

## 7. Token 机制：`src/utils/token.ts`

- `issueAccessToken(payload)`：`JSON.stringify(payload)` → `base64url`，拼接前缀 `demo-token.`。
- `parseAccessToken(token)`：
  - 不以 `demo-token.` 开头 → 返回 `null`；
  - 去掉前缀后 base64url 解码并 `JSON.parse`，解析失败 → 返回 `null`；
  - 成功 → 返回 `AuthTokenPayload { userId, role }`。
- 本质是**未签名**的编码载荷，客户端可自行伪造 `userId`/`role`。

## 8. 认证中间件：`src/middleware/auth-middleware.ts`

1. 读取 `request.headers.authorization`，必须存在且以 `Bearer ` 开头，否则 → `401 { "error": "Missing bearer token" }`。
2. `parseAccessToken(token)`，解析失败 → `401 { "error": "Invalid token" }`。
3. 成功 → `response.locals.currentUser = payload`，调用 `next()`。

应用位置：`src/routes/profile.ts` 的 `GET /api/profile`，成功时返回 `{ message, currentUser }`。

## 9. 类型定义：`src/types/user.ts`

```ts
interface UserRecord    { id; email; passwordHash; displayName; role: "admin" | "member" }
interface LoginResult   { accessToken: string; user: Pick<UserRecord, "id"|"email"|"displayName"|"role"> }
interface AuthTokenPayload { userId: string; role: UserRecord["role"] }
```

## 10. 响应与状态码一览

| 场景 | 状态码 | 响应体 |
|---|---|---|
| 缺少 email 或 password | 400 | `{ error: "email and password are required" }` |
| 登录成功 | 200 | `{ accessToken, user: {...} }` |
| 用户不存在 | 401 | `{ error: "User not found" }` |
| 密码错误 | 401 | `{ error: "Invalid password" }` |
| 受保护接口缺少 Bearer | 401 | `{ error: "Missing bearer token" }` |
| 受保护接口 token 非法 | 401 | `{ error: "Invalid token" }` |

## 11. 风险点与改进建议

| 项 | 现状 | 建议 |
|---|---|---|
| 认证方式 | 无状态 Bearer token，base64 未签名 | 改用签名 JWT，并设置 `exp` 过期时间 |
| Token 吊销 | 无 | 增加 refresh token、黑名单/版本号 |
| 密码存储 | `hash:` + 明文 | bcrypt / argon2 + 随机盐 |
| 密码比较 | 普通字符串比较 | 使用库提供的常量时间比较 |
| 错误信息 | 区分 "User not found" 与 "Invalid password" | 统一为 "Invalid credentials"，防用户枚举 |
| 防爆破 | 无速率限制 | 增加限流、失败次数锁定、验证码 |
| 日志审计 | 无 | 记录登录成功/失败事件与来源 IP |
| 会话 | 无 session/登出 | 如需登出能力，引入令牌吊销机制 |

## 12. 本地验证方式

```bash
npm run dev                      # 启动 http://localhost:3001
curl -X POST http://localhost:3001/api/login \
  -H "Content-Type: application/json" \
  -d '{"email":"demo@example.com","password":"demo123"}'
```
