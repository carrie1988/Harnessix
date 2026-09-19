import assert from "node:assert/strict"
import { readFile } from "node:fs/promises"
import { retryDelay } from "../src/path_utils.mjs"

const source = await readFile("src/path_utils.mjs", "utf8")
assert.equal(source.includes("attempt ==="), false)
assert.deepEqual([0, 1, 2, 3].map((attempt) => retryDelay(attempt, 100)), [100, 200, 400, 800])
