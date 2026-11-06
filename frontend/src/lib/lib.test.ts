import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, session } from "./api";
import { layoutDag } from "./dag";
import { formatDuration, percent, relativeTime } from "./format";
import { SSEParser } from "./sse";

describe("SSEParser", () => {
  it("parses events split across arbitrary chunk boundaries", () => {
    const stream = 'retry: 3000\n\nid: 7\nevent: log\ndata: {"message":"hi"}\n\n: keepalive\n\nevent: end\ndata: {}\n\n';
    for (const size of [1, 3, 10, stream.length]) {
      const parser = new SSEParser();
      const out = [];
      for (let i = 0; i < stream.length; i += size) out.push(...parser.feed(stream.slice(i, i + size)));
      expect(out).toEqual([
        { event: "log", data: '{"message":"hi"}', id: "7" },
        { event: "end", data: "{}", id: "7" }, // id persists, per the SSE spec
      ]);
    }
  });

  it("handles CRLF line endings and multi-line data", () => {
    const out = new SSEParser().feed("event: x\r\ndata: a\r\ndata: b\r\n\r\n");
    expect(out).toEqual([{ event: "x", data: "a\nb", id: undefined }]);
  });

  it("ignores retry-only blocks with no data", () => {
    expect(new SSEParser().feed("retry: 3000\n\n")).toEqual([]);
  });
});

describe("layoutDag", () => {
  it("places each task one column right of its deepest parent", () => {
    const placed = layoutDag([
      { key: "capture", dependsOn: [] },
      { key: "qualify", dependsOn: ["capture"] },
      { key: "store", dependsOn: ["qualify"] },
      { key: "alert", dependsOn: ["qualify", "store"] },
      { key: "audit", dependsOn: ["capture"] },
    ]);
    const column = Object.fromEntries(placed.map((p) => [p.key, p.column]));
    expect(column).toEqual({ capture: 0, qualify: 1, audit: 1, store: 2, alert: 3 });
    const xs = placed.map((p) => p.x);
    expect(new Set(xs).size).toBe(4);
  });

  it("detects cycles instead of recursing forever", () => {
    expect(() => layoutDag([{ key: "a", dependsOn: ["b"] }, { key: "b", dependsOn: ["a"] }])).toThrow(/cycle/);
  });
});

describe("format", () => {
  it.each([
    [0.042, "42 ms"],
    [3.25, "3.3 s"],
    [42, "42 s"],
    [125, "2m 5s"],
    [3720, "1h 2m"],
    [null, "-"],
  ])("formatDuration(%s) = %s", (input, expected) => expect(formatDuration(input)).toBe(expected));

  it("formats relative times in both directions", () => {
    const now = Date.parse("2026-11-05T12:00:00Z");
    expect(relativeTime("2026-11-05T11:58:00Z", now)).toBe("2 min ago");
    expect(relativeTime("2026-11-05T14:00:00Z", now)).toBe("in 2 h");
    expect(relativeTime("2026-11-05T11:59:50Z", now)).toBe("just now");
  });

  it("formats ratios", () => {
    expect(percent(0.66666)).toBe("66.7%");
    expect(percent(null)).toBe("-");
  });
});

describe("api client", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    session.clear();
  });

  it("refreshes once for concurrent 401s and retries each request", async () => {
    session.set({ access_token: "old", refresh_token: "r".repeat(32), expires_in: 900 });
    let refreshes = 0;
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/auth/refresh")) {
        refreshes += 1;
        await new Promise((r) => setTimeout(r, 5));
        return new Response(JSON.stringify({ access_token: "new", refresh_token: "n".repeat(32), expires_in: 900 }), { status: 200 });
      }
      const auth = new Headers(init?.headers).get("Authorization");
      return auth === "Bearer new"
        ? new Response(JSON.stringify({ ok: true }), { status: 200 })
        : new Response(JSON.stringify({ code: "token_expired", detail: "expired" }), { status: 401 });
    });
    vi.stubGlobal("fetch", fetchMock);

    const results = await Promise.all([api("/a"), api("/b"), api("/c")]);
    expect(results).toEqual([{ ok: true }, { ok: true }, { ok: true }]);
    expect(refreshes).toBe(1); // reusing a rotated refresh token would revoke the session
  });

  it("turns problem+json into ApiError with the request id", async () => {
    vi.stubGlobal("fetch", vi.fn(async () =>
      new Response(JSON.stringify({ code: "not_found", detail: "Run not found", request_id: "req-1" }), { status: 404 }),
    ));
    const error = await api("/runs/x").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 404, code: "not_found", message: "Run not found", requestId: "req-1" });
  });
});
