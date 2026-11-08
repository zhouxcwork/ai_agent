# 登录会话模块测试补充与失败用例修复

## 问题定位
`src/auth/session-policy.ts` 中的 `isSessionExpired` 实现有缺陷：

```ts
const expiresOn = session.expiresAt.slice(0, 10);
const currentDay = now.toISOString().slice(0, 10);
return expiresOn < currentDay;
```

它只比较日期字符串的前 10 位（`YYYY-MM-DD`），即按"天"粒度判断，导致同一天内已经过期的会话不会被判为过期。
因此边界用例（到期时刻、到期后 1ms）全部失败。

## 修复方案
改为按精确时间戳比较：

```ts
export function isSessionExpired(session: ExpiringSession, now: Date): boolean {
  const expiresAtMs = Date.parse(session.expiresAt);

  if (Number.isNaN(expiresAtMs)) {
    // 无法解析的过期时间按已过期处理，避免会话被无限期使用。
    return true;
  }

  return now.getTime() >= expiresAtMs;
}
```

语义：`expiresAt <= now` 即视为已过期（到期时刻本身过期），无效时间视为过期（fail-safe）。

## 测试补充
`tests/session-policy.test.ts`、`tests/session-boundary.test.ts` 覆盖三类边界：

| 用例 | now 相对 expiresAt | 期望 |
| --- | --- | --- |
| 到期前仍然有效 | -0.001s | false |
| 到期时刻视为已过期 | 0 | true |
| 到期后立即失效 | +0.001s | true |
| 会话在过期时刻之前仍然有效 | -15min | false |
| 会话在过期时刻之后立即失效 | +15min | true |

## 验证结果
`run_test`（scope=default，命令 `node --import tsx --test "tests/**/*.test.ts"`）：**PASS，exit_code=0**

```
✔ 登录成功后返回会话令牌
✔ 密码错误时登录失败
✔ 到期前仍然有效
✔ 到期时刻视为已过期
✔ 到期后立即失效
✔ 会话在过期时刻之后立即失效
✔ 会话在过期时刻之前仍然有效
ℹ tests 7  ℹ pass 7  ℹ fail 0
```
