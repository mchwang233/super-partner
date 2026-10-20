import assert from "node:assert/strict";
import { before, test } from "node:test";
import { fileURLToPath } from "node:url";
import { MessageChannel } from "node:worker_threads";
import { JSDOM } from "jsdom";
import { build } from "vite";

let browserCode;
before(async () => {
  const appFile = fileURLToPath(new URL("../src.tsx", import.meta.url));
  const testEntry = fileURLToPath(new URL("./virtual-mission-actions-entry.js", import.meta.url));
  const bundle = await build({
    configFile: false,
    logLevel: "silent",
    define: { "process.env.NODE_ENV": '"development"' },
    plugins: [{
      name: "mount-actual-app-for-dom-tests",
      enforce: "pre",
      resolveId(id) { if (id === testEntry) return id; },
      load(id) {
        if (id === testEntry) return `
          import React, { act } from "react";
          import { createRoot } from "react-dom/client";
          import { App } from ${JSON.stringify(appFile)};
          let root;
          window.appTest = {
            act,
            mount() { root = createRoot(document.getElementById("root")); root.render(React.createElement(App)); },
            unmount() { root.unmount(); }
          };
        `;
      },
      transform(code, id) {
        if (id !== appFile) return;
        // Replace only the bootstrap so each test can mount and clean up the actual App.
        const bootstrap = 'createRoot(document.getElementById("root")!).render(<App/>);';
        assert.ok(code.includes(bootstrap));
        return code.replace(bootstrap, "export { App };");
      },
    }],
    build: {
      write: false,
      minify: false,
      lib: { entry: testEntry, formats: ["iife"], name: "MissionActionsTest" },
    },
  });
  const outputs = Array.isArray(bundle) ? bundle : [bundle];
  browserCode = outputs.flatMap(output => output.output).find(item => item.type === "chunk").code;
});

function mission(id, status) {
  return {
    id, status, prompt: id, budget_usd: 1, actual_cost_usd: 0, use_cli: false,
    final_result: status === "succeeded" ? "Completed output" : null,
    plan: { summary: id, nodes: [], planning_method: "heuristic", planning_cost_usd: 0,
      estimate: { low_usd: 0, high_usd: 0 } },
  };
}

