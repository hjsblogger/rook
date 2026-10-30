/**
 * The smallest MCP server that is actually an MCP server, shared by the three
 * travel servers.
 *
 * JSON-RPC 2.0 over newline-delimited stdin/stdout: `initialize`,
 * `tools/list`, `tools/call`. No SDK, because a dependency here would mean
 * `npm install` between the planner and its first answer.
 *
 * NOTHING may be written to stdout except protocol frames — a stray log line
 * is a parse error at the other end. Diagnostics go to stderr.
 */

import path from "node:path";
import { pathToFileURL } from "node:url";

/** True only when `metaUrl` is the entry script, so a test can import the data. */
export function invokedDirectly(metaUrl) {
  return Boolean(process.argv[1]) && metaUrl === pathToFileURL(path.resolve(process.argv[1])).href;
}

/** A stable small integer from a string: the same route and date always price the same. */
export function hash(text) {
  let h = 2166136261;
  for (const ch of String(text)) {
    h ^= ch.codePointAt(0);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

/** `YYYY-MM-DD` → Date at UTC midnight, or null. */
export function parseDate(value) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
  const d = new Date(`${value}T00:00:00Z`);
  return Number.isNaN(d.getTime()) ? null : d;
}

export function nightsBetween(checkIn, checkOut) {
  const a = parseDate(checkIn);
  const b = parseDate(checkOut);
  if (!a || !b) return null;
  return Math.round((b - a) / 86400000);
}

/** Thrown by a tool for a bad request; reported as an `isError` result, not a crash. */
export class ToolError extends Error {}

export function serve(name, tools) {
  const write = (msg) => process.stdout.write(JSON.stringify(msg) + "\n");
  const reply = (id, result) => write({ jsonrpc: "2.0", id, result });
  const fail = (id, code, message) => write({ jsonrpc: "2.0", id, error: { code, message } });

  let buffer = "";
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", (chunk) => {
    buffer += chunk;
    let nl;
    while ((nl = buffer.indexOf("\n")) !== -1) {
      const line = buffer.slice(0, nl).trim();
      buffer = buffer.slice(nl + 1);
      if (!line) continue;

      let msg;
      try {
        msg = JSON.parse(line);
      } catch {
        process.stderr.write(`${name}: unparseable frame\n`);
        continue;
      }

      // Notifications carry no id and expect no reply.
      const { id, method, params } = msg;
      const isNotification = id === undefined || id === null;

      if (method === "initialize") {
        reply(id, {
          protocolVersion: "2024-11-05",
          capabilities: { tools: {} },
          serverInfo: { name, version: "1.0.0" },
        });
        continue;
      }

      if (isNotification) continue;

      if (method === "tools/list") {
        reply(id, {
          tools: Object.entries(tools).map(([toolName, tool]) => ({
            name: toolName,
            description: tool.description,
            inputSchema: tool.schema,
          })),
        });
        continue;
      }

      if (method === "tools/call") {
        const tool = tools[params?.name];
        if (!tool) {
          fail(id, -32602, `no tool named ${params?.name}`);
          continue;
        }
        try {
          const out = tool.run(params.arguments ?? {});
          // The JSON goes in a text block, which every client can read
          // without negotiating a schema.
          reply(id, { content: [{ type: "text", text: JSON.stringify(out, null, 2) }], isError: false });
        } catch (err) {
          const message = err instanceof ToolError ? err.message : `internal error: ${err?.message ?? err}`;
          reply(id, { content: [{ type: "text", text: JSON.stringify({ error: message }) }], isError: true });
        }
        continue;
      }

      fail(id, -32601, `method not found: ${method}`);
    }
  });

  process.stderr.write(`${name} mcp on stdio\n`);
}
