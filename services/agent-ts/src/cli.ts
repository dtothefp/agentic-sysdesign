// CLI transport: stream one agent turn to stdout. The curl-free way to watch the loop, mirroring
// the Python agent's `python -m agent "..."` (packages/agent/agent/__main__.py).
//
//   npm run chat -- "what creators do we track?"
//
// Points at SYSDESIGN_API_URL (default http://localhost:8000), so run services/api first
// (moon run api:dev) or export SYSDESIGN_API_URL=https://sysdesign.thedefrag.ai to talk to prod.

import { runAgent } from "./loop.js"

function short(value: unknown, n = 200): string {
  const s = String(value)
  return s.length <= n ? s : s.slice(0, n) + "..."
}

async function main(): Promise<number> {
  const argv = process.argv.slice(2)
  if (argv.length === 0) {
    process.stderr.write('usage: npm run chat -- "your question"\n')
    return 2
  }
  const prompt = argv.join(" ")

  for await (const ev of runAgent(prompt)) {
    if (ev.type === "text") {
      process.stdout.write(ev.text)
    } else if (ev.type === "tool_use") {
      process.stdout.write(`\n  → ${ev.name}(${JSON.stringify(ev.input)})\n`)
    } else if (ev.type === "tool_result") {
      process.stdout.write(
        `  ← [${ev.ok ? "ok" : "ERR"}] ${short(ev.result)}\n`,
      )
    } else if (ev.type === "done") {
      process.stdout.write(
        `\n\n[done: ${ev.stop_reason} in ${ev.turns} turn(s)]\n`,
      )
    } else if (ev.type === "error") {
      process.stderr.write(`\n[error] ${ev.error}\n`)
      return 1
    }
  }
  return 0
}

main().then((code) => process.exit(code))
