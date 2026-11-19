# 作业四第一题 · pi 项目长期记忆

[login_demo/](login_demo/) 内置课程 3-1 的完整登录项目（Express 登录服务、TypeScript 源码及 npm 依赖锁文件）。新增的 Python 测试仅使用标准库，通过 HTTP 验证本项目的登录和受保护接口。扩展由 pi 加载，验收使用 pi 1.5.0 和 DeepSeek 的 `deepseek-flash` 模型。复跑前在环境中提供 `DEEPSEEK_API_KEY`，或在 pi 中配置 DeepSeek 凭据。

## 交付文件与项目结构

```text
chapter04/
├── q1-memory/
│   ├── README.md                  # 本说明
│   └── login_demo/
│       ├── src/                   # 沿用课程 3-1 登录服务源码
│       │   ├── app.ts、server.ts  # Express 应用组装和启动入口
│       │   ├── routes/            # 登录和个人资料接口
│       │   ├── services/          # 登录业务逻辑
│       │   ├── middleware/        # 令牌认证
│       │   ├── repositories/、db/ # 用户查询和内存数据
│       │   └── utils/、types/     # 密码、令牌和类型定义
│       ├── package.json           # 课程依赖和构建命令，新增记忆测试入口
│       ├── package-lock.json      # 沿用课程的 npm 依赖锁文件
│       ├── tsconfig.json          # 沿用课程的 TypeScript 构建配置
│       ├── extension.ts           # 本次新增的 pi 长期记忆扩展
│       ├── memory-store.ts        # Markdown 持久化及查询逻辑
│       ├── memory-store.test.cjs  # 无模型的跨进程回归测试
│       ├── tsconfig.memory.json   # 回归测试编译配置
│       ├── test_session.py        # 本次新增的登录会话测试
│       ├── TESTING.md             # 本次新增的测试说明
│       └── .gitignore             # 本次新增的运行产物忽略规则
└── evidence/
    └── q1/                        # 第一题验收证据
        ├── 01-write.png               # 写入与持久化截图
        ├── 02-read.png                # 全新会话读取与核对截图
        ├── 03-update.png              # 迁移、实跑和更新截图
        ├── pi_run_log.log             # 手动复跑的终端日志
        ├── verification.md            # 独立构建、隔离与删除验收
        └── memory/                    # 手跑实际生成的记忆快照
            ├── index.md
            ├── log.md
            └── testing/session-tests.md
```

课程底座来自 3-1 的 `code/chapter03/01/login_demo`。交付版的 `src/`、`package-lock.json` 和 `tsconfig.json` 与课程副本一致，`package.json` 在课程配置上增加了 `test:memory` 脚本，已完整包含在 [login_demo/](login_demo/) 中。课程项目原来没有会话测试，本次新增的 [test_session.py](login_demo/test_session.py) 会启动课程应用，验证实际的登录和认证行为。

交付源码保留迁移前的测试入口。复跑时，才会把它移到 `tests/auth/test_session.py`，并生成 `login_demo/memory/`。

## 下载后运行

进入 [login_demo/](login_demo/)，使用 Node.js 20 或更新版本、npm 和 Python 3.10 或更新版本。首次安装依赖需要 npm 软件包仓库访问权限。

```bash
npm ci
npm run build
python -m unittest -v test_session.py
```

预期 3 项全部通过。可用 `npm start` 单独启动服务，访问 `http://localhost:3001/health`。测试会自行在回环地址启动构建好的应用并选取空闲端口。直接跑测试无需 pi 或模型凭据。

## 长期记忆自动化回归

在 `login_demo/` 运行：

```bash
npm ci
npm run test:memory
```

预期 4 组用例通过。测试使用临时项目目录和独立 Node.js 进程，覆盖空库、写入、跨进程回读、项目隔离、原地更新、删除、旧 JSON 导入，以及正文写入后索引失败的重试恢复。已安装依赖后可离线运行，无需 pi 或 DeepSeek 凭据。下节的 pi 复跑用于验证真实模型对题面指令的执行效果。

## 复跑

进入 `login_demo/` 后，复制样本到临时目录。每次验收用一个新副本。

