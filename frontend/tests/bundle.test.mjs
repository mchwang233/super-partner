import assert from "node:assert/strict";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { build } from "vite";

// The page loads first and the Markdown renderer (most of the bundle) on its own (markdown.tsx),
// so the first download stays under Vite's 500 kB warning.
test("the Markdown renderer is not in the page's own bundle", async () => {
  const root = fileURLToPath(new URL("..", import.meta.url));
  const result = await build({ root, logLevel: "silent", build: { write: false } });
  const chunks = (Array.isArray(result) ? result : [result]).flatMap(output => output.output).filter(item => item.type === "chunk");
  const entry = chunks.find(chunk => chunk.isEntry);
  assert.ok(entry.code.length < 500_000, `entry is ${entry.code.length} bytes`);
  assert.ok(!entry.moduleIds.some(id => id.includes("react-markdown")));
  assert.ok(chunks.some(chunk => !chunk.isEntry && chunk.moduleIds.some(id => id.includes("react-markdown"))));
});
