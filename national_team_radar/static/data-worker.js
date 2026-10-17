"use strict";
importScripts("/static/timeline.js?v=20261017-scrub");

// Fetch, JSON parsing, column expansion and indexing stay off the UI thread.
self.onmessage = async ({ data: { id, url } }) => {
  try {
    const response = await fetch(url, { headers: { Accept: "application/json" } });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : `数据读取失败（HTTP ${response.status}）`);
    self.postMessage({ id, result: RadarTimeline.prepare(data) });
  } catch (error) { self.postMessage({ id, error: error.message || "后台数据读取失败" }); }
};
