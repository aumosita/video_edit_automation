// Minimal fetch wrapper + WebSocket helpers.
//
// In dev (Vite proxy at :5173) and in prod (FastAPI on :8000) the API
// and WS endpoints are same-origin, so we use relative URLs.

const BASE = "";

export class ApiError extends Error {
  constructor(status, statusText, body) {
    super(`${status} ${statusText}: ${body}`);
    this.status = status;
    this.body = body;
  }
}

async function request(path, opts = {}) {
  const r = await fetch(BASE + path, opts);
  if (!r.ok) {
    const text = await r.text();
    throw new ApiError(r.status, r.statusText, text);
  }
  if (r.status === 204) return null;
  return r.json();
}

export const api = {
  health: () => request("/api/health"),
  listJobs: () => request("/api/jobs"),
  getJob: (id) => request(`/api/jobs/${id}`),
  cancelJob: (id) =>
    request(`/api/jobs/${id}`, { method: "DELETE" }),
  defaultOptions: () => request("/api/config/defaults"),
  listModels: async () => {
    const r = await request("/api/config/models");
    return r && Array.isArray(r.models) ? r.models : [];
  },
  jobFcpxmlUrl: (id) => `${BASE}/api/jobs/${id}/download/${id}.fcpxml`,
  jobReportMdUrl: (id) => `${BASE}/api/jobs/${id}/download/${id}.report.md`,
  jobReportJsonUrl: (id) => `${BASE}/api/jobs/${id}/download/${id}.report.json`,
};

// Named re-exports for convenience
export const { health, listJobs, getJob, cancelJob, defaultOptions, listModels } = api;

/** Open a WebSocket to ``/api/ws/jobs/{id}``. Returns the raw WebSocket. */
export function openJobSocket(jobId) {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return new WebSocket(`${proto}//${location.host}/api/ws/jobs/${jobId}`);
}

/** Submit a job: POST multipart with the file and JSON options. */
export function submitJob(opts) {
  const params = new URLSearchParams({
    options: JSON.stringify({
      noise_db: Number(opts.noiseDb),
      min_silence: Number(opts.minSilence),
      margin: Number(opts.margin),
      no_silence: !!opts.noSilence,
      no_subtitles: !!opts.noSubtitles,
      model: opts.model,
      language: opts.language || null,
      device: opts.device,
      compute_type: opts.computeType,
      style_position: "bottom",
      style_font: "Apple SD Gothic Neo",
      style_font_size: 48,
      style_max_chars: 42,
      style_max_lines: 2,
      style_min_duration: 0.8,
      style_max_duration: 6.0,
    }),
  });
  const fd = new FormData();
  fd.append("file", opts.file, opts.file.name);
  return request(`/api/jobs?${params}`, {
    method: "POST",
    body: fd,
  });
}
