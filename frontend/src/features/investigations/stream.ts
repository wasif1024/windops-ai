// An investigation's live progress, from Web BFF's server-sent events (API-003). A browser's
// EventSource cannot send an Authorization header, so this reads the stream with fetch, which can,
// and carries on after the last event it saw with Last-Event-ID, as the stream asks.
import { API } from "../../api/client";
import type { ProgressEvent } from "./api";

export interface SseMessage {
  id: string;
  event: string;
  data: string;
}

/** Split a buffer into whole messages, and what is left of an unfinished one. */
export function parseSse(buffer: string): { messages: SseMessage[]; rest: string } {
  const blocks = buffer.split(/\r?\n\r?\n/);
  const rest = blocks.pop() ?? "";
  const messages: SseMessage[] = [];
  for (const block of blocks) {
    const message: SseMessage = { id: "", event: "message", data: "" };
    let seen = false;
    for (const line of block.split(/\r?\n/)) {
      if (!line || line.startsWith(":")) continue;
      const colon = line.indexOf(":");
      const field = colon < 0 ? line : line.slice(0, colon);
      const value = colon < 0 ? "" : line.slice(colon + 1).replace(/^ /, "");
      if (field === "id") message.id = value;
      else if (field === "event") message.event = value;
      else if (field === "data") message.data += (message.data ? "\n" : "") + value;
      else continue;
      seen = true;
    }
    if (seen) messages.push(message);
  }
  return { messages, rest };
}

/**
 * Follow a case's progress until it is finished or `signal` aborts: each event once, in order.
 * Returns the last sequence seen. A stream that ends unfinished is read again from there.
 */
export async function follow(
  caseId: string,
  token: string,
  onEvent: (event: ProgressEvent) => void,
  signal: AbortSignal,
  fetcher: typeof fetch = fetch,
  pauseMs = 3000,
): Promise<number> {
  let after = 0;
  while (!signal.aborted) {
    let finished = false;
    try {
      const response = await fetcher(`${API}/investigations/${encodeURIComponent(caseId)}/events`, {
        headers: { Authorization: `Bearer ${token}`, Accept: "text/event-stream", "Last-Event-ID": String(after) },
        credentials: "omit",
        signal,
      });
      if (!response.ok || !response.body) throw new Error(`The stream answered ${response.status}`);
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const { messages, rest } = parseSse(buffer);
        buffer = rest;
        for (const message of messages) {
          if (message.event === "finished") finished = true;
          else if (message.data) {
            const event = JSON.parse(message.data) as ProgressEvent;
            if (event.sequence > after) {
              after = event.sequence;
              onEvent(event);
            }
          }
        }
      }
    } catch (error) {
      if (signal.aborted) break;
      if (error instanceof Error && /answered 4\d\d/.test(error.message)) throw error;
    }
    if (finished) break;
    await new Promise((resolve) => setTimeout(resolve, pauseMs));
  }
  return after;
}
