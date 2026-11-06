/**
 * Server-Sent Events over fetch().
 *
 * Why not the browser's EventSource? It can't send an Authorization header, and
 * putting the JWT in the query string would leak it into proxy and access logs.
 * fetch() + a streaming body gives us headers, AbortController cancellation and
 * explicit reconnect-with-Last-Event-ID, in about 100 lines.
 */
import { authorizedFetch, toApiError } from "./api";

export interface SSEMessage {
  event: string;
  data: string;
  id?: string;
}

/** Incremental parser for the text/event-stream format (handles any chunking). */
export class SSEParser {
  private buffer = "";
  private event = "message";
  private data: string[] = [];
  private id: string | undefined;

  feed(chunk: string): SSEMessage[] {
    this.buffer += chunk;
    const out: SSEMessage[] = [];
    let newline: number;
    while ((newline = this.buffer.search(/\r\n|\n|\r/)) >= 0) {
      const line = this.buffer.slice(0, newline);
      const sepLength = this.buffer.startsWith("\r\n", newline) ? 2 : 1;
      this.buffer = this.buffer.slice(newline + sepLength);
      if (line === "") {
        if (this.data.length) out.push({ event: this.event, data: this.data.join("\n"), id: this.id });
        this.event = "message";
        this.data = [];
        continue;
      }
      if (line.startsWith(":")) continue; // comment / keepalive
      const colon = line.indexOf(":");
      const field = colon < 0 ? line : line.slice(0, colon);
      const value = colon < 0 ? "" : line.slice(colon + 1).replace(/^ /, "");
      if (field === "event") this.event = value;
      else if (field === "data") this.data.push(value);
      else if (field === "id") this.id = value;
    }
    return out;
  }
}

export interface StreamOptions {
  onMessage: (message: SSEMessage) => void;
  onStateChange?: (state: "connecting" | "live" | "reconnecting" | "closed") => void;
  signal: AbortSignal;
  reconnectMs?: number;
}

/** Follows a stream until the server says it's finished or the signal aborts.
 *  Network drops and the server's max-duration cut-off reconnect and resume. */
export async function followStream(path: string, opts: StreamOptions): Promise<void> {
  const { onMessage, onStateChange, signal, reconnectMs = 3000 } = opts;
  let lastEventId: string | undefined;
  onStateChange?.("connecting");
  while (!signal.aborted) {
    let finished = false;
    try {
      const headers: HeadersInit = { Accept: "text/event-stream" };
      if (lastEventId) (headers as Record<string, string>)["Last-Event-ID"] = lastEventId;
      const response = await authorizedFetch(path, { headers, signal });
      if (!response.ok || !response.body) throw await toApiError(response);
      onStateChange?.("live");
      const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
      const parser = new SSEParser();
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        for (const message of parser.feed(value)) {
          if (message.id) lastEventId = message.id;
          if (message.event === "end") {
            finished = (JSON.parse(message.data) as { reason: string }).reason === "finished";
          } else {
            onMessage(message);
          }
        }
      }
    } catch (error) {
      if (signal.aborted) break;
      if (error instanceof Error && "status" in error && [401, 403, 404].includes((error as { status: number }).status)) {
        onStateChange?.("closed");
        throw error; // not transient: don't hammer the server
      }
    }
    if (finished || signal.aborted) break;
    onStateChange?.("reconnecting");
    await new Promise((resolve) => setTimeout(resolve, reconnectMs));
  }
  onStateChange?.("closed");
}
