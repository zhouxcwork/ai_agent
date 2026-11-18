const assert = require("node:assert/strict");
const { execFile } = require("node:child_process");
const { access, mkdir, mkdtemp, readFile, readdir, realpath, rename, rm, writeFile } = require("node:fs/promises");
const { tmpdir } = require("node:os");
const { join } = require("node:path");
const { test } = require("node:test");
const { promisify } = require("node:util");

const exec = promisify(execFile);
const modulePath = require.resolve("./dist/memory/memory-store.js");
const id = "session-test-entry";
const initial = {
  testPath: "test_session.py",
  command: "python -m unittest -v test_session.py",
  sources: ["test_session.py", "TESTING.md"],
};
const migrated = {
  testPath: "tests/auth/test_session.py",
  command: "python3 -m unittest -v tests.auth.test_session",
  sources: ["tests/auth/test_session.py", "TESTING.md"],
  verification: "3 项通过",
  coverage: ["登录后可访问个人资料"],
};

async function createProject(t) {
  const root = await mkdtemp(join(tmpdir(), "login-demo-memory-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  return realpath(root);
}

async function projectMemory(root, request) {
  const invoke = `
    const { executeMemory } = require(process.argv[1]);
    executeMemory(process.cwd(), JSON.parse(process.argv[2]))
      .then(({ details }) => console.log(JSON.stringify(details)))
      .catch((error) => { console.error(error); process.exitCode = 1; });
  `;
  const { stdout } = await exec(process.execPath, ["-e", invoke, modulePath, JSON.stringify(request)], { cwd: root });
  return JSON.parse(stdout);
}

test("空库查询不会创建记忆目录", async (t) => {
  const root = await createProject(t);
  assert.equal((await projectMemory(root, { action: "read" })).status, "not_found");
  await assert.rejects(access(join(root, "memory")), { code: "ENOENT" });
});

test("写入、跨进程查找、项目隔离、原地更新和删除", async (t) => {
  const root = await createProject(t);
  const other = await createProject(t);
  const memory = join(root, "memory");
  const index = join(memory, "index.md");
  const log = join(memory, "log.md");
  const read = { action: "read", id };
  await writeFile(join(root, initial.testPath), "# initial test\n");
  await writeFile(join(root, "TESTING.md"), initial.command);

  const written = await projectMemory(root, { action: "write", record: initial });
  assert.equal(written.status, "written");
  assert.equal(written.id, id);
  assert.equal(written.storagePath, join(memory, "testing/session-tests.md"));
  assert.match(await readFile(written.storagePath, "utf8"), /type: concept/);
  assert.match(await readFile(index, "utf8"), /testing\/session-tests\.md/);
  assert.match(await readFile(log, "utf8"), /写入「登录会话测试」/);
  assert.equal((await projectMemory(root, { action: "write", record: initial })).status, "already_exists");
  const found = await projectMemory(root, read);
  assert.equal(found.status, "found");
  assert.deepEqual(found.record, initial);
  assert.equal((await projectMemory(other, read)).status, "not_found");
  await assert.rejects(access(join(other, "memory")), { code: "ENOENT" });

  await mkdir(join(root, "tests/auth"), { recursive: true });
  await rename(join(root, initial.testPath), join(root, migrated.testPath));
  await writeFile(join(root, "TESTING.md"), migrated.command);
  const updated = await projectMemory(root, { action: "update", id, record: migrated });
  assert.equal(updated.status, "updated");
  assert.equal(updated.id, id);
  assert.equal(updated.storagePath, written.storagePath);
  assert.deepEqual(await readdir(join(memory, "testing")), ["session-tests.md"]);
  const latest = await projectMemory(root, read);
  assert.equal(latest.status, "found");
  assert.deepEqual(latest.record, migrated);
  assert.match(await readFile(log, "utf8"), /更新「登录会话测试」.*tests\/auth\/test_session\.py/);

  assert.equal((await projectMemory(root, { action: "delete", id })).status, "deleted");
  assert.equal((await projectMemory(root, read)).status, "not_found");
  await assert.rejects(access(written.storagePath), { code: "ENOENT" });
  assert.doesNotMatch(await readFile(index, "utf8"), /session-tests\.md/);
  assert.match(await readFile(log, "utf8"), /删除「登录会话测试」/);
});

test("旧 JSON 可回读，更新后用 Markdown 持久化", async (t) => {
  const root = await createProject(t);
  const legacyDir = join(root, ".pi");
  const legacyPath = join(legacyDir, "project-memory.json");
  await mkdir(legacyDir);
  await writeFile(legacyPath, JSON.stringify({ "chapter04-login-demo-session-tests": initial }));
  const read = { action: "read", id };
  const found = await projectMemory(root, read);
  assert.equal(found.status, "found");
  assert.equal(found.storagePath, legacyPath);
  assert.deepEqual(found.record, initial);
  await assert.rejects(access(join(root, "memory")), { code: "ENOENT" });

  await mkdir(join(root, "tests/auth"), { recursive: true });
  await writeFile(join(root, migrated.testPath), "# migrated test\n");
  await writeFile(join(root, "TESTING.md"), migrated.command);
  const updated = await projectMemory(root, { action: "update", id, record: migrated });
  assert.equal(updated.status, "updated");
  const latest = await projectMemory(root, read);
  assert.equal(latest.status, "found");
  assert.deepEqual(latest.record, migrated);
  assert.equal(latest.storagePath, join(root, "memory/testing/session-tests.md"));
  await assert.rejects(access(legacyPath), { code: "ENOENT" });
  const [backup] = await readdir(legacyDir);
  assert.match(backup, /^project-memory\.json\.imported-/);
  assert.deepEqual(JSON.parse(await readFile(join(legacyDir, backup), "utf8")),
    { "chapter04-login-demo-session-tests": initial });
});
