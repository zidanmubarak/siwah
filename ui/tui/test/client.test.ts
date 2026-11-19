import { expect, test } from "bun:test"

import { parseApiError } from "../src/api/client"

test("parses FastAPI detail objects", () => {
  const parsed = parseApiError(409, "Conflict", {
    detail: { code: "permission_required", message: "Shell needs approval", preview: "$ rm -rf" },
  })
  expect(parsed.code).toBe("permission_required")
  expect(parsed.message).toBe("Shell needs approval")
  expect(parsed.details).toEqual({
    code: "permission_required",
    message: "Shell needs approval",
    preview: "$ rm -rf",
  })
})

test("parses string detail from auth errors", () => {
  const parsed = parseApiError(400, "Bad Request", { detail: "Unknown provider: openai" })
  expect(parsed.code).toBe("http_400")
  expect(parsed.message).toBe("Unknown provider: openai")
})

test("parses legacy error objects and validation arrays", () => {
  const legacy = parseApiError(500, "Server Error", {
    error: { code: "provider_failed", message: "upstream failed" },
  })
  expect(legacy.code).toBe("provider_failed")
  expect(legacy.message).toBe("upstream failed")

  const validation = parseApiError(422, "Unprocessable", {
    detail: [{ loc: ["body", "api_key"], msg: "Field required" }],
  })
  expect(validation.message).toBe("Field required")
  expect(Array.isArray(validation.details)).toBe(true)
})
