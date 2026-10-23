"use strict";

// Transport is shared by refresh, creation and recovery commands. An ambiguous
// POST can be resent with its original key, so a lost response cannot duplicate it.
const BoardClient = (() => {
  const pendingKeys = new Map();
  async function request(url, options = {}, timeoutMs = 15000) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(url, {
        cache: "no-store",
        ...options,
        signal: controller.signal,
      });
      let payload;
      try {
        payload = await response.json();
      } catch (_) {
        throw new Error(
          `Invalid server response (HTTP ${response.status}). Refresh and try again.`,
        );
      }
      if (!response.ok) {
        const error = new Error(
          payload.safe_error || `HTTP ${response.status}`,
        );
        error.status = response.status;
        error.payload = payload;
        throw error;
      }
      return payload;
    } catch (error) {
      if (error.name === "AbortError")
        throw new Error(
          "Request timed out. Refresh to check the result before retrying.",
        );
      throw error;
    } finally {
      clearTimeout(timer);
    }
  }
  async function post(url, payload, token) {
    const identity = JSON.stringify([url, payload]);
    if (!pendingKeys.has(identity))
      pendingKeys.set(
        identity,
        globalThis.crypto?.randomUUID?.() ||
          `web-${Date.now()}-${Math.random()}`,
      );
    try {
      const result = await request(url, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "x-robert-csrf-token": token || "",
          "x-idempotency-key": pendingKeys.get(identity),
        },
        body: JSON.stringify(payload),
      });
      pendingKeys.delete(identity);
      return result;
    } catch (error) {
      if (error.status && error.status < 500) pendingKeys.delete(identity);
      throw error;
    }
  }
  async function pages(url, count = 1) {
    let data;
    const items = new Map();
    let cursor;
    for (let page = 0; page < count; page++) {
      data = await request(
        url + (cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""),
      );
      data.items.forEach((item) => {
        if (!items.has(item.work_item_id)) items.set(item.work_item_id, item);
      });
      cursor = data.next_cursor;
      if (!cursor) break;
    }
    return { ...data, items: Array.from(items.values()) };
  }
  return { get: request, post, pages };
})();
