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

async function setup(t, records = [mission("finished-task", "succeeded"), mission("running-task", "running")]) {
  const dom = new JSDOM('<div id="root"></div>', {
    url: "http://127.0.0.1:5173", runScripts: "outside-only", pretendToBeVisual: true,
  });
  const window = dom.window;
  const channels = [];
  window.MessageChannel = class extends MessageChannel {
    constructor() { super(); channels.push(this); }
  };
  const state = {
    records,
    deletes: [], cancels: [], nativeConfirms: 0, holdNextRead: false, releaseRead: null,
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
  window.confirm = () => { state.nativeConfirms++; return false; };
  window.IS_REACT_ACT_ENVIRONMENT = true;
  const polls = new Map();
  let timerId = 0;
  window.setInterval = callback => { polls.set(++timerId, callback); return timerId; };
  window.clearInterval = id => polls.delete(id);
  state.poll = () => [...polls.values()][0]();
  window.fetch = async (url, options = {}) => {
    const path = new URL(url).pathname;
    if (options.method === "POST" && path.endsWith("/cancel")) {
      const id = decodeURIComponent(path.slice("/api/tasks/".length, -"/cancel".length));
      state.cancels.push(id);
      return state.cancelResponse(id, options.signal);
    }
    if (options.method === "DELETE") {
      const id = decodeURIComponent(path.slice("/api/tasks/".length));
      state.deletes.push(id);
      return state.deleteResponse(id, options.signal);
    }
    if (path === "/api/tasks") {
      const response = json(state.records);
      if (state.holdNextRead) {
        state.holdNextRead = false;
        return new Promise(resolve => { state.releaseRead = () => resolve(response); });
      }
      return response;
    }
    if (path === "/api/models" || path === "/api/providers") return json([]);
    if (path === "/api/projects/native-picker") return json({ available: false });
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

  await click(document.querySelector(".run"));
  await click(button("删除任务记录", document.querySelector(".detail")));
  await click(button("确认删除", dialog()));
  assert.deepEqual(state.deletes, ["finished-task"]);
  assert.equal(dialog(), null);
  assert.equal(document.querySelectorAll(".run-entry").length, 1);
  assert.doesNotMatch(document.querySelector(".mission-history").textContent, /finished-task/);
  assert.match(document.querySelector(".detail").textContent, /Select a mission/);
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
    await click(document.querySelectorAll(".run")[1]);
    const cancel = button("Cancel mission");
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
    assert.match(document.querySelectorAll(".run-entry")[1].textContent, /cancelled/);
    const detail = document.querySelector(".detail");
    assert.equal(detail.querySelector(".pill").textContent, "cancelled");
    assert.ok(button("删除任务记录", detail));
    assert.equal(button("Cancel mission"), undefined);
    assert.equal(state.records[0].status, "succeeded");
  });
}

test("pending cancellation blocks repeated clicks; server failure stays visible and can be retried", async t => {
  const { state, document, act, dialog, button, click, escape } = await setup(t);
  const cancelResponse = state.cancelResponse;
  let complete;
  state.cancelResponse = () => new Promise(resolve => { complete = resolve; });
  await click(document.querySelectorAll(".run")[1]);
  await click(button("Cancel mission"));
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
  assert.equal(document.querySelector(".detail .pill").textContent, "running");
  assert.ok(!button("确认取消", dialog()).disabled);
  state.cancelResponse = cancelResponse;
  await click(button("确认取消", dialog()));
  assert.equal(dialog(), null);
  assert.equal(state.cancels.length, 2);
  assert.equal(document.querySelector(".detail .pill").textContent, "cancelled");
});

test("a poll started before cancellation cannot restore the running status", async t => {
  const { state, act, document, dialog, button, click } = await setup(t);
  await click(document.querySelectorAll(".run")[1]);
  state.holdNextRead = true;
  await act(async () => { state.poll(); });
  assert.equal(typeof state.releaseRead, "function");
  await click(button("Cancel mission"));
  await click(button("确认取消", dialog()));
  await act(async () => state.releaseRead());
  assert.equal(document.querySelector(".detail .pill").textContent, "cancelled");
  assert.match(document.querySelectorAll(".run-entry")[1].textContent, /cancelled/);
  await act(async () => state.poll());
  assert.equal(document.querySelector(".detail .pill").textContent, "cancelled");
});

test("a task finishing while confirmation is open shows the server conflict without claiming cancellation", async t => {
  const { state, act, document, dialog, button, click } = await setup(t);
  await click(document.querySelectorAll(".run")[1]);
  await click(button("Cancel mission"));
  state.records[1] = mission("running-task", "succeeded");
  await act(async () => state.poll());
  state.cancelResponse = async () => new Response(JSON.stringify({ detail: "Task has already finished" }), { status: 409 });
  await click(button("确认取消", dialog()));
  assert.match(dialog().querySelector('[role="alert"]').textContent, /Task has already finished/);
  assert.equal(document.querySelector(".detail .pill").textContent, "succeeded");
  assert.match(document.querySelector(".detail .result").textContent, /Completed output/);
});

test("cancellation network failure is visible and preserves the task", async t => {
  const { state, window, document, dialog, button, click } = await setup(t);
  state.cancelResponse = async () => { throw new window.TypeError("Failed to fetch"); };
  await click(document.querySelectorAll(".run")[1]);
  await click(button("Cancel mission"));
  await click(button("确认取消", dialog()));
  assert.match(dialog().querySelector('[role="alert"]').textContent, /Failed to fetch/);
  assert.equal(document.querySelector(".detail .pill").textContent, "running");
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
  await click(document.querySelectorAll(".run")[1]);
  await click(button("Cancel mission"));
  await click(button("确认取消", dialog()));
  await act(async () => expire());
  assert.match(dialog().querySelector('[role="alert"]').textContent, /取消请求超时，请检查任务状态后重试/);
  assert.equal(document.querySelector(".detail .pill").textContent, "running");
  assert.ok(!button("确认取消", dialog()).disabled);
});
