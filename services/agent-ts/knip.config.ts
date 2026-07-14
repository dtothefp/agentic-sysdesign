import type { KnipConfig } from "knip"

// knip auto-detects the two runtime entries from package.json scripts (src/index.ts via start,
// src/cli.ts via chat); the test suite is the only root it needs told about explicitly.
const config: KnipConfig = {
  entry: ["test/**/*.test.ts"],
  project: ["src/**/*.ts", "test/**/*.ts"],
}

export default config