```bash
run_dir="$(mktemp -d /tmp/login-demo.XXXXXX)"
mkdir "$run_dir/login_demo"
cp -R src "$run_dir/login_demo/"
cp package.json package-lock.json tsconfig.json tsconfig.memory.json extension.ts memory-store.ts memory-store.test.cjs test_session.py TESTING.md "$run_dir/login_demo/"
cd "$run_dir/login_demo"
npm ci && npm run build && python -m unittest -v test_session.py
```

### 按作业原文操作

从准备好的副本根目录启动 pi。在 macOS 上可用以下命令同时保存终端日志，每次退出重启时重复同一命令：

```bash
script -a "$run_dir/pi_run_log.log" pi \
  --model deepseek/deepseek-flash --no-session --no-extensions -e ./extension.ts
```

1. 启动 pi，输入「读取当前项目的会话测试文件和测试说明，将测试路径和运行命令保存到项目长期记忆，附上来源。返回实际保存路径。」
2. 退出 pi，用相同命令启动全新进程，输入「从当前项目长期记忆中查找会话测试入口，再核对当前文件是否存在。」
3. 退出并再次启动 pi，输入「将会话测试迁移到 tests/auth/test_session.py，同步修改测试说明，运行新测试命令。根据实际结果更新原来的长期记忆。」
4. 第 3 步通过后，应能从 `TESTING.md` 与同一篇记忆读到新路径和新命令。本次人工验收用 `python3 -m unittest -v tests.auth.test_session`，迁移前后各 3 项测试通过。可再启动新进程重复第 2 步，检查更新已落盘。删除验证可在验收副本中查询、删除记录，再启动新进程查询，预期返回 `not_found`。

### 记忆文件

第一次写入会话测试记忆时，在 pi 启动目录下生成：

```text
memory/
├── index.md                  # 当前记录链接
├── log.md                    # 按日期记录写入、更新和删除
└── testing/
    └── session-tests.md      # 同一篇会话测试记忆
```

具体记忆用 Markdown 展示路径、命令、来源及实际提供的测试覆盖与结果。页首保留 YAML 元数据，包括 `type`、`title`、`description`、`tags` 和 `generated`。目录和日志用普通 Markdown。工具从同一 Markdown 文件读取末尾的结构化注释。更新覆盖原页并记入日志，删除移除页面及索引链接。空库查询不创建文件。

工具默认识别会话测试记录 `session-test-entry`，也支持指定其他测试记录 ID。已有 `.pi/project-memory.json` 可继续读取，首次成功写操作会把旧记录导入 Markdown，并将 JSON 改名留档。旧记录如果用的是仓库根相对路径，可在项目根目录重新核对后更新。

首次写入时，若正文已落盘但索引或日志写入失败，工具返回 `saved_with_warning`。解除阻塞后对同一 ID 重试写入，工具补齐索引与日志并返回 `recovered`，保留已保存的正文。

## 验收材料

提交时保留 `q1-memory/` 和 `evidence/q1/` 的相对目录结构。本目录的 `login_demo/` 是可从空记忆状态复跑的完整项目。手跑证据来自独立副本，`test-01/` 无需提交。

| 验收项 | 证据 |
| --- | --- |
| 写入 | [截图 1](../evidence/q1/01-write.png) 展示工具返回 `written`、实际保存路径、命令和来源。 |
| 全新会话查询 | [截图 2](../evidence/q1/02-read.png) 展示 `found` 和迁移前文件的存在性核对。 |
| 迁移并更新 | [截图 3](../evidence/q1/03-update.png) 展示迁移、测试通过及原记录返回 `updated`。 |
| 完整痕迹 | [pi 终端日志](../evidence/q1/pi_run_log.log) · [落盘索引](../evidence/q1/memory/index.md) · [变更日志](../evidence/q1/memory/log.md) · [迁移后的记忆](../evidence/q1/memory/testing/session-tests.md)。 |

[补充验收](../evidence/q1/verification.md)记录前一版的独立构建、测试、项目隔离与删除检查。本次修复新增的索引失败恢复用例可用上文的 `npm run test:memory` 复跑。

证据中的绝对路径是实跑位置。记忆正文中的测试路径、命令和来源以 `login_demo` 根目录为基准。
