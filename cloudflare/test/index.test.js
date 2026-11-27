import assert from "node:assert/strict";
import test from "node:test";

import worker, { isAthensTenAM } from "../src/index.js";

test("does not expose an HTTP endpoint", async () => {
  const response = await worker.fetch();
  assert.equal(response.status, 404);
  assert.equal(await response.text(), "Not found");
});

test("selects 10:00 Athens in winter and skips the neighboring UTC trigger", () => {
  assert.equal(isAthensTenAM("2026-11-27T08:00:00Z"), true);
  assert.equal(isAthensTenAM("2026-11-27T07:00:00Z"), false);
});

test("selects 10:00 Athens in summer and skips the neighboring UTC trigger", () => {
  assert.equal(isAthensTenAM("2026-07-27T07:00:00Z"), true);
  assert.equal(isAthensTenAM("2026-07-27T08:00:00Z"), false);
});

test("dispatches the GitHub workflow only for the matching Athens trigger", async () => {
  const originalFetch = globalThis.fetch;
  let requestUrl;
  let requestOptions;
  globalThis.fetch = async (url, options) => {
    requestUrl = url;
    requestOptions = options;
    return new Response(null, { status: 204 });
  };

  try {
    await worker.scheduled(
      { scheduledTime: Date.parse("2026-11-27T08:00:00Z") },
      {
        GITHUB_REPOSITORY: "ckefalianou/smartMarket-public",
        GITHUB_TOKEN: "test-token",
      },
    );
    assert.equal(
      requestUrl,
      "https://api.github.com/repos/ckefalianou/smartMarket-public/actions/workflows/daily-prices.yml/dispatches",
    );
    assert.equal(requestOptions.method, "POST");
    assert.equal(requestOptions.headers.Authorization, "Bearer test-token");
    assert.deepEqual(JSON.parse(requestOptions.body), {
      ref: "main",
      inputs: { scheduled: true },
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("does not call GitHub for a UTC trigger outside 10:00 Athens", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    throw new Error("GitHub must not be called for the skipped trigger");
  };

  try {
    await worker.scheduled(
      { scheduledTime: Date.parse("2026-11-27T07:00:00Z") },
      {},
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("surfaces missing GitHub token configuration", async () => {
  await assert.rejects(
    worker.scheduled(
      { scheduledTime: Date.parse("2026-11-27T08:00:00Z") },
      { GITHUB_REPOSITORY: "ckefalianou/smartMarket-public" },
    ),
    /Missing Cloudflare secret GITHUB_TOKEN/,
  );
});

test("surfaces GitHub dispatch failures", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(null, { status: 401 });

  try {
    await assert.rejects(
      worker.scheduled(
        { scheduledTime: Date.parse("2026-11-27T08:00:00Z") },
        {
          GITHUB_REPOSITORY: "ckefalianou/smartMarket-public",
          GITHUB_TOKEN: "test-token",
        },
      ),
      /HTTP 401/,
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("surfaces an invalid GitHub repository configuration", async () => {
  await assert.rejects(
    worker.scheduled(
      { scheduledTime: Date.parse("2026-11-27T08:00:00Z") },
      { GITHUB_REPOSITORY: "owner/repo/extra", GITHUB_TOKEN: "test-token" },
    ),
    /GITHUB_REPOSITORY must use the owner\/repository format/,
  );
});
