import { mkdir, mkdtemp, readFile, readdir, rename, rm, writeFile } from "node:fs/promises";
import { dirname, isAbsolute, join } from "node:path";

type MemoryRecord = {
  testPath: string;
  command: string;
  verification?: string;
  coverage?: string[];
  sources: string[];
};

type MemoryRequest = {
  action: "write" | "read" | "update" | "delete";
  id?: string;
  record?: MemoryRecord;
};

const SESSION_ID = "session-test-entry";
const LEGACY_SESSION_ID = "chapter04-login-demo-session-tests";
const validId = /^[a-z0-9][a-z0-9-]*$/;
const pageName = (id: string) => id === SESSION_ID ? "session-tests.md" : `record-${id}.md`;
const titleOf = (id: string) => id === SESSION_ID ? "登录会话测试" : id;
const normalizeId = (id: string) => id === LEGACY_SESSION_ID ? SESSION_ID : id;

function isRecord(value: unknown): value is MemoryRecord {
  if (!value || typeof value !== "object") return false;
  const { testPath, command, sources, coverage, verification } = value as Partial<MemoryRecord>;
  return typeof testPath === "string" && isProjectPath(testPath)
    && typeof command === "string" && command.trim().length > 0
    && (verification === undefined || typeof verification === "string")
    && (coverage === undefined || Array.isArray(coverage) && coverage.every((item) => typeof item === "string"))
    && Array.isArray(sources) && sources.length > 0
    && sources.every((source) => typeof source === "string" && isProjectPath(source));
}

function isProjectPath(path: string): boolean {
  return path.trim().length > 0 && !isAbsolute(path) && !/^[a-z]:|^\\/i.test(path) && !path.split(/[\\/]/).includes("..");
}

async function readOptional(path: string): Promise<string | undefined> {
  try {
    return await readFile(path, "utf8");
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return undefined;
    throw error;
  }
}

async function readLegacy(path: string): Promise<Map<string, MemoryRecord>> {
  const raw = await readOptional(path);
  if (raw === undefined) return new Map();
  const data: unknown = JSON.parse(raw);
  if (!data || typeof data !== "object" || Array.isArray(data)) {
    throw new Error("记忆文件应为按 ID 索引的 JSON 对象。");
  }
  const entries = Object.entries(data);
  if (entries.some(([id, record]) => !validId.test(id) || !isRecord(record))) {
    throw new Error("记忆文件含无效记录。");
  }
  return new Map(entries.map(([id, record]) => [normalizeId(id), record]));
}

async function saveFile(path: string, content: string) {
  const directory = dirname(path);
  await mkdir(directory, { recursive: true });
  const temporary = await mkdtemp(join(directory, ".memory-"));
  try {
    const draft = join(temporary, "draft.md");
    await writeFile(draft, content, { mode: 0o600 });
    await rename(draft, path);
  } finally {
    await rm(temporary, { recursive: true, force: true });
  }
}

async function readPages(directory: string) {
  const names = await readdir(directory).catch((error: NodeJS.ErrnoException) => {
    if (error.code === "ENOENT") return [];
    throw error;
  });
  const entries = new Map<string, MemoryRecord>();
  for (const name of names.filter((name) => name === pageName(SESSION_ID) || /^record-[a-z0-9-]+\.md$/.test(name))) {
    const id = name === pageName(SESSION_ID) ? SESSION_ID : name.slice(7, -3);
    const body = await readFile(join(directory, name), "utf8");
    const data = body.match(/\n<!-- project-memory: (.+) -->\n?$/)?.[1];
    const record: unknown = data && JSON.parse(data);
    if (!isRecord(record)) throw new Error(`记忆文件格式无效：${name}`);
    entries.set(id, record);
  }
  return entries;
}

