import assert from "node:assert/strict"
import { normalizePath } from "../src/path_utils.mjs"

assert.equal(normalizePath("src\\agent\\runtime.ts"), "src/agent/runtime.ts")
assert.equal(normalizePath("src/agent/runtime.ts"), "src/agent/runtime.ts")