async function setup(t, records = [mission("finished-task", "succeeded"), mission("running-task", "running")], models = [], { openComposer = true } = {}) {
  const dom = new JSDOM('<div id="root"></div>', {
    url: "http://127.0.0.1:5173", runScripts: "outside-only", pretendToBeVisual: true,
  });
  const window = dom.window;
  const channels = [];
  window.MessageChannel = class extends MessageChannel {
    constructor() { super(); channels.push(this); }
  };
  const state = {
    records, models, actions: [], resumes: [], publishes: [], uploads: [], attachmentDeletes: [], taskBodies: [], diagnosisReplies: [], leadMessages: [], followups: [], network: [],
    deletes: [], cancels: [], confirms: [], taskPosts: 0, quota: [], quotaResets: [], nativePicker: true, pickerStarts: [], nativeConfirms: 0, holdNextRead: false, releaseRead: null,
  };
  const json = value => new Response(JSON.stringify(value), { headers: { "content-type": "application/json" } });
  state.deleteResponse = async id => {
    state.records = state.records.filter(run => run.id !== id);
    return new Response(null, { status: 204 });
  };
  state.cancelResponse = async id => {
    const updated = { ...state.records.find(run => run.id === id), status: "cancelled" };
    state.records = state.records.map(run => run.id === id ? updated : run);
    return json(updated);
  };
  state.replyResponse = async (id, diagnosisId, body) => {
    const current = state.records.find(run => run.id === id);
    const diagnoses = current.diagnoses.map(item => item.id === diagnosisId
      ? { ...item, messages: [...(item.messages ?? []), { ...body, created_at: "2026-10-15T10:00:00Z" }] } : item);
    diagnoses.push({ id: "reply-diagnosis", trigger: "manual", status: "running", model_id: "cli:claude", reply_to: diagnosisId, created_at: "2026-10-15T10:00:01Z" });
    const updated = { ...current, diagnoses };
    state.records = state.records.map(run => run.id === id ? updated : run);
    return json(updated);
  };
  window.confirm = () => { state.nativeConfirms++; return false; };
  window.IS_REACT_ACT_ENVIRONMENT = true;
  const polls = new Map();
  let timerId = 0;
  window.setInterval = callback => { polls.set(++timerId, callback); return timerId; };
  window.clearInterval = id => polls.delete(id);
  state.poll = () => [...polls.values()][0]();
  window.fetch = async (url, options = {}) => {
    const path = new URL(url).pathname;
    if (path.startsWith("/api/models/quota")) {
      if (options.method === "DELETE") {
        const id = decodeURIComponent(path.slice("/api/models/quota/".length));
        state.quotaResets.push(id);
        state.quota = state.quota.filter(item => item.model_id !== id);
      }
      return json(state.quota);
    }
    if (options.method === "PUT" && path.endsWith("/network")) {
      const id = decodeURIComponent(path.split("/")[3]);
      const { allow } = JSON.parse(options.body);
      state.network.push({ id, allow });
      state.records = state.records.map(item => item.id === id ? { ...item, allow_network: allow } : item);
      return json(state.records.find(item => item.id === id));
    }
    if (options.method === "POST" && path.endsWith("/followup")) {
      const parentId = decodeURIComponent(path.split("/")[3]);
      const body = JSON.parse(options.body);
      state.followups.push({ parentId, ...body });
      const child = { ...mission("change-task", "awaiting_confirmation"), prompt: body.prompt, parent_run_id: parentId, budget_usd: body.budget_usd };
      child.plan = { ...child.plan, summary: "增加到期提醒" };
      state.records = [child, ...state.records];
      return json(child);
    }
    if (options.method === "POST" && path.endsWith("/cancel")) {
      const id = decodeURIComponent(path.slice("/api/tasks/".length, -"/cancel".length));
      state.cancels.push(id);
      return state.cancelResponse(id, options.signal);
    }
    if (options.method === "POST" && path.endsWith("/lead")) {
      const id = decodeURIComponent(path.split("/")[3]);
      const { message } = JSON.parse(options.body);
      state.leadMessages.push({ id, message });
      const current = state.records.find(run => run.id === id);
      const updated = { ...current, lead_messages: [...(current.lead_messages ?? []),
        { id: "m1", role: "user", text: message, status: "done", created_at: "2026-10-16T04:00:00Z" },
        { id: "m2", role: "lead", text: "", status: "pending", created_at: "2026-10-16T04:00:01Z" }] };
      state.records = state.records.map(run => run.id === id ? updated : run);
      return json(updated, 202);
    }
    if (options.method === "POST" && path.endsWith("/reply")) {
      const id = decodeURIComponent(path.split("/")[3]);
      const diagnosisId = decodeURIComponent(path.split("/")[5]);
      const body = JSON.parse(options.body);
      state.diagnosisReplies.push({ id, diagnosisId, ...body });
      return state.replyResponse(id, diagnosisId, body);
    }
    if (options.method === "POST" && (path.endsWith("/pause") || path.endsWith("/diagnose"))) {
      const action = path.endsWith("/pause") ? "pause" : "diagnose";
      const id = decodeURIComponent(path.split("/")[3]);
      state.actions.push([action, id]);
      const current = state.records.find(run => run.id === id);
      const updated = action === "pause" ? { ...current, status: "paused" }
        : { ...current, diagnoses: [...(current.diagnoses ?? []), { id: "d9", trigger: "manual", model_id: "cli:claude", status: "running", created_at: "2026-10-14T07:00:00Z" }] };
      state.records = state.records.map(run => run.id === id ? updated : run);
      return json(updated);
    }
    if (options.method === "POST" && path.endsWith("/resume")) {
      const id = decodeURIComponent(path.slice("/api/tasks/".length, -"/resume".length));
      const body = JSON.parse(options.body);
      state.resumes.push({ id, ...body });
      const updated = { ...state.records.find(run => run.id === id), status: "queued", budget_usd: body.budget_usd };
      state.records = state.records.map(run => run.id === id ? updated : run);
      return json(updated);
    }
    if (options.method === "POST" && path.endsWith("/confirm")) {
      const id = decodeURIComponent(path.slice("/api/tasks/".length, -"/confirm".length));
      const body = JSON.parse(options.body);
      state.confirms.push({ id, ...body });
      const updated = { ...state.records.find(run => run.id === id), status: "queued", budget_usd: body.budget_usd };
      state.records = state.records.map(run => run.id === id ? updated : run);
      return json(updated);
    }
    if (path === "/api/attachments" && options.method === "POST") {
      const name = new URL(url).searchParams.get("name");
      const id = `att${state.uploads.length + 1}`.padEnd(32, "0");
      state.uploads.push(name);
      return new Response(JSON.stringify({ id, name, size: options.body.size, kind: "image", media_type: "image/png" }), { status: 201 });
    }
    if (path.startsWith("/api/attachments/") && options.method === "DELETE") {
      state.attachmentDeletes.push(path.split("/")[3]);
      return new Response(null, { status: 204 });
    }
    if (options.method === "DELETE") {
      const id = decodeURIComponent(path.slice("/api/tasks/".length));
      state.deletes.push(id);
      return state.deleteResponse(id, options.signal);
    }
    if (path === "/api/tasks" && options.method === "POST") { state.taskPosts++; state.taskBodies.push(JSON.parse(options.body)); return json(state.records[0]); }
    if (path === "/api/projects/discover") return json({ projects: [{ name: "shop", path: "/home/me/prj/shop" }], truncated: false });
    if (path === "/api/projects/inspect") {
      const target = new URL(url).searchParams.get("path");
      return target.startsWith("/home/me/")
        ? json({ path: target, git: true, branch: "main", uncommitted_files: 2 })
        : new Response(JSON.stringify({ detail: "Project directory must be inside an allowed root" }), { status: 400 });
    }
    if (path === "/api/projects/native-picker") {
      if (options.method !== "POST") return json({ available: state.nativePicker });
      state.pickerStarts.push(new URL(url).searchParams.get("start"));
      return json({ path: "/home/me/prj/picked" });
    }
    if (path === "/api/tasks") {
      const response = json(state.records);
      if (state.holdNextRead) {
        state.holdNextRead = false;
        return new Promise(resolve => { state.releaseRead = () => resolve(response); });
      }
      return response;
    }
    if (path.endsWith("/timeline")) return json(state.timeline ?? []);
    if (path.endsWith("/files/content")) {
      const file = new URL(url).searchParams.get("path");
      return json({ path: file, text: file.endsWith(".md") ? "## 玩法\n- 方向键移动" : `content of ${file}` });
    }
    if (path.endsWith("/files")) return json({ root: "/work/finished-task", in_project: false, files: [{ path: "tetris/index.html", size: 8423 }, { path: "tetris/game.js", size: 50291 }, { path: "tetris/README.md", size: 900 }], github_url: state.githubUrl ?? null });
    if (path === "/api/github") return json({ token: state.githubToken ?? true, git: true });
    if (path === "/api/github/token" && options.method === "PUT") { state.githubToken = true; return json({ stored: "keyring" }); }
    if (path.endsWith("/github") && options.method === "POST") {
      state.publishes.push(JSON.parse(options.body));
      state.githubUrl = "https://github.com/me/tetris";
      return json({ url: state.githubUrl });
    }
    if (path === "/api/models") return json(state.models);
    if (path === "/api/providers") return json([]);
    if (path === "/api/engineer") return json(state.engineer ?? { available: true, jobs: [] });
    if (path.startsWith("/api/engineer/") && options.method === "POST") {
      const body = JSON.parse(options.body || "{}");
      state.engineerActions = [...(state.engineerActions ?? []), { path, body }];
      const engineer = state.engineer ?? (state.engineer = { available: true, jobs: [] });
      if (path === "/api/engineer/jobs") {
        const job = { id: `job${engineer.jobs.length + 1}`, kind: body.kind, origin: "user", title: body.request.slice(0, 40),
          request: body.request, status: "queued", messages: [], budget_usd: 2, cost_usd: 0,
          created_at: "2026-10-19T08:00:00Z", updated_at: "2026-10-19T08:00:00Z" };
        engineer.jobs = [job, ...engineer.jobs];
        return json(job, 201);
      }
      const [, , , , id, action] = path.split("/");
      const next = { approve: "queued", reject: "rejected", reply: "queued" }[action];
      engineer.jobs = engineer.jobs.map(job => job.id === id ? { ...job, status: next } : job);
      return json(engineer.jobs.find(job => job.id === id));
    }
    throw new Error(`Unexpected request: ${path}`);
  };
  window.eval(browserCode);
  const { act } = window.appTest;
  t.after(async () => {
    await act(async () => window.appTest.unmount());
    for (const channel of channels) { channel.port1.close(); channel.port2.close(); }
    window.close();
  });
  await act(async () => window.appTest.mount());
  const document = window.document;
  const dialog = () => document.querySelector('[role="alertdialog"]');
  const button = (text, root = document) => [...root.querySelectorAll("button")].find(node => node.textContent === text);
  const click = async target => {
    assert.ok(target, "Button must exist");
    await act(async () => { target.focus(); target.click(); });
  };
  const escape = () => act(async () => dialog().dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
  // The new-task form is folded behind the header button once tasks exist; most tests use it.
  if (openComposer && !document.querySelector(".composer")) await click(document.querySelector("header .new-task"));
  return { state, window, document, act, dialog, button, click, escape };
}

test("native dialogs unavailable: list/detail clicks open confirmation, cancel preserves history, confirm deletes", async t => {
  const { state, document, dialog, button, click, escape } = await setup(t);
  const firstDelete = button("删除");
  await click(firstDelete);
  assert.match(dialog().textContent, /finished-task/);
  assert.match(dialog().textContent, /保留本地工程文件/);
  assert.equal(document.activeElement, button("取消", dialog()));
  assert.deepEqual(state.deletes, []);
  await click(button("取消", dialog()));
  assert.equal(dialog(), null);
  assert.equal(document.activeElement, firstDelete);
  await click(firstDelete);
  await escape();
  assert.equal(dialog(), null);
  assert.deepEqual(state.deletes, []);

  await click([...document.querySelectorAll(".run")].find(run => run.title === "finished-task"));
  await click(button("删除任务记录", document.querySelector(".detail")));
  await click(button("确认删除", dialog()));
  assert.deepEqual(state.deletes, ["finished-task"]);
  assert.equal(dialog(), null);
  assert.equal(document.querySelectorAll(".run-entry").length, 1);
  assert.doesNotMatch(document.querySelector(".mission-history").textContent, /finished-task/);
  assert.match(document.querySelector(".detail").textContent, /在左侧选择一个任务/);
  assert.equal(state.nativeConfirms, 0);
});

test("pending delete prevents duplicate requests; server error remains visible and can be retried", async t => {
  const { state, act, document, dialog, button, click, escape } = await setup(t);
  let complete;
  state.deleteResponse = () => new Promise(resolve => { complete = resolve; });
  await click(button("删除"));
  await click(button("确认删除", dialog()));
  assert.equal(dialog().getAttribute("aria-busy"), "true");
  assert.match(dialog().textContent, /正在删除/);
  assert.ok([...dialog().querySelectorAll("button")].every(node => node.disabled));
  await click(button("删除中…", dialog()));
  await escape();
  assert.ok(dialog());
  assert.equal(state.deletes.length, 1);
  await act(async () => complete(new Response(JSON.stringify({ detail: "任务仍在结束处理中" }), { status: 409 })));
  assert.match(dialog().querySelector('[role="alert"]').textContent, /任务仍在结束处理中/);
  assert.equal(document.querySelectorAll(".run-entry").length, 2);
  assert.ok(!button("确认删除", dialog()).disabled);
  state.deleteResponse = async () => new Response(null, { status: 204 });
  await click(button("确认删除", dialog()));
  assert.equal(dialog(), null);
  assert.equal(state.deletes.length, 2);
});

test("an already removed task clears locally when the server returns 404", async t => {
  const { state, document, dialog, button, click } = await setup(t);
  state.deleteResponse = async () => new Response(null, { status: 404 });
  await click(button("删除"));
  await click(button("确认删除", dialog()));
  assert.equal(dialog(), null);
  assert.equal(document.querySelectorAll(".run-entry").length, 1);
});

test("a poll started before deletion cannot restore the removed record", async t => {
  const { state, act, document, dialog, button, click } = await setup(t);
  state.holdNextRead = true;
  await act(async () => { state.poll(); });
  assert.equal(typeof state.releaseRead, "function");
  await click(button("删除"));
  await click(button("确认删除", dialog()));
  await act(async () => state.releaseRead());
  assert.doesNotMatch(document.querySelector(".mission-history").textContent, /finished-task/);
  await act(async () => state.poll());
  assert.equal(document.querySelectorAll(".run-entry").length, 1);
});

test("network failure keeps confirmation and task visible", async t => {
  const { state, window, document, dialog, button, click } = await setup(t);
  state.deleteResponse = async () => { throw new window.TypeError("Failed to fetch"); };
  await click(button("删除"));
  await click(button("确认删除", dialog()));
  assert.match(dialog().querySelector('[role="alert"]').textContent, /Failed to fetch/);
  assert.equal(document.querySelectorAll(".run-entry").length, 2);
  assert.ok(!button("确认删除", dialog()).disabled);
});

test("a stalled request times out with a retryable message", async t => {
  const { state, window, document, act, dialog, button, click } = await setup(t);
  let expire;
  const setTimeout = window.setTimeout.bind(window);
  window.setTimeout = (callback, delay, ...args) => {
    if (delay === 15000) { expire = callback; return -1; }
    return setTimeout(callback, delay, ...args);
  };
  state.deleteResponse = (_, signal) => new Promise((resolve, reject) => {
    signal.addEventListener("abort", () => reject(new window.DOMException("Aborted", "AbortError")), { once: true });
  });
  await click(button("删除"));
  await click(button("确认删除", dialog()));
  await act(async () => expire());
  assert.match(dialog().querySelector('[role="alert"]').textContent, /删除请求超时，请重试/);
  assert.equal(document.querySelectorAll(".run-entry").length, 2);
  assert.ok(!button("确认删除", dialog()).disabled);
});

for (const status of ["awaiting_confirmation", "queued", "running", "paused"]) {
  test(`cancel ${status} mission without native dialogs; dismiss preserves it and confirmation updates both views`, async t => {
    const { state, document, dialog, button, click, escape } = await setup(t, [
      mission("finished-task", "succeeded"), mission("target-task", status),
    ]);
    await click([...document.querySelectorAll(".run")].find(run => run.title === "target-task"));
    const cancel = button("取消任务");
    await click(cancel);
    assert.match(dialog().textContent, /取消任务？/);
    assert.match(dialog().textContent, /target-task/);
    assert.match(dialog().textContent, /保留已有的结果、日志和本地工程文件/);
    assert.equal(document.activeElement, button("返回", dialog()));
    assert.deepEqual(state.cancels, []);
    await click(button("返回", dialog()));
    assert.equal(dialog(), null);
    assert.equal(document.activeElement, cancel);
    await click(cancel);
    await escape();
    assert.equal(dialog(), null);
    assert.deepEqual(state.cancels, []);

    await click(cancel);
    await click(button("确认取消", dialog()));
    assert.deepEqual(state.cancels, ["target-task"]);
    assert.deepEqual(state.deletes, []);
    assert.equal(state.nativeConfirms, 0);
    assert.equal(dialog(), null);
    assert.equal(document.querySelectorAll(".run-entry").length, 2);
    assert.match(document.querySelectorAll(".run-entry")[1].textContent, /已取消/);
    const detail = document.querySelector(".detail");
    assert.equal(detail.querySelector(".pill").textContent, "已取消");
    assert.ok(button("删除任务记录", detail));
    assert.equal(button("取消任务"), undefined);
    assert.equal(state.records[0].status, "succeeded");
  });
}

test("pending cancellation blocks repeated clicks; server failure stays visible and can be retried", async t => {
  const { state, document, act, dialog, button, click, escape } = await setup(t);
  const cancelResponse = state.cancelResponse;
  let complete;
  state.cancelResponse = () => new Promise(resolve => { complete = resolve; });
  await click([...document.querySelectorAll(".run")].find(run => run.title === "running-task"));
  await click(button("取消任务"));
  await click(button("确认取消", dialog()));
  assert.equal(dialog().getAttribute("aria-busy"), "true");
  assert.match(dialog().textContent, /正在取消/);
  assert.ok([...dialog().querySelectorAll("button")].every(node => node.disabled));
  await click(button("取消中…", dialog()));
  await escape();
  assert.ok(dialog());
  assert.equal(state.cancels.length, 1);
  await act(async () => complete(new Response(JSON.stringify({ detail: "取消服务暂时不可用" }), { status: 503 })));
  assert.match(dialog().querySelector('[role="alert"]').textContent, /取消服务暂时不可用/);
  assert.equal(document.querySelector(".detail .pill").textContent, "运行中");
  assert.ok(!button("确认取消", dialog()).disabled);
  state.cancelResponse = cancelResponse;
  await click(button("确认取消", dialog()));
  assert.equal(dialog(), null);
  assert.equal(state.cancels.length, 2);
  assert.equal(document.querySelector(".detail .pill").textContent, "已取消");
});

test("a poll started before cancellation cannot restore the running status", async t => {
  const { state, act, document, dialog, button, click } = await setup(t);
  await click([...document.querySelectorAll(".run")].find(run => run.title === "running-task"));
  state.holdNextRead = true;
  await act(async () => { state.poll(); });
  assert.equal(typeof state.releaseRead, "function");
  await click(button("取消任务"));
  await click(button("确认取消", dialog()));
  await act(async () => state.releaseRead());
  assert.equal(document.querySelector(".detail .pill").textContent, "已取消");
  assert.match(document.querySelectorAll(".run-entry")[1].textContent, /已取消/);
  await act(async () => state.poll());
  assert.equal(document.querySelector(".detail .pill").textContent, "已取消");
});

test("a task finishing while confirmation is open shows the server conflict without claiming cancellation", async t => {
  const { state, act, document, dialog, button, click } = await setup(t);
  await click([...document.querySelectorAll(".run")].find(run => run.title === "running-task"));
  await click(button("取消任务"));
  state.records[1] = mission("running-task", "succeeded");
  await act(async () => state.poll());
  state.cancelResponse = async () => new Response(JSON.stringify({ detail: "Task has already finished" }), { status: 409 });
  await click(button("确认取消", dialog()));
  assert.match(dialog().querySelector('[role="alert"]').textContent, /Task has already finished/);
  assert.equal(document.querySelector(".detail .pill").textContent, "已完成");
  // Finishing switches the task page to its result.
  assert.match(document.querySelector(".detail .result-text").textContent, /Completed output/);
});

test("cancellation network failure is visible and preserves the task", async t => {
  const { state, window, document, dialog, button, click } = await setup(t);
  state.cancelResponse = async () => { throw new window.TypeError("Failed to fetch"); };
  await click([...document.querySelectorAll(".run")].find(run => run.title === "running-task"));
  await click(button("取消任务"));
  await click(button("确认取消", dialog()));
  assert.match(dialog().querySelector('[role="alert"]').textContent, /Failed to fetch/);
  assert.equal(document.querySelector(".detail .pill").textContent, "运行中");
  assert.ok(!button("确认取消", dialog()).disabled);
});

test("cancellation timeout offers recovery and leaves the status unchanged until confirmed by the server", async t => {
  const { state, window, document, act, dialog, button, click } = await setup(t);
  let expire;
  const setTimeout = window.setTimeout.bind(window);
  window.setTimeout = (callback, delay, ...args) => {
    if (delay === 15000) { expire = callback; return -1; }
    return setTimeout(callback, delay, ...args);
  };
  state.cancelResponse = (_, signal) => new Promise((resolve, reject) => {
    signal.addEventListener("abort", () => reject(new window.DOMException("Aborted", "AbortError")), { once: true });
  });
  await click([...document.querySelectorAll(".run")].find(run => run.title === "running-task"));
  await click(button("取消任务"));
  await click(button("确认取消", dialog()));
  await act(async () => expire());
  assert.match(dialog().querySelector('[role="alert"]').textContent, /取消请求超时，请检查任务状态后重试/);
  assert.equal(document.querySelector(".detail .pill").textContent, "运行中");
  assert.ok(!button("确认取消", dialog()).disabled);
});

test("the newest mission opens by default instead of an empty detail panel", async t => {
  const { document } = await setup(t);
  assert.equal(document.querySelector(".detail h3").textContent, "finished-task");
  assert.equal(document.querySelector(".run.active").textContent.includes("finished-task"), true);
});

test("confirming uses the budget entered on the mission, not the new-task form", async t => {
  const { state, window, document, act, button, click } = await setup(t, [mission("draft-task", "awaiting_confirmation")]);
  const setValue = async (input, value) => act(async () => {
    Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set.call(input, value);
    input.dispatchEvent(new window.Event("input", { bubbles: true }));
  });
  await setValue(document.querySelector(".composer .budget-field input"), "9");
  await setValue(document.querySelector(".detail .budget-field input"), "5");
  await click(button("确认并开始执行"));
  assert.deepEqual(state.confirms, [{ id: "draft-task", budget_usd: 5 }]);
  assert.equal(document.querySelector(".detail .pill").textContent, "排队中");
});

const settle = (act, ms) => act(() => new Promise(resolve => setTimeout(resolve, ms)));

test("project field suggests local git projects and reports the chosen project's git state", async t => {
  const { window, document, act, click } = await setup(t);
  const input = document.querySelector('input[aria-label="项目目录"]');
  await act(async () => { input.focus(); input.click(); });
  await settle(act, 0);
  const menu = document.querySelector(".project-menu");
  assert.match(menu.textContent, /本机 git 项目/);
  await click([...menu.querySelectorAll(".project-option")].find(node => node.textContent.includes("shop")));
  assert.equal(input.value, "/home/me/prj/shop");
  assert.equal(document.querySelector(".project-menu"), null);
  await settle(act, 400);
  assert.match(document.querySelector(".project-status").textContent, /分支 main.*2 个未提交改动/);
});

test("a path outside the allowed roots is flagged before the task is submitted", async t => {
  const { window, document, act } = await setup(t);
  const input = document.querySelector('input[aria-label="项目目录"]');
  await act(async () => {
    Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set.call(input, "/etc");
    input.dispatchEvent(new window.Event("input", { bubbles: true }));
  });
  await settle(act, 400);
  assert.match(document.querySelector(".project-status.bad").textContent, /allowed root/);
});

test("one browse button opens the desktop folder dialog at the current folder", async t => {
  const { state, window, document, act, button, click } = await setup(t);
  await settle(act, 0);
  const input = document.querySelector('input[aria-label="项目目录"]');
  await act(async () => {
    Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set.call(input, "/home/me/prj/shop");
    input.dispatchEvent(new window.Event("input", { bubbles: true }));
  });
  assert.equal([...document.querySelectorAll(".project-field button")].filter(node => /浏览|系统/.test(node.textContent)).length, 1);
  await click(button("浏览…"));
  await settle(act, 0);
  assert.deepEqual(state.pickerStarts, ["/home/me/prj/shop"]);
  assert.equal(input.value, "/home/me/prj/picked");
  assert.equal(state.taskPosts, 0);
});

test("the model list opens from the task form and closes on Escape or an outside click", async t => {
  const { state, window, document, act, click } = await setup(t);
  const fab = () => document.querySelector(".composer .fleet-trigger");
  assert.equal(fab().getAttribute("aria-expanded"), "false");
  assert.equal(document.querySelector(".fleet"), null);
  assert.equal(document.querySelector(".grid .fleet"), null);
  await click(fab());
  assert.ok(document.querySelector('.fleet[role="dialog"]'));
  await act(async () => window.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape" })));
  assert.equal(document.querySelector(".fleet"), null);
  await click(fab());
  await act(async () => document.querySelector(".detail").dispatchEvent(new window.MouseEvent("mousedown", { bubbles: true })));
  assert.equal(document.querySelector(".fleet"), null);
  // Inside the task form, the list's own buttons must never submit a task.
  await click(fab());
  assert.ok([...document.querySelectorAll(".fleet button")].every(node => node.type === "button"));
  await click(document.querySelector(".fleet-profiles-link"));
  assert.equal(state.taskPosts, 0);
});

test("opening the model page is not remembered across reloads", async t => {
  const { window, document, click } = await setup(t);
  const nav = label => [...document.querySelectorAll(".app-nav button")].find(node => node.textContent.startsWith(label));
  await click(nav("模型画像"));
  assert.equal(nav("模型画像").getAttribute("aria-pressed"), "true");
  assert.notEqual(window.location.hash, "#models");
  assert.match(window.location.hash, /^#task=/);
});

test("the plan shows stages, dependencies, parallel work and each task's model", async t => {
  const node = (id, deps, model_id = "cli:claude", difficulty = "normal") => ({
    id, title: `Task ${id}`, instructions: "", capability: "coding", dependencies: deps,
    model_id, status: "pending", estimated_cost_usd: 0, difficulty,
    quality_threshold: { easy: 70, normal: 80, hard: 90 }[difficulty],
  });
  const run = mission("planned", "awaiting_confirmation");
  run.plan.nodes = [node("a", [], "cli:claude", "hard"), node("b", ["a"]), node("c", ["a"], "kimi:k3"), node("d", ["b", "c"], "cli:claude", "easy")];
  run.plan.nodes[2].routing_note = "子任务优先用 API 模型，在达标的 API 模型中选最便宜的。代码 88 ≥ 门槛 80。";
  const scores = { coding: 96.7, planning: 96, research: 93.8, chinese: 97, tool_use: 97.6, structured_output: 96, reliability: 88.8 };
  const models = [{ id: "cli:claude", provider: "anthropic", display_name: "Claude Code CLI", channel: "cli", enabled: true, configured: true, scores, scores_basis: "按 AA 换算" }];
  const { document, click } = await setup(t, [run], models);

  assert.match(document.querySelector(".plan-summary").textContent, /4 个子任务.*3 个阶段.*最多 2 项可并行/);
  const stages = [...document.querySelectorAll(".plan-stage")];
  assert.deepEqual(stages.map(stage => stage.querySelectorAll(".plan-node").length), [1, 2, 1]);
  assert.match(stages[1].querySelector(".plan-stage-mode").textContent, /2 项并行/);
  // Cards keep one status line; dependencies are drawn as lines and listed in the drawer.
  assert.ok(document.querySelector(".plan-board svg.plan-links"));
  assert.deepEqual([...document.querySelectorAll(".plan-node .plan-check")].map(line => line.textContent), ["可以开始", "等待 01", "等待 01", "等待 02、03"]);
  assert.equal(document.querySelector(".plan-difficulty"), null);
  await click(stages[2].querySelector(".plan-node-title"));
  const drawer = document.querySelector(".node-progress.drawer");
  assert.deepEqual([...drawer.querySelectorAll(".plan-deps i")].map(i => i.textContent), ["02", "03"]);
  assert.match(drawer.querySelector(".node-progress-facts").textContent, /简单 · 门槛 70/);
  await click(drawer.querySelector('[aria-label="关闭进行情况"]'));
  // The model chip opens its capability chart; the profile link goes to the model page.
  const chips = [...document.querySelectorAll(".plan-model")];
  assert.equal(chips[0].textContent, "Claude Code CLI");
  assert.equal(chips[2].textContent, "kimi:k3");
  assert.ok(chips.every(chip => chip.type === "button"));
  await click(chips[0]);
  const dialog = document.querySelector(".model-capability-modal");
  assert.ok(dialog.querySelector(".profile-radar svg"));
  assert.match(dialog.querySelector(".capability-scores").textContent, /代码97/);
  assert.match(dialog.querySelector(".selection-reason").textContent, /困难任务.*代码分最高.*代码 97 ≥ 门槛 90/);
  assert.match(chips[2].title, /一般任务：子任务优先用 API 模型/);
  await click(dialog.querySelector(".capability-profile-link"));
  assert.equal(document.querySelector(".model-capability-modal"), null);
  assert.equal([...document.querySelectorAll(".app-nav button")].find(b => b.textContent.startsWith("模型画像")).getAttribute("aria-pressed"), "true");
});

test("a plan made in parts shows its major parts as stages; each opens in place, or all at once", async t => {
  const node = (id, part_id, status = "pending", deps = []) => ({
    id, title: `Task ${id}`, instructions: "", capability: "coding", dependencies: deps, part_id,
    model_id: "cli:claude", status, estimated_cost_usd: 0, difficulty: "normal", quality_threshold: 80,
    attempts: status === "succeeded" ? [{ model_id: "cli:claude", outcome: "passed", cost_usd: 0.25 }] : [],
  });
  const run = mission("parted", "running");
  run.plan.parts = [
    { id: "api", title: "后端", goal: "提供待办接口", acceptance: "接口测试通过", parent_id: null, dependencies: [] },
    { id: "store", title: "存储", goal: "保存待办", parent_id: "api", dependencies: [] },
    { id: "ui", title: "前端", goal: "待办页面", parent_id: null, dependencies: ["api"] },
  ];
  run.plan.nodes = [node("model", "store", "succeeded"), node("db", "store", "running", ["model"]),
    node("routes", "api", "pending", ["db"]), node("page", "ui", "pending", ["routes"])];
  const { document, click } = await setup(t, [run]);

  // Only the major parts, as stages from left to right, with how far each has come.
  assert.match(document.querySelector(".plan-summary").textContent, /2 个大项.*4 个任务.*已完成 1/);
  const stages = [...document.querySelectorAll(".plan-stage")];
  assert.deepEqual(stages.map(stage => [...stage.querySelectorAll(".part-card-line b")].map(b => b.textContent)), [["后端"], ["前端"]]);
  assert.ok(document.querySelector(".plan-board svg.plan-links"));
  assert.equal(document.querySelector(".part-task"), null);
  const cards = [...document.querySelectorAll(".part-card")];
  assert.match(cards[0].textContent, /进行中.*1 \/ 3 个任务完成 · 1 个进行中.*1 个子项.*\$0\.25.*展开/);

  // A part opens in place on its sub-parts and tasks, in plan order; numbers are those of the whole plan.
  await click(cards[0].querySelector(".part-card-head"));
  assert.equal(cards[0].querySelector(".part-card-head").getAttribute("aria-expanded"), "true");
  const body = cards[0].querySelector(".part-body");
  assert.deepEqual([...body.children].map(item => item.className.split(" ")[0]), ["part-sub", "part-task"]);
  assert.match(body.querySelector(".part-sub-head").textContent, /1\.1存储1\/2/);
  assert.match(body.querySelector(".part-task").textContent, /03Task routes.*等待 02/);
  await click(body.querySelector(".part-sub-head"));
  assert.deepEqual([...body.querySelectorAll(".part-task .part-task-title span:nth-child(2)")].map(n => n.textContent), ["Task model", "Task db", "Task routes"]);
  await click(body.querySelector(".part-task-title"));
  assert.match(document.querySelector(".node-progress.drawer h4").textContent, /Task model/);
  await click(document.querySelector('[aria-label="关闭进行情况"]'));

  // One switch opens every part, and closes them all again.
  const all = document.querySelector(".part-toggle-all");
  assert.equal(all.textContent, "全部展开");
  await click(all);
  assert.equal(all.textContent, "全部关闭");
  assert.equal(document.querySelectorAll(".part-task").length, 4);
  await click(all);
  assert.equal(document.querySelectorAll(".part-task").length, 0);
});

test("the budget accepts round amounts such as 2 and can be cleared while typing", async t => {
  const { window, document, act } = await setup(t);
  const input = document.querySelector(".composer .budget-field input");
  const type = async value => act(async () => {
    Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set.call(input, value);
    input.dispatchEvent(new window.Event("input", { bubbles: true }));
  });
  for (const amount of ["2", "3", "0.3", "1.25", "10"]) {
    await type(amount);
    assert.equal(input.value, amount);
    assert.ok(input.checkValidity(), `${amount} should be a valid budget`);
  }
  await type("");
  assert.equal(input.value, "");
  assert.ok(document.querySelector(".composer button.primary").disabled);
  await type("2");
  assert.equal(input.value, "2");
  assert.ok(!document.querySelector(".composer button.primary").disabled);
});

test("files can be attached by picking, pasting or dropping, and are sent with the task", async t => {
  const { state, window, document, act, click } = await setup(t);
  const file = (name, type = "image/png") => new window.File([new Uint8Array([137, 80, 78, 71])], name, { type });
  const settle = () => act(async () => { for (let i = 0; i < 5; i++) await new Promise(resolve => window.setTimeout(resolve, 0)); });
  const picker = document.querySelector('.composer input[type="file"]');
  assert.ok(document.querySelector(".attach-button"));
  await act(async () => {
    Object.defineProperty(picker, "files", { configurable: true, value: [file("screen.png"), file("spec.md", "text/markdown")] });
    picker.dispatchEvent(new window.Event("change", { bubbles: true }));
  });
  await settle();
  const textarea = document.querySelector(".composer textarea");
  const paste = new window.Event("paste", { bubbles: true, cancelable: true });
  paste.clipboardData = { files: [file("")] };
  await act(async () => textarea.dispatchEvent(paste));
  const drop = new window.Event("drop", { bubbles: true, cancelable: true });
  drop.dataTransfer = { files: [file("dropped.png")], types: ["Files"] };
  await act(async () => document.querySelector(".composer form").dispatchEvent(drop));
  await settle();

  assert.deepEqual(state.uploads, ["screen.png", "spec.md", "粘贴的图片.png", "dropped.png"]);
  const chips = () => [...document.querySelectorAll(".composer .attachment-chip")];
  assert.equal(chips().length, 4);
  assert.ok(chips().every(chip => chip.classList.contains("ready")));
  await click(chips()[1].querySelector(".attachment-remove"));
  assert.equal(chips().length, 3);
  assert.equal(state.attachmentDeletes.length, 1);

  await act(async () => {
    Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value").set.call(textarea, "Fix the layout in the screenshot");
    textarea.dispatchEvent(new window.Event("input", { bubbles: true }));
  });
  await act(async () => document.querySelector(".composer form").requestSubmit());
  await settle();
  assert.equal(state.taskBodies.length, 1);
  assert.equal(state.taskBodies[0].attachments.length, 3);
  assert.equal(chips().length, 0);
});

test("each task shows its check result, and clicking it shows its progress", async t => {
  const run = mission("checked", "running");
  run.rework_rounds = 1;
  run.plan.nodes = [
    { id: "impl", title: "Implement", capability: "coding", dependencies: [], model_id: "cli:codex", status: "succeeded", estimated_cost_usd: 0,
      acceptance_criteria: "login.py is changed", review_passed: true, review: "Change is in place.", changed_files: ["login.py", "tests/test_login.py"],
      result: "Edited login.py",
      attempts: [
        { model_id: "cli:codex", outcome: "rejected", reason: "Only described the fix.", reviewer_model_id: "cli:claude", cost_usd: 0 },
        { model_id: "cli:claude", outcome: "passed", reason: "Change is in place.", reviewer_model_id: "cli:codex", cost_usd: 0 },
      ] },
    { id: "check", title: "Verify", capability: "coding", dependencies: ["impl"], verifies: ["impl"], model_id: "cli:claude", status: "running", estimated_cost_usd: 0,
      feedback: "tests still fail", inputs: [{ node_id: "impl", form: "summary", changed_files: 2 }], started_at: new Date(Date.now() - 65000).toISOString(),
      attempts: [{ model_id: "cli:claude", outcome: "verify_failed", reason: "验证发现问题：tests still fail", cost_usd: 0 }] },
  ];
  const at = "2026-10-14T07:00:00Z";
  const timeline = [
    { id: 1, kind: "node.started", data: { node_id: "impl", model_id: "cli:codex", attempt: 1 }, created_at: at },
    { id: 2, kind: "node.retry", data: { node_id: "impl", attempt: 2, model_id: "cli:claude", reason: "Only described the fix." }, created_at: at },
    { id: 3, kind: "node.succeeded", data: { node_id: "impl", cost_usd: 0 }, created_at: at },
    { id: 4, kind: "run.rework", data: { round: 1, verifier: "check", reopened: ["impl", "check"], problems: "tests still fail" }, created_at: at },
    { id: 5, kind: "node.started", data: { node_id: "check", attempt: 2 }, created_at: at },
  ];
  const { state, document, click } = await setup(t, [run]);
  state.timeline = timeline;
  assert.match(document.querySelector(".plan-summary").textContent, /重试 1 次.*返工 1 轮/);
  // Stages run left to right.
  assert.deepEqual([...document.querySelectorAll(".plan-stage .plan-stage-head b")].map(b => b.textContent), ["阶段 1", "阶段 2"]);
  const [impl, check] = [...document.querySelectorAll(".plan-node")];
  assert.equal(impl.querySelector(".plan-check").textContent, "✓ 验收通过");
  assert.match(check.querySelector(".plan-check").textContent, /↻ 第 2 次尝试中/);
  assert.equal(document.querySelector(".node-progress"), null);

  await click(impl.querySelector(".plan-node-title"));
  let panel = document.querySelector(".node-progress");
  assert.match(panel.querySelector("h4").textContent, /Implement/);
  assert.match(panel.querySelector(".node-progress-now").textContent, /已完成/);
  assert.deepEqual([...panel.querySelectorAll(".node-progress-timeline li span")].map(li => li.textContent), [
    "开始第 1 次执行（cli:codex）", "未通过，准备第 2 次尝试（换用 cli:claude）：Only described the fix.", "完成 · $0",
    "被 02 退回返工（第 1 轮）：tests still fail",
  ]);
  assert.match(panel.textContent, /login\.py is changed/);
  assert.match(panel.textContent, /改动的文件（2）/);
  assert.deepEqual([...panel.querySelectorAll(".plan-attempts b")].map(b => b.textContent), ["第 1 次 · 验收未通过", "第 2 次 · 通过"]);
  assert.equal(panel.querySelector(".node-progress-output .markdown-body").textContent, "Edited login.py");

  await click(check);
  panel = document.querySelector(".node-progress");
  assert.match(panel.querySelector(".node-progress-now").textContent, /第 2 次执行中 · 已运行 1 分/);
  assert.match(panel.textContent, /01 的摘要（改动 2 个文件）/);
  assert.deepEqual([...panel.querySelectorAll(".plan-verifies i")].map(i => i.textContent), ["01"]);
  assert.match(panel.textContent, /需要修改tests still fail/);
  assert.match(panel.querySelector(".node-progress-timeline").textContent, /验证未通过，第 1 轮返工/);
  await click(panel.querySelector('[aria-label="关闭进行情况"]'));
  assert.equal(document.querySelector(".node-progress"), null);
});

test("a failed task continues from the failed step with the budget entered there", async t => {
  const run = mission("broken", "failed");
  run.error = "1 个步骤失败：Verify（node --check needs approval）";
  run.plan.nodes = [
    { id: "a", title: "Implement", dependencies: [], model_id: "cli:codex", status: "succeeded", estimated_cost_usd: 0 },
    { id: "b", title: "Verify", dependencies: ["a"], model_id: "cli:claude", status: "failed", estimated_cost_usd: 0 },
  ];
  const { state, window, document, act, click } = await setup(t, [run]);
  const bar = document.querySelector(".resume-bar");
  assert.match(bar.textContent, /「Verify」 失败.*已完成的 1 个步骤保留/);
  const input = bar.querySelector("input");
  await act(async () => {
    Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set.call(input, "3");
    input.dispatchEvent(new window.Event("input", { bubbles: true }));
  });
  await click([...bar.querySelectorAll("button")].find(b => b.textContent === "从失败处继续"));
  assert.deepEqual(state.resumes, [{ id: "broken", budget_usd: 3 }]);
  assert.equal(document.querySelector(".resume-bar"), null);
});

test("a finished task opens on its result tab with files, ZIP download and GitHub upload", async t => {
  const { state, window, document, act, click } = await setup(t);
  const tab = label => [...document.querySelectorAll(".task-tabs button")].find(b => b.textContent === label);
  assert.equal(tab("结果").getAttribute("aria-pressed"), "true");
  const page = document.querySelector(".detail .result-page.embedded");
  assert.ok(page);
  assert.match(page.querySelector(".result-text").textContent, /Completed output/);
  const zip = [...page.querySelectorAll("a")].find(a => a.textContent.includes("另存为 ZIP"));
  assert.ok(zip.getAttribute("href").endsWith("/api/tasks/finished-task/archive") && zip.hasAttribute("download"));
  await act(async () => { await new Promise(resolve => window.setTimeout(resolve, 0)); });
  assert.match(page.querySelector(".result-location").textContent, /\/work\/finished-task/);

  await click([...page.querySelectorAll(".result-tabs button")].find(b => b.textContent.startsWith("文件")));
  assert.match(page.querySelector(".result-tabs").textContent, /文件（3）/);
  await click([...page.querySelectorAll(".result-file-list button")][1]);
  await act(async () => { await new Promise(resolve => window.setTimeout(resolve, 0)); });
  assert.equal(page.querySelector(".result-file-view pre").textContent, "content of tetris/game.js");

  await click([...page.querySelectorAll("button")].find(b => b.textContent === "上传到 GitHub"));
  await act(async () => { await new Promise(resolve => window.setTimeout(resolve, 0)); });
  const dialog = document.querySelector(".github-modal");
  const name = dialog.querySelector('[aria-label="仓库名"]');
  await act(async () => {
    Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set.call(name, "tetris");
    name.dispatchEvent(new window.Event("input", { bubbles: true }));
  });
  const submit = [...dialog.querySelectorAll("button")].find(b => b.textContent === "创建私有仓库并上传");
  await click(submit);
  await act(async () => { await new Promise(resolve => window.setTimeout(resolve, 0)); });
  assert.deepEqual(state.publishes, [{ name: "tetris", private: true, description: "finished-task" }]);
  assert.match(document.querySelector(".github-done").textContent, /github\.com\/me\/tetris/);
  await click([...document.querySelectorAll(".github-done button")][0]);
  await act(async () => { await new Promise(resolve => window.setTimeout(resolve, 0)); });
  assert.match(document.querySelector(".result-published").textContent, /github\.com\/me\/tetris/);

  await click(tab("进度"));
  assert.equal(document.querySelector(".result-page"), null);
  assert.ok(document.querySelector(".detail .plan-graph") || document.querySelector(".detail .task-tabs"));
  await click([...document.querySelectorAll(".task-actions button")].find(b => b.textContent === "查看结果"));
  assert.ok(document.querySelector(".detail .result-page"));
});

test("without a saved token the GitHub dialog asks for one or for an existing repository", async t => {
  const { state, window, document, act, click } = await setup(t);
  state.githubToken = false;
  await click([...document.querySelectorAll(".result-page button")].find(b => b.textContent === "上传到 GitHub"));
  await act(async () => { await new Promise(resolve => window.setTimeout(resolve, 0)); });
  const dialog = document.querySelector(".github-modal");
  assert.ok(dialog.querySelector('[aria-label="GitHub 令牌"]'));
  assert.ok([...dialog.querySelectorAll("button")].find(b => b.textContent.includes("创建")).disabled);
  await click([...dialog.querySelectorAll("label")].find(l => l.textContent.includes("已建好的空仓库")).querySelector("input"));
  const remote = dialog.querySelector('[aria-label="仓库地址"]');
  await act(async () => {
    Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set.call(remote, "git@github.com:me/empty.git");
    remote.dispatchEvent(new window.Event("input", { bubbles: true }));
  });
  await click([...dialog.querySelectorAll("button")].find(b => b.textContent === "推送"));
  await act(async () => { await new Promise(resolve => window.setTimeout(resolve, 0)); });
  assert.deepEqual(state.publishes, [{ remote: "git@github.com:me/empty.git" }]);
});

test("the sidebar groups tasks by what they need and folds into a rail it remembers", async t => {
  const withNodes = (run, statuses) => { run.plan.nodes = statuses.map((status, index) => ({ id: `n${index}`, title: `N${index}`, dependencies: [], model_id: "cli:codex", status, estimated_cost_usd: 0 })); return run; };
  const { window, document, click } = await setup(t, [
    withNodes(mission("build-task", "running"), ["succeeded", "running"]), mission("plan-task", "awaiting_confirmation"),
    mission("broken-task", "failed"), mission("finished-task", "succeeded"),
  ], [], { openComposer: false });
  const groups = [...document.querySelectorAll(".run-group")];
  assert.deepEqual(groups.map(group => group.querySelector(".run-group-title").textContent), ["进行中1", "需要处理2", "已结束1"]);
  assert.match(groups[0].querySelector(".run small").textContent, /运行中 · 1\/2/);
  assert.equal(window.document.title, "● (2) Super Partner");

  await click(document.querySelector('.mission-history [title="收起任务栏"]'));
  assert.ok(document.querySelector(".workbench").classList.contains("history-collapsed"));
  const dots = [...document.querySelectorAll(".history-rail .rail-run")];
  assert.equal(dots.length, 4);
  assert.match(dots[2].title, /broken-task · 失败/);
  await click(dots[2]);
  assert.match(document.querySelector(".detail h3").textContent, /broken-task/);
  assert.equal(window.localStorage.getItem("super-partner.history"), "collapsed");
  await click(document.querySelector('.mission-history [title="展开任务栏"]'));
  assert.ok(document.querySelector(".mission-history .run-list"));
  assert.equal(window.localStorage.getItem("super-partner.history"), "open");
});

test("the new-task form stays folded behind the header button once tasks exist", async t => {
  const { document, click } = await setup(t, undefined, [], { openComposer: false });
  assert.equal(document.querySelector(".composer"), null);
  const open = document.querySelector("header .new-task");
  await click(open);
  assert.ok(document.querySelector(".composer textarea"));
  await click(document.querySelector('.composer [aria-label="收起新任务"]'));
  assert.equal(document.querySelector(".composer"), null);
});

test("with no tasks the new-task form is open and cannot be folded away", async t => {
  const { document } = await setup(t, [], [], { openComposer: false });
  assert.ok(document.querySelector(".composer textarea"));
  assert.equal(document.querySelector('.composer [aria-label="收起新任务"]'), null);
});

test("the task page shows its phase, progress and spend, and notices tell when a task finishes", async t => {
  const run = mission("build-task", "running");
  run.budget_usd = 2; run.actual_cost_usd = 0.5;
  run.plan.nodes = [
    { id: "a", title: "A", dependencies: [], model_id: "cli:codex", status: "succeeded", estimated_cost_usd: 0, started_at: new Date(Date.now() - 120000).toISOString() },
    { id: "b", title: "B", dependencies: ["a"], model_id: "cli:codex", status: "running", estimated_cost_usd: 0 },
  ];
  const { state, window, document, act, click } = await setup(t, [run, mission("other-task", "succeeded")], [], { openComposer: false });
  const steps = [...document.querySelectorAll(".task-steps li")];
  assert.deepEqual(steps.map(step => step.className), ["done", "done", "current", "todo"]);
  const metrics = document.querySelector(".task-metrics").textContent;
  assert.match(metrics, /进度1 \/ 2/);
  assert.match(metrics, /用时2 分/);
  assert.match(metrics, /\$0\.5 \/ \$2\.00/);
  assert.ok([...document.querySelectorAll(".task-actions button")].some(b => b.textContent === "取消任务"));

  await click([...document.querySelectorAll(".run")].find(b => b.title === "other-task"));
  state.records[0] = { ...run, status: "succeeded", final_result: "Built it" };
  await act(async () => state.poll());
  const toast = document.querySelector(".toast");
  assert.match(toast.textContent, /「build-task」已完成/);
  await click([...toast.querySelectorAll("button")].find(b => b.textContent === "查看"));
  assert.match(document.querySelector(".detail h3").textContent, /build-task/);
  assert.equal(document.querySelector(".toast"), null);
  assert.equal(window.document.title, "Super Partner");
});

test("Markdown in results and .md files is previewed safely, with the source one click away", async t => {
  const run = mission("doc-task", "succeeded");
  run.final_result = "# 交付说明\n- 已完成 **全部** 功能\n- [ ] 待补充\n\n| 文件 | 行数 |\n|---|---|\n| game.js | 1365 |\n\n<script>window.hacked = true</script><b>raw</b>\n[危险链接](javascript:alert(1)) [文档](https://example.com/doc) [说明](tetris/README.md) [绝对路径](/work/finished-task/tetris/game.js)\n第一行\n第二行";
  const { window, document, act, click } = await setup(t, [run], [], { openComposer: false });
  const view = document.querySelector(".result-text");
  const body = view.querySelector(".markdown-body");
  assert.equal(body.querySelector("h1").textContent, "交付说明");
  assert.equal(body.querySelector("li strong").textContent, "全部");
  assert.ok(body.querySelector('li input[type="checkbox"]'));
  assert.deepEqual([...body.querySelectorAll("td")].map(td => td.textContent), ["game.js", "1365"]);
  assert.equal(body.querySelector("script"), null);
  assert.equal(body.querySelector("b"), null);
  assert.equal(window.hacked, undefined);
  const links = [...body.querySelectorAll("a")];
  assert.ok(links.every(link => !link.getAttribute("href")?.startsWith("javascript")));
  assert.equal(links.find(link => link.textContent === "文档").getAttribute("target"), "_blank");
  assert.ok(body.querySelector("br"), "single line breaks are kept");
  // Links to the task's own files open them in the files tab.
  await act(async () => { await new Promise(resolve => window.setTimeout(resolve, 0)); });
  await click(links.find(link => link.textContent === "绝对路径"));
  await act(async () => { await new Promise(resolve => window.setTimeout(resolve, 0)); });
  assert.equal(document.querySelector(".result-file-name").textContent, "tetris/game.js");
  await click([...document.querySelectorAll(".result-tabs button")].find(b => b.textContent === "最终结果"));
  await click([...document.querySelector(".result-text").querySelectorAll("a")].find(link => link.textContent === "说明"));
  await act(async () => { await new Promise(resolve => window.setTimeout(resolve, 0)); });
  assert.equal(document.querySelector(".result-file-name").textContent, "tetris/README.md");
  await click([...document.querySelectorAll(".result-tabs button")].find(b => b.textContent === "最终结果"));
  await click([...document.querySelector(".result-text").querySelectorAll(".markdown-toggle button")].find(b => b.textContent === "源码"));
  assert.match(document.querySelector(".result-text pre").textContent, /^# 交付说明/);

  const page = document.querySelector(".result-page");
  await click([...page.querySelectorAll(".result-tabs button")].find(b => b.textContent.startsWith("文件")));
  await act(async () => { await new Promise(resolve => window.setTimeout(resolve, 0)); });
  const file = name => [...page.querySelectorAll(".result-file-list button")].find(b => b.textContent.startsWith(name));
  await click(file("tetris/README.md"));
  await act(async () => { await new Promise(resolve => window.setTimeout(resolve, 0)); });
  assert.equal(page.querySelector(".result-file-view .markdown-body h2").textContent, "玩法");
  await click(file("tetris/game.js"));
  await act(async () => { await new Promise(resolve => window.setTimeout(resolve, 0)); });
  assert.equal(page.querySelector(".result-file-view .markdown-body"), null);
  assert.equal(page.querySelector(".result-file-view pre").textContent, "content of tetris/game.js");
});

test("a running task can be paused and diagnosed, and the report shows its advice", async t => {
  const run = mission("build-task", "running");
  run.plan.nodes = [{ id: "a", title: "Check", dependencies: [], model_id: "cli:codex", status: "running", estimated_cost_usd: 0,
    attempts: [{ model_id: "cli:codex", outcome: "error", reason: "node missing", cost_usd: 0 }] }];
  const models = [{ id: "cli:claude", provider: "anthropic", display_name: "Claude Code CLI", channel: "cli", enabled: true, configured: true, scores: null }];
  const { state, document, act, click } = await setup(t, [run], models, { openComposer: false });
  const action = label => [...document.querySelectorAll(".task-actions button")].find(b => b.textContent === label);

  await click(action("暂停"));
  assert.deepEqual(state.actions, [["pause", "build-task"]]);
  assert.equal(document.querySelector(".detail .pill").textContent, "已暂停");
  assert.match(document.querySelector(".action-bar").textContent, /正在运行的 1 个步骤会跑完/);
  assert.equal(action("暂停"), undefined);

  await click(action("诊断"));
  assert.deepEqual(state.actions[1], ["diagnose", "build-task"]);
  assert.ok(action("诊断中…").disabled);
  assert.match(document.querySelector(".diagnosis-card").textContent, /正在由 Claude Code CLI 分析/);

  state.records[0] = { ...state.records[0], status: "failed", diagnoses: [
    { id: "d1", trigger: "auto", node_id: "a", model_id: "cli:claude", status: "done", report: "## 根本原因\n缺少 node", advice: "先确认 node 可用", created_at: "2026-10-14T06:00:00Z" },
    { id: "d2", trigger: "manual", model_id: "cli:claude", status: "failed", error: "CLI timed out", created_at: "2026-10-14T07:00:00Z" },
  ] };
  await act(async () => state.poll());
  const card = () => document.querySelector(".diagnosis-card");
  assert.match(card().textContent, /诊断没有完成：CLI timed out/);
  await click(card().querySelector('[aria-label="上一次诊断"]'));
  assert.match(card().textContent, /自动诊断 · Check/);
  assert.equal(card().querySelector(".markdown-body h2").textContent, "根本原因");
  assert.match(card().querySelector(".diagnosis-advice").textContent, /先确认 node 可用.*从失败处继续/);

  // The environment could not be repaired: the task pauses with the report.
  state.records[0] = { ...state.records[0], status: "paused", error: "步骤「Check」遇到需要你处理的问题，任务已暂停。", diagnoses: [
    { id: "d3", trigger: "auto", node_id: "a", model_id: "cli:claude", status: "done", report: "缺少 node", advice: "先安装 node", verdict: "blocked", created_at: "2026-10-14T08:00:00Z" },
  ] };
  state.records[0].plan.nodes[0].status = "failed";
  await act(async () => state.poll());
  assert.match(card().querySelector(".diagnosis-verdict.warn").textContent, /Claude Code CLI 判断需要你先处理/);
  assert.match(card().querySelector(".diagnosis-advice").textContent, /先安装 node.*按此预算继续/);
  assert.match(document.querySelector(".action-bar").textContent, /遇到需要你处理的问题/);
});


test("models out of quota are listed until they reset, and can be put back by hand", async t => {
  const models = [{ id: "cli:claude", provider: "anthropic", display_name: "Claude Code CLI", channel: "cli", enabled: true, configured: true, scores: null }];
  const { state, document, act, click } = await setup(t, [mission("build-task", "running")], models, { openComposer: false });
  assert.equal(document.querySelector(".quota-banner"), null);

  state.quota = [{ model_id: "cli:claude", until: "2026-10-15T07:00:00Z", reason: "Claude AI usage limit reached" }];
  await act(async () => state.poll());
  const banner = document.querySelector(".quota-banner");
  assert.match(banner.textContent, /模型暂不可用.*Claude Code CLI（额度用完），.*后重试.*自动换用其它可用模型/);

  await click([...banner.querySelectorAll("button")].find(b => b.textContent === "恢复使用"));
  assert.deepEqual(state.quotaResets, ["cli:claude"]);
  assert.equal(document.querySelector(".quota-banner"), null);
});

test("service failures are distinguished from quota exhaustion", async t => {
  const models = [{ id: "deepseek:deepseek-flash", provider: "deepseek", display_name: "DeepSeek", channel: "api", enabled: true, configured: true, scores: null }];
  const { state, document, act } = await setup(t, [mission("build-task", "running")], models, { openComposer: false });
  state.quota = [{ model_id: "deepseek:deepseek-flash", kind: "unavailable", until: "2026-10-15T07:00:00Z", reason: "deepseek returned 503" }];
  await act(async () => state.poll());
  const banner = document.querySelector(".quota-banner");
  assert.match(banner.textContent, /DeepSeek（服务故障）.*后重试/);
  assert.doesNotMatch(banner.textContent, /额度用完/);
});

test("a failed task with a running repair can be cancelled but cannot be resumed concurrently", async t => {
  const run = mission("repair-task", "failed");
  run.diagnoses = [{ id: "d1", trigger: "manual", node_id: "a", status: "running", created_at: "2026-10-15T03:00:00Z" }];
  const { state, document, click } = await setup(t, [run], [], { openComposer: false });
  const resume = [...document.querySelectorAll(".resume-bar button")].find(b => b.textContent === "从失败处继续");
  assert.ok(resume.disabled);
  await click([...document.querySelectorAll(".task-actions button")].find(b => b.textContent === "取消任务"));
  await click([...document.querySelectorAll('[role="alertdialog"] button')].find(b => b.textContent === "确认取消"));
  assert.deepEqual(state.cancels, ["repair-task"]);
  assert.equal(document.querySelector(".detail .pill").textContent, "已取消");
});

test("unverified nodes show the missing verification instead of a pass", async t => {
  const run = mission("unverified-task", "failed");
  run.plan.nodes = [{ id: "v", title: "Verify", dependencies: [], verifies: ["a"], model_id: "x:judge", status: "failed",
    estimated_cost_usd: 0, review_passed: null, review: "验证没有给出 VERDICT，尚未验证。",
    attempts: [{ outcome: "unverified", cost_usd: .01 }] }];
  const { document } = await setup(t, [run], [], { openComposer: false });
  assert.equal(document.querySelector(".plan-check").textContent, "✗ 尚未验证");
  assert.doesNotMatch(document.querySelector(".plan-node").textContent, /✓ 验证通过/);
});

function interactiveDiagnosis(choices = ["保留兼容", "允许升级接口"]) {
  const run = mission("interactive-task", "paused");
  run.waiting_diagnosis_id = "question-1";
  run.diagnoses = [{ id: "question-1", trigger: "auto", status: "done", verdict: "needs_input",
    report: "需要确认兼容性要求。", question: "旧版接口是否需要继续兼容？", choices,
    created_at: "2026-10-15T10:00:00Z" }];
  return run;
}

async function fillDiagnosisReply(context, value) {
  const { window, document, act } = context;
  const textarea = document.querySelector('[aria-label="补充你的想法"]');
  await act(async () => {
    Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value").set.call(textarea, value);
    textarea.dispatchEvent(new window.Event("input", { bubbles: true }));
  });
  return textarea;
}

test("diagnosis accepts both a deliberate choice and Chinese free text, then continues", async t => {
  const context = await setup(t, [interactiveDiagnosis()], [], { openComposer: false });
  const { document, state, click, button } = context;
  const form = document.querySelector('[aria-label="回复诊断"]');
  assert.match(form.textContent, /旧版接口是否需要继续兼容/);
  assert.equal(form.querySelector('input:checked'), null);
  assert.equal(button("发送并继续诊断").disabled, true);
  assert.equal(button("按此预算继续"), undefined);
  await click(form.querySelector('input[value="保留兼容"]'));
  await fillDiagnosisReply(context, "已有三个项目依赖旧接口，不要改调用方。中文与 emoji ✅");
  await click(button("发送并继续诊断"));
  assert.deepEqual(state.diagnosisReplies, [{ id: "interactive-task", diagnosisId: "question-1", choice: "保留兼容", text: "已有三个项目依赖旧接口，不要改调用方。中文与 emoji ✅" }]);
  // The reply and the follow-up diagnosis join the same conversation, with no reply box meanwhile.
  assert.match(document.querySelector(".diagnosis-running").textContent, /你的回复继续诊断/);
  assert.match(document.querySelector('[aria-label="你的诊断回复"]').textContent, /保留兼容.*已有三个项目/s);
  assert.equal(document.querySelector('[aria-label="上一次诊断"]'), null);
  assert.equal(document.querySelector('[aria-label="回复诊断"]'), null);
  assert.match(document.querySelector(".dx-chip").textContent, /分析中/);
});

test("diagnosis permits a free-text alternative without selecting a proposed option", async t => {
  const context = await setup(t, [interactiveDiagnosis()], [], { openComposer: false });
  await fillDiagnosisReply(context, "我希望增加兼容开关，由每个项目自己选择。");
  await context.click(context.button("发送并继续诊断"));
  assert.equal(context.state.diagnosisReplies[0].choice, undefined);
  assert.equal(context.state.diagnosisReplies[0].text, "我希望增加兼容开关，由每个项目自己选择。");
});

test("a failed reply keeps the selected option and text for retry", async t => {
  const context = await setup(t, [interactiveDiagnosis()], [], { openComposer: false });
  const { state, document, click, button } = context;
  state.replyResponse = async () => new Response(JSON.stringify({ detail: "当前诊断仍在进行" }), { status: 409 });
  await click(document.querySelector('input[value="保留兼容"]'));
  await fillDiagnosisReply(context, "补充的要求不要丢失");
  await click(button("发送并继续诊断"));
  assert.equal(document.querySelector('[aria-label="补充你的想法"]').value, "补充的要求不要丢失");
  assert.equal(document.querySelector('input[value="保留兼容"]').checked, true);
  assert.match(document.querySelector('[role="alert"]').textContent, /当前诊断仍在进行/);
});

test("diagnosis reply remains available for legacy reports without choices", async t => {
  const run = interactiveDiagnosis([]);
  delete run.waiting_diagnosis_id;
  delete run.diagnoses[0].question;
  run.diagnoses[0].verdict = "blocked";
  const context = await setup(t, [run], [], { openComposer: false });
  assert.equal(context.document.querySelectorAll('.diagnosis-choices input').length, 0);
  await fillDiagnosisReply(context, "这个权限已经配置好了，请继续检查。");
  await context.click(context.button("发送并继续诊断"));
  assert.equal(context.state.diagnosisReplies.length, 1);
});


test("the diagnosis waiting for an answer is highlighted, and a picked option can be unpicked", async t => {
  const context = await setup(t, [interactiveDiagnosis()], [], { openComposer: false });
  const { document, click, button } = context;
  assert.ok(document.querySelector(".diagnosis-card").classList.contains("waiting"));
  assert.match(document.querySelector(".dx-chip").textContent, /等你回复/);
  assert.match(document.querySelector(".dx-question").textContent, /旧版接口是否需要继续兼容/);
  const option = () => document.querySelector('input[value="允许升级接口"]');
  await click(option());
  assert.equal(option().checked, true);
  assert.equal(button("发送并继续诊断").disabled, false);
  await click(option());
  assert.equal(option().checked, false);
  assert.equal(button("发送并继续诊断").disabled, true);
});


test("an attempt shows its turns, whether it ran out, and where its tool calls went", async t => {
  const run = mission("turns-task", "running");
  run.plan.nodes = [{ id: "a", title: "Frontend", dependencies: [], model_id: "deepseek:deepseek-flash", status: "pending", estimated_cost_usd: 0, feedback: "继续",
    attempts: [{ model_id: "deepseek:deepseek-flash", outcome: "rejected", reason: "只交了进度总结", cost_usd: .2, turns: 60, turn_limit: true,
      tool_calls: [{ turn: 1, tool: "read_file", detail: "easylife/static/app.js", ok: true },
                   { turn: 2, tool: "run_command", detail: "python3 -m unittest", ok: false },
                   { turn: 3, tool: "run_command", detail: "node --check app.js", ok: true }] }] }];
  const { document, click } = await setup(t, [run], [], { openComposer: false });
  await click(document.querySelector(".plan-node-title"));
  const panel = document.querySelector(".node-progress");
  assert.equal(panel.querySelector(".attempt-turns.limit").textContent, "60 轮 · 轮次用完");
  const log = panel.querySelector(".tool-log");
  assert.equal(log.querySelector("summary").textContent, "工具调用 3 次：读文件 1 · 运行命令 2（失败 1）");
  assert.equal(log.querySelectorAll("li.bad").length, 1);
  assert.match(log.querySelector("li.bad").textContent, /第 2 轮.*运行命令.*python3 -m unittest.*失败/);
  assert.deepEqual([...panel.querySelectorAll(".plan-attempts b")].map(b => b.textContent), ["第 1 次 · 验收未通过"]);
});


test("a finished task's result page starts a requirement change that builds on it", async t => {
  const parent = { ...mission("todo-task", "succeeded"), budget_usd: 3 };
  parent.plan = { ...parent.plan, summary: "待办网页" };
  const context = await setup(t, [parent], [], { openComposer: false });
  const { window, document, state, act, click, button } = context;
  const change = button("✎ 需求变更");
  assert.ok(change);
  await click(change);
  const panel = document.querySelector('[aria-label="需求变更"]');
  assert.match(panel.textContent, /沿用同一个工作目录/);
  assert.equal(button("生成变更计划").disabled, true);
  const box = panel.querySelector('[aria-label="新的需求"]');
  await act(async () => {
    Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value").set.call(box, "增加到期提醒");
    box.dispatchEvent(new window.Event("input", { bubbles: true }));
  });
  assert.equal(panel.querySelector('[aria-label="需求变更的预算上限（美元）"]').value, "3");
  await click(button("生成变更计划"));
  assert.deepEqual(state.followups, [{ parentId: "todo-task", prompt: "增加到期提醒", budget_usd: 3, allow_network: false, use_cli: false }]);

  // The new task opens for confirmation, and links back to the task it continues.
  assert.match(document.querySelector(".task-title h3").textContent, /增加到期提醒/);
  const back = document.querySelector(".change-of .link-button");
  assert.match(back.textContent, /待办网页/);
  await click(back);
  assert.match(document.querySelector(".task-title h3").textContent, /待办网页/);
});

test("a running task offers no requirement change", async t => {
  const { button } = await setup(t, [mission("busy-task", "running")], [], { openComposer: false });
  assert.equal(button("✎ 需求变更"), undefined);
});

test("network access is opt-in per task, at planning and before running", async t => {
  const context = await setup(t, [mission("plan-task", "awaiting_confirmation")], [], { openComposer: false });
  const { document, state, click } = context;
  const bar = document.querySelector(".action-bar");
  const toggle = bar.querySelector(".network-switch input");
  assert.equal(toggle.checked, false);
  assert.match(bar.querySelector(".network-switch").title, /本机上的服务仍然访问不到/);
  await click(toggle);
  assert.deepEqual(state.network, [{ id: "plan-task", allow: true }]);
  assert.equal(document.querySelector(".action-bar .network-switch input").checked, true);
});

test("an incomplete task says what could not be done and why, and offers to redo only that", async t => {
  const run = { ...mission("deploy-task", "incomplete"), final_result: "## 未能完成\n\n### 部署上线\n\n沙箱不能联网，没有实际部署。",
    error: "1 个步骤无法完成：部署上线（沙箱不能联网）" };
  run.plan = { ...run.plan, summary: "网站", nodes: [
    { id: "build", title: "构建网站", status: "succeeded", model_id: "x:api", result: "构建完成", dependencies: [] },
    { id: "deploy", title: "部署上线", status: "unable", model_id: "x:api", result: "脚本已就绪", dependencies: ["build"],
      unable_reason: "沙箱不能联网，没有实际部署。\n\n需要你处理：允许本任务联网" },
    { id: "check", title: "检查线上页面", status: "blocked", model_id: "x:api", dependencies: ["deploy"] },
  ] };
  const { document, state, click, button } = await setup(t, [run], [], { openComposer: false });
  assert.equal(document.querySelector(".task-actions .pill").textContent, "未能完成");
  // The result opens first, led by the plain statement of what is missing.
  assert.match(document.querySelector(".result-incomplete").textContent, /任务没有全部完成。.*1 个步骤在当前环境中做不到/);
  assert.match(document.querySelector(".result-text").textContent, /沙箱不能联网，没有实际部署/);
  assert.match(document.querySelector(".resume-bar").textContent, /「部署上线」 在当前环境中无法完成/);
  assert.ok(button("✎ 需求变更"));
  await click(button("重做未完成的步骤"));
  assert.equal(state.resumes.length, 1);
  assert.equal(state.resumes[0].id, "deploy-task");
});

test("a requirement change starts with the earlier task's network and CLI settings, and can change them", async t => {
  const parent = { ...mission("net-task", "succeeded"), allow_network: true, use_cli: true };
  const { window, document, state, act, click, button } = await setup(t, [parent], [], { openComposer: false });
  await click(button("✎ 需求变更"));
  const panel = document.querySelector('[aria-label="需求变更"]');
  const [cli, network] = [...panel.querySelectorAll(".switch input")];
  assert.equal(cli.checked, true);
  assert.equal(network.checked, true);
  assert.match(panel.textContent, /允许联网/);
  await click(network);
  const box = panel.querySelector('[aria-label="新的需求"]');
  await act(async () => {
    Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value").set.call(box, "改成离线版本");
    box.dispatchEvent(new window.Event("input", { bubbles: true }));
  });
  await click(button("生成变更计划"));
  assert.equal(state.followups[0].allow_network, false);
  assert.equal(state.followups[0].use_cli, true);
});

test("the lead panel shows its decisions and questions, takes replies, and lets you write to it", async t => {
  const run = mission("led-task", "paused");
  run.plan.nodes = [{ id: "a", title: "实现", dependencies: [], status: "succeeded", estimated_cost_usd: 0, attempts: [] },
    { id: "v", title: "验证", dependencies: ["a"], status: "pending", estimated_cost_usd: 0, attempts: [] }];
  run.supervisor = { model_id: "cli:codex", calls: 3 };
  run.decisions = [{ id: "d1", node_id: "v", event: "verify_failed", action: "rework", targets: ["a"],
    reason: "这是原有要求中的真实缺陷。", instructions: "到期时按完成结算", created_at: "2026-10-16T03:31:09Z" }];
  run.waiting_diagnosis_id = "q1";
  run.diagnoses = [{ id: "q1", trigger: "lead", node_id: "v", model_id: "cli:codex", status: "done", verdict: "needs_input",
    report: "两种做法都可以。", question: "数据用 SQLite 还是 JSON？", choices: ["SQLite", "JSON"], created_at: "2026-10-16T03:40:00Z" }];
  const models = [{ id: "cli:codex", provider: "openai", display_name: "Codex CLI", channel: "cli", enabled: true, configured: true, scores: null }];
  const context = await setup(t, [run], models, { openComposer: false });
  const { document, state, act, click, button } = context;
  const panel = () => document.querySelector(".lead-panel");
  assert.match(panel().textContent, /负责人Codex CLI.*等你回复/);
  assert.match(panel().textContent, /验证没通过 · 验证.*返工 实现.*这是原有要求中的真实缺陷/);
  assert.match(panel().querySelector(".lead-instructions").textContent, /到期时按完成结算/);
  // The lead's question is answered here, not in a separate diagnosis panel.
  assert.equal(document.querySelectorAll(".diagnosis-card").length, 1);
  assert.match(panel().textContent, /数据用 SQLite 还是 JSON/);
  await click(panel().querySelector('input[value="SQLite"]'));
  await click(button("回复负责人"));
  assert.deepEqual(state.diagnosisReplies, [{ id: "led-task", diagnosisId: "q1", choice: "SQLite", text: "" }]);

  // With nothing to answer, you can write to it; its answer arrives with a later poll.
  state.records[0] = { ...state.records[0], waiting_diagnosis_id: null };
  await act(async () => state.poll());
  const box = document.querySelector('[aria-label="给负责人的消息"]');
  await act(async () => {
    Object.getOwnPropertyDescriptor(context.window.HTMLTextAreaElement.prototype, "value").set.call(box, "为什么要返工？");
    box.dispatchEvent(new context.window.Event("input", { bubbles: true }));
  });
  await click(button("发送"));
  assert.deepEqual(state.leadMessages, [{ id: "led-task", message: "为什么要返工？" }]);
  assert.match(panel().textContent, /为什么要返工？.*负责人正在看任务的情况/s);
  assert.ok(document.querySelector('[aria-label="给负责人的消息"]').closest("fieldset").disabled);
  state.records[0] = { ...state.records[0], lead_messages: [state.records[0].lead_messages[0],
    { ...state.records[0].lead_messages[1], status: "done", text: "因为写出的 **0 秒** 会让数据读不回来。" }] };
  await act(async () => state.poll());
  assert.match(panel().querySelector(".markdown-body").textContent, /0 秒/);
  assert.equal(document.querySelector('[aria-label="给负责人的消息"]').closest("fieldset").disabled, false);
});

test("the lead panel shows the lead deciding, and the repair it asked for under its decision", async t => {
  const run = mission("thinking-task", "running");
  run.plan.nodes = [{ id: "d", title: "发布", dependencies: [], status: "running", estimated_cost_usd: 0, attempts: [] }];
  run.supervisor = { model_id: "cli:codex", calls: 2 };
  run.lead_thinking = { event: "attempt_failed", node_id: "d", started_at: new Date(Date.now() - 45_000).toISOString() };
  run.decisions = [{ id: "d1", node_id: "d", event: "attempt_failed", action: "repair_environment",
    reason: "部署只差 SSH 凭据。", instructions: "把私钥临时放进工作目录", created_at: "2026-10-16T07:19:00Z" }];
  run.diagnoses = [{ id: "r1", trigger: "lead_repair", decision_id: "d1", node_id: "d", model_id: "cli:codex",
    status: "done", verdict: "retry", report: "没找到可读的私钥。", created_at: "2026-10-16T07:19:05Z" }];
  const models = [{ id: "cli:codex", provider: "openai", display_name: "Codex CLI", channel: "cli", enabled: true, configured: true, scores: null }];
  const { document } = await setup(t, [run], models, { openComposer: false });
  const panel = document.querySelector(".lead-panel");
  assert.match(panel.textContent, /判断中/);
  assert.match(panel.querySelector(".lead-thinking").textContent, /尝试失败了，正在判断怎么处理：「发布」 · 已 4\d 秒/);
  const repair = panel.querySelector(".lead-decision .lead-repair");
  assert.match(repair.textContent, /修复Codex CLI未发现环境问题.*修复报告.*结果已交回负责人/s);
  // Its repair is not shown again in the separate diagnosis panel.
  assert.equal(document.querySelectorAll(".diagnosis-card").length, 1);
});

function engineerJob(fields) {
  return { id: "job1", kind: "component", origin: "user", title: "导出按钮", request: "给结果页加一个导出按钮",
    status: "plan_ready", plan: "- 目标: 结果页可以导出 CSV", messages: [], files: [], checks: [], budget_usd: 2, cost_usd: 0,
    created_at: "2026-10-19T08:00:00Z", updated_at: "2026-10-19T08:00:00Z", ...fields };
}

test("the platform engineer page takes requests, shows finished work, and the user approves its plan", async t => {
  const { state, document, click, act, window } = await setup(t, [mission("finished-task", "succeeded")], [], { openComposer: false });
  state.engineer = { available: true, jobs: [engineerJob({}), engineerJob({ id: "job0", title: "修复状态", status: "applied", kind: "repair",
    report: "改好了", files: ["backend/super_partner/executor.py"], checks: ["Backend tests"], diff: "+fixed", commit: "abcdef1234" })] };
  await act(async () => state.poll());
  const nav = [...document.querySelectorAll(".app-nav button")].find(node => node.textContent.startsWith("平台工程师"));
  // One job waits for the user.
  assert.equal(nav.querySelector(".nav-badge").textContent, "1");
  await click(nav);
  const page = document.querySelector(".engineer-page");
  assert.match(page.querySelector(".engineer-job").textContent, /方案待批准.*结果页可以导出 CSV/s);
  await click([...page.querySelectorAll("button")].find(node => node.textContent === "批准方案，开始实施"));
  assert.deepEqual(state.engineerActions.map(item => item.path), ["/api/engineer/jobs/job1/approve"]);
  // A finished repair shows its files, checks, diff and commit.
  await click([...page.querySelectorAll(".engineer-list button")].find(node => node.textContent.includes("修复状态")));
  const detail = document.querySelector(".engineer-job");
  assert.match(detail.textContent, /已生效.*executor\.py.*检查通过：Backend tests.*abcdef12/s);
  // A new request goes to the engineer with its kind.
  await click([...page.querySelectorAll(".engineer-kind label")].find(node => node.textContent === "修复缺陷"));
  const box = page.querySelector(".engineer-request textarea");
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value").set;
    setter.call(box, "暂停后继续一直显示排队中");
    box.dispatchEvent(new window.Event("input", { bubbles: true }));
  });
  await click([...page.querySelectorAll("button")].find(node => node.textContent === "交给平台工程师"));
  const posted = state.engineerActions.at(-1);
  assert.equal(posted.path, "/api/engineer/jobs");
  assert.deepEqual(posted.body, { request: "暂停后继续一直显示排队中", kind: "repair" });
});

test("the lead panel shows a component the lead asked for, and opens it on the engineer page", async t => {
  const run = mission("waiting-task", "paused");
  run.plan.nodes = [{ id: "a", title: "读资料", dependencies: [], status: "pending", estimated_cost_usd: 0, attempts: [] }];
  run.supervisor = { model_id: "cli:claude", calls: 2 };
  run.engineer_job_id = "job1";
  run.decisions = [{ id: "d1", node_id: "a", event: "attempt_failed", action: "request_component", engineer_job_id: "job1",
    reason: "缺一个读 PDF 的工具。", instructions: "给执行模型加一个读 PDF 的工具", created_at: "2026-10-19T07:00:00Z" }];
  const { state, document, click } = await setup(t, [run], [], { openComposer: false });
  state.engineer = { available: true, jobs: [engineerJob({ origin: "lead", run_id: "waiting-task", node_id: "a", title: "PDF 工具" })] };
  const panel = document.querySelector(".lead-panel");
  assert.match(panel.textContent, /请平台工程师添加组件.*任务在等这个组件/s);
  await click([...panel.querySelectorAll("button")].find(node => node.textContent === "查看方案和进度"));
  const detail = document.querySelector(".engineer-job");
  assert.match(detail.textContent, /PDF 工具.*负责人请求.*来自任务.*结束后结果会交回负责人/s);
});