function renderRecord(id: string, record: MemoryRecord, at: string) {
  const title = titleOf(id);
  const inline = (value: string) => `<code>${value.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")}</code>`;
  const fence = "`".repeat(Math.max(2, ...(record.command.match(/`+/g) ?? []).map((part) => part.length)) + 1);
  const payload = JSON.stringify(record).replaceAll("<", "\\u003c").replaceAll(">", "\\u003e");
  return [
    "---", "type: concept", `title: ${title}`,
    "description: 登录会话测试的入口、运行命令与覆盖范围",
    "tags: [login, session, testing]", "generated:",
    "  by: project-memory/1.0.0", `  at: "${at}"`, "---", "",
    `# ${title}`, "", "## 测试入口", "", inline(record.testPath), "",
    "## 运行命令", "", "在 login_demo 根目录执行。首次下载后按 TESTING.md 安装依赖并构建。", "",
    fence + "bash", record.command, fence, "",
    ...(record.coverage?.length ? ["## 覆盖范围", "", ...record.coverage.map((item) => `- ${item}`), ""] : []),
    ...(record.verification ? ["## 最近验证", "", record.verification, ""] : []),
    "## 来源", "", ...record.sources.map((source) => `- ${inline(source)}`), "",
    `<!-- project-memory: ${payload} -->`, "",
  ].join("\n");
}

async function updateIndexAndLog(root: string, ids: string[], event: string, day: string) {
  const links = ids.sort().map((id) => `- [${titleOf(id)}](testing/${pageName(id)})`);
  await saveFile(join(root, "index.md"), [
    "# login_demo 项目记忆", "", "## 测试", "", ...(links.length ? links : ["暂无记忆。"]),
    "", "[变更日志](log.md)", "",
  ].join("\n"));
  const log = join(root, "log.md");
  const title = "# 记忆变更日志";
  const heading = `## ${day}`;
  const previous = await readOptional(log) ?? `${title}\n`;
  const section = previous.indexOf(`\n${heading}\n`);
  const next = section < 0 ? -1 : previous.indexOf("\n## ", section + 1);
  const logText = section < 0
    ? previous.replace(title, () => `${title}\n\n${heading}\n\n${event}\n`)
    : next < 0 ? `${previous.trimEnd()}\n${event}\n`
    : `${previous.slice(0, next).trimEnd()}\n${event}\n${previous.slice(next)}`;
  await saveFile(log, logText);
}

export async function executeMemory(
  projectRoot: string,
  { action, id: requested, record }: MemoryRequest,
) {
  const id = normalizeId(requested ?? SESSION_ID);
  if (!validId.test(id)) throw new Error("记录 ID 仅支持小写字母、数字和短横线。");
  const root = join(projectRoot, "memory");
  const directory = join(root, "testing");
  const legacyPath = join(projectRoot, ".pi", "project-memory.json");
  const storagePath = join(directory, pageName(id));
  const pages = await readPages(directory);
  const legacy = await readLegacy(legacyPath);
  const records = new Map([...legacy, ...pages]);
  const existing = records.get(id);
  const result = (status: string, saved?: MemoryRecord, path = storagePath, warning?: string) => {
    const details = { status, action, id, storagePath: path, record: saved, warning };
    return { content: [{ type: "text" as const, text: JSON.stringify(details, null, 2) }], details };
  };

  if (action === "read") {
    if (existing) return result("found", existing, pages.has(id) ? storagePath : legacyPath);
    if (requested === undefined && records.size) {
      const details = { status: "listed", ids: [...records.keys()] };
      return { content: [{ type: "text" as const, text: JSON.stringify(details) }], details };
    }
    return result("not_found");
  }
  if (requested === undefined && action === "delete") throw new Error("删除时必须提供记录 ID。");
  const day = new Date().toLocaleDateString("en-CA");
  if (action === "write" && existing) {
    if (!pages.has(id)) return result("already_exists", existing, legacyPath);
    const event = `- 补齐「${titleOf(id)}」的索引与日志。`;
    try {
      const index = await readOptional(join(root, "index.md"));
      const log = await readOptional(join(root, "log.md"));
      const hasLog = log?.split("\n").some((line) =>
        line.startsWith(`- 写入「${titleOf(id)}」`) || line === event
      );
      if (!index?.includes(`](testing/${pageName(id)})`) || !hasLog) {
        await updateIndexAndLog(root, [...pages.keys()], event, day);
        return result("recovered", existing);
      }
      return result("already_exists", existing);
    } catch {
      return result("saved_with_warning", existing, storagePath, "正文已保存，索引或日志未同步，请重试写入同一 ID。");
    }
  }
  if (action !== "write" && !existing) return result("not_found");
  if (action !== "delete") {
    if (!isRecord(record)) throw new Error("请提供项目相对路径、实跑命令和来源。");
    for (const path of [record.testPath, ...record.sources]) {
      if (await readOptional(join(projectRoot, path)) === undefined) {
        throw new Error(`请从 pi 启动目录使用真实的项目相对路径：${path}`);
      }
    }
  }

  const now = new Date();
  const at = now.toISOString();
  for (const [legacyId, saved] of legacy) {
    if (!pages.has(legacyId)) await saveFile(join(directory, pageName(legacyId)), renderRecord(legacyId, saved, at));
  }
  if (legacy.size) await rename(legacyPath, `${legacyPath}.imported-${at.replaceAll(":", "-")}`);
  if (action === "delete") {
    await rm(storagePath);
    records.delete(id);
  } else {
    await saveFile(storagePath, renderRecord(id, record!, at));
    records.set(id, record!);
  }
  const operation = { write: "写入", update: "更新", delete: "删除" }[action];
  const pathChange = existing && record && existing.testPath !== record.testPath
    ? `，入口由 \`${existing.testPath}\` 迁至 \`${record.testPath}\`` : "";
  const imported = legacy.size ? `- 导入旧 JSON 记忆 ${legacy.size} 条。\n` : "";
  const summary = record?.verification ? `，${record.verification.replace(/\s+/g, " ")}` : "";
  const event = `${imported}- ${operation}「${titleOf(id)}」${pathChange}${summary}。`;
  try {
    await updateIndexAndLog(root, [...records.keys()], event, day);
  } catch (error) {
    if (action !== "write") throw error;
    return result("saved_with_warning", record, storagePath, "正文已保存，索引或日志未同步，请重试写入同一 ID。");
  }
  return result({ write: "written", update: "updated", delete: "deleted" }[action], action === "delete" ? undefined : record);
}
