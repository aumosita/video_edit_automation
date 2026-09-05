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
    let text = "";
    try { text = await r.text(); } catch (_) { /* noop */ }
    throw new ApiError(r.status, r.statusText, text);
  }
  if (r.status === 204) return null;
  return r.json();
}

export const api = {
  health: () => request("/api/health"),
  listJobs: () => request("/api/jobs"),
  getJob: (id) => request(`/api/jobs/${id}`),
  cancelJob: (id) => request(`/api/jobs/${id}`, { method: "DELETE" }),
  deleteJob: (id) => request(`/api/jobs/${id}`, { method: "DELETE" }),
  defaultOptions: () => request("/api/config/defaults"),
  listModels: async () => {
    const r = await request("/api/config/models");
    return r && Array.isArray(r.models) ? r.models : [];
  },
  jobFcpxmlUrl: (id) => `${BASE}/api/jobs/${id}/download/output.fcpxml`,
  jobReportMdUrl: (id) => `${BASE}/api/jobs/${id}/download/report.md`,
  jobReportJsonUrl: (id) => `${BASE}/api/jobs/${id}/download/report.json`,
};

// Named re-exports for convenience
export const { health, listJobs, getJob, cancelJob, deleteJob, defaultOptions, listModels } = api;

/** Open a WebSocket to ``/api/ws/jobs/{id}`` (per-job). Returns the raw WebSocket. */
export function openJobSocket(jobId) {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return new WebSocket(`${proto}//${location.host}/api/ws/jobs/${jobId}`);
}

/** Open a WebSocket to ``/api/ws`` (global event stream). Returns the raw WebSocket. */
export function openGlobalSocket() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return new WebSocket(`${proto}//${location.host}/api/ws`);
}

/**
 * Submit a job. ``file`` is a ``File`` instance; ``options`` is an
 * object whose keys mirror the backend query params:
 *
 *   noiseDb, minSilence, margin, minKeepSeconds, model,
 *   language, device, computeType, projectName, eventName, ...
 */
export function submitJob(file, options = {}) {
  const params = new URLSearchParams();
  const json = {
    noise_db: Number(options.noiseDb ?? -30),
    auto_noise_db: !!options.autoNoiseDb,
    noise_db_offset: Number(options.noiseDbOffset ?? 0),
    min_silence: Number(options.minSilence ?? 1.5),
    margin: Number(options.margin ?? 0.3),
    min_keep_seconds: Number(options.minKeepSeconds ?? 0.15),
    no_silence: !!options.noSilence,
    no_subtitles: !!options.noSubtitles,
    // `subtitle_target` is the new 4-way control: srt | fcpxml | both | none.
    // Only send it when the caller explicitly picked one, so the backend
    // can fall back to `no_subtitles` / "both" for old clients.
    ...(options.subtitle_target != null
        ? { subtitle_target: options.subtitle_target }
        : {}),
    model: options.model || "medium",
    language: options.language || null,
    device: options.device || "auto",
    compute_type: options.computeType || "auto",
    project_name: options.projectName || "Auto Edit",
    event_name: options.eventName || "veauto",
    style_position: options.style_position || "bottom",
    style_font: options.style_font || "Apple SD Gothic Neo",
    style_font_size: Number(options.style_font_size ?? 56),
    style_bold: options.style_bold !== false,
    style_color: options.style_color || "#FFFFFF",
    style_offset_y: Number(options.style_offset_y ?? 0),
    style_template: options.style_template || "text",
    subtitle_offset: Number(options.subtitle_offset ?? 0),
    style_max_chars: Number(options.style_max_chars ?? 42),
    style_max_lines: Number(options.style_max_lines ?? 2),
    style_split_sentence: options.style_split_sentence !== false,
  };
  params.set("options", JSON.stringify(json));
  const fd = new FormData();
  fd.append("file", file, file.name);
  return request(`/api/jobs?${params}`, { method: "POST", body: fd });
}

