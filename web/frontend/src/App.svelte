<script>
  import { onMount, onDestroy } from "svelte";
  import UploadCard from "./UploadCard.svelte";
  import { listJobs, cancelJob, deleteJob, clearJobs, openGlobalSocket } from "./api.js";

  let jobs = $state([]);
  let ws = null;
  let reconnectTimer = null;
  let pollTimer = null;
  // Per-job toggle for the expanded details row (stats + downloads +
  // error). Using a Set keeps each row's expand state independent of
  // WS re-renders.
  let expandedRows = $state(new Set());
  let uploadCard = null;

  function toggleDetails(id) {
    // Reassign a fresh Set so Svelte's reactivity picks up the change.
    const next = new Set(expandedRows);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    expandedRows = next;
  }

  // Progress history per job (in-memory only). Used to derive the
  // "last minute" processing speed. Progress updates arrive as steps
  // (per pipeline stage), so a raw instantaneous slope just flickers;
  // a 60-second window smooths it out.
  let progressSamples = new Map(); // jobId -> [{ t: ms, p: 0..1 }]
  const SAMPLE_WINDOW_MS = 60_000;

  function recordSample(job) {
    if (typeof job.progress !== "number" || !job.started_at) return;
    let samples = progressSamples.get(job.id);
    if (!samples) {
      samples = [];
      progressSamples.set(job.id, samples);
    }
    const last = samples[samples.length - 1];
    // Only record actual changes so a busy message stream doesn't
    // fill the buffer with identical points.
    if (last && last.p === job.progress && Date.now() - last.t < 5000) return;
    samples.push({ t: Date.now(), p: job.progress });
    // Keep ~2 minutes of history.
    const cutoff = Date.now() - 2 * SAMPLE_WINDOW_MS;
    while (samples.length && samples[0].t < cutoff) samples.shift();
  }

  onMount(async () => {
    await refresh();
    startGlobalWs();
    startFallbackPoll();
  });

  onDestroy(() => {
    stopGlobalWs();
    stopFallbackPoll();
  });

  async function refresh() {
    try {
      const list = await listJobs();
      jobs = list;
      for (const j of list) recordSample(j);
    } catch (e) {
      console.error("listJobs failed:", e);
    }
  }

  function startGlobalWs() {
    try {
      ws = openGlobalSocket();
      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data);
          if (
            msg.type === "job.update" ||
            msg.type === "job.create" ||
            msg.type === "state"
          ) {
            if (msg.job) upsertJob(msg.job);
          } else if (msg.type === "job.deleted") {
            jobs = jobs.filter((j) => j.id !== msg.id);
          } else if (msg.type === "snapshot") {
            // Replace the whole table with the server's truth. Using
            // upsert (per-row) would leave deleted rows alive.
            jobs = Array.isArray(msg.jobs) ? msg.jobs : [];
            for (const j of jobs) recordSample(j);
          } else if (msg.type === "list") {
            // Legacy message shape; kept for backward compat.
            jobs = Array.isArray(msg.jobs) ? msg.jobs : [];
          } else if (msg.type === "pong") {
            // Heartbeat reply — nothing to do.
          } else {
            console.debug("unhandled ws message:", msg);
          }
        } catch (e) {
          console.warn("bad ws message:", e);
        }
      };
      ws.onclose = () => {
        ws = null;
        reconnectTimer = setTimeout(() => {
          startGlobalWs();
          refresh();
        }, 2000);
      };
      ws.onerror = () => {
        try { ws && ws.close(); } catch (_) {}
      };
    } catch (e) {
      console.warn("WS connect failed, will poll:", e);
    }
  }

  function stopGlobalWs() {
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    if (ws) { try { ws.close(); } catch (_) {} ws = null; }
  }

  function startFallbackPoll() {
    pollTimer = setInterval(() => {
      if (!ws || ws.readyState !== 1) refresh();
    }, 5000);
  }

  function stopFallbackPoll() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  function upsertJob(job) {
    recordSample(job);
    const idx = jobs.findIndex((j) => j.id === job.id);
    if (idx === -1) jobs = [job, ...jobs];
    else {
      const copy = jobs.slice();
      copy[idx] = job;
      jobs = copy;
    }
  }

  async function onCancel(id) {
    // Cancel keeps the record (status -> cancelled) and kills any ffmpeg
    // child, so refresh afterwards to show the updated status.
    try {
      await cancelJob(id);
      await refresh();
    } catch (e) { console.error("cancel failed:", e); }
  }

  async function onDelete(id) {
    try {
      const r = await fetch(`/api/jobs/${id}`, { method: "DELETE" });
      if (r.ok) jobs = jobs.filter((j) => j.id !== id);
    } catch (e) { console.error("delete failed:", e); }
  }

  function onSubmitted(job) {
    upsertJob(job);
  }

  function statusClass(s) { return s || "queued"; }
  function fmtDuration(secs) {
    if (secs == null) return "—";
    if (secs < 60) return `${secs.toFixed(1)}s`;
    const m = Math.floor(secs / 60);
    const s = (secs - m * 60).toFixed(0);
    return `${m}m${s}s`;
  }
  function shortTime(iso) {
    if (!iso) return "";
    return new Date(iso).toLocaleTimeString();
  }
  // Ticking clock so "elapsed" for running jobs updates every second
  // without waiting for the next WebSocket progress message.
  let now = $state(Date.now());
  let clockTimer = null;
  function startClock() {
    if (clockTimer) return;
    clockTimer = setInterval(() => { now = Date.now(); }, 1000);
  }
  function stopClock() {
    if (clockTimer) { clearInterval(clockTimer); clockTimer = null; }
  }
  $effect(() => {
    if (jobs.some((j) => j.status === "queued" || j.status === "running")) startClock();
    else stopClock();
    return () => stopClock();
  });
  // Elapsed time: live count-up while running, frozen total once the
  // job reaches a terminal state (completed / failed / cancelled).
  function elapsed(job) {
    if (!job.started_at) return null;
    const start = new Date(job.started_at).getTime();
    const end = job.finished_at
      ? new Date(job.finished_at).getTime()
      : now;
    return Math.max(0, (end - start) / 1000);
  }

  // Processing speed, expressed as a realtime multiplier: 2.0× means
  // two seconds of source media are handled per wall-clock second.
  // ``sourceSeconds`` = how much of the source has been processed.
  function sourceSecondsDone(job, at) {
    if (job.input_duration == null) return null;
    return (job.progress || 0) * job.input_duration;
  }
  function avgSpeed(job) {
    const el = elapsed(job);
    if (el == null || el <= 0.5) return null;
    const done = job.status === "completed"
      ? job.input_duration ?? null
      : sourceSecondsDone(job);
    if (done == null) return null;
    return done / el;
  }
  // Speed measured over the last ~60s of progress samples. Returns null
  // until a minute of history exists (or for finished jobs, where the
  // average is the meaningful number).
  function recentSpeed(job) {
    if (job.status !== "running") return null;
    const samples = progressSamples.get(job.id);
    if (!samples || samples.length < 2) return null;
    const latest = samples[samples.length - 1];
    const base = samples.find((s) => latest.t - s.t <= SAMPLE_WINDOW_MS)
      ?? samples[0];
    const dt = (latest.t - base.t) / 1000;
    if (dt < 30) return null; // need at least half a minute of history
    const dp = latest.p - base.p;
    if (dp <= 0) return null;
    if (job.input_duration == null) return null;
    return (dp * job.input_duration) / dt;
  }
  function fmtSpeed(x) {
    if (x == null || !Number.isFinite(x)) return "—";
    // Realtime multiplier: how many source-seconds are handled per
    // wall-clock second. Five decimal places keeps the value stable
    // when the rate is very slow (e.g. long videos transcoding).
    return `${x.toFixed(5)}×`;
  }
  function etaSeconds(job) {
    if (job.status !== "running") return null;
    const speed = avgSpeed(job);
    if (!speed || speed <= 0) return null;
    const remaining = job.input_duration != null
      ? (job.input_duration - sourceSecondsDone(job)) / speed
      : (1 - (job.progress || 0)) / (speed || 1);
    return remaining > 0 ? remaining : null;
  }
  function fmtDurationShort(secs) {
    if (secs == null) return "";
    const m = Math.floor(secs / 60);
    const s = Math.round(secs % 60);
    return m > 0 ? `${m}m ${s}s` : `${s}s`;
  }

  // ---- Per-stage progress rendering ----------------------------------
  // The backend now sends a ``stages`` array on every job update. We
  // render it as a single horizontal bar split into one coloured
  // segment per stage, with the active stage's bar fill animated to
  // its within-stage ``progress``. A small legend below lists each
  // stage with its status icon, so the user always knows which
  // stage is active and how far each one has gotten.

  // The bar widths are determined by the backend's ``weight`` field
  // (already normalised so they sum to 100%). We render them as a
  // CSS grid with each cell's width = weight% — no float drift in
  // ``width: calc(...)``.
  function stackedSegments(stages) {
    if (!Array.isArray(stages) || stages.length === 0) return null;
    // ``flex-basis`` percentages should sum to ~100% but the
    // backend may emit rounded numbers that drift a hair; we use
    // the last cell to absorb the rounding so they always fit.
    const segments = stages.map((s, i) => ({
      ...s,
      // 0..1 within-stage fill, only meaningful for "active".
      fill: s.status === "active"
        ? Math.max(0, Math.min(1, s.progress || 0))
        : s.status === "done"
          ? 1
          : 0,
      // Visual class for colour-coding.
      klass: `seg-${s.status}`,
      // Display text below the bar.
      icon: s.status === "done"
        ? "\u2713"
        : s.status === "active"
          ? "\u25B6"
          : s.status === "skipped"
            ? "\u00B7"
            : "\u00B7",
    }));
    return segments;
  }

  function useSettings(job) {
    if (uploadCard && typeof uploadCard.applySettings === "function") {
      uploadCard.applySettings(job.options || {}, job.input_name);
    }
  }

  async function onClearAll() {
    if (jobs.length === 0) return;
    const ok = typeof confirm === "function"
      ? confirm(`Delete all ${jobs.length} job(s) and their output files?`)
      : true;
    if (!ok) return;
    try {
      await clearJobs();
      jobs = [];
      expandedRows = new Set();
    } catch (e) { console.error("clearAll failed:", e); }
  }
</script>

<header class="topbar">
  <div class="brand">
    <span class="logo">▶</span>
    <span class="title">veauto</span>
    <span class="subtitle">video edit automation</span>
  </div>
  <div class="actions">
    <button class="ghost" onclick={refresh} title="Refresh">↻ Refresh</button>
  </div>
</header>

<main>
  <UploadCard bind:this={uploadCard} onSubmitted={onSubmitted} />

  <section class="jobs">
    <div class="jobs-head">
      <h2>Jobs <span class="count">({jobs.length})</span></h2>
      {#if jobs.length > 0}
        <div class="actions">
          <button class="link danger" onclick={onClearAll}>Clear all</button>
        </div>
      {/if}
    </div>

    {#if jobs.length === 0}
      <p class="empty">No jobs yet. Upload a video to get started.</p>
    {:else}
      <table>
        <thead>
          <tr>
            <th>Input</th>
            <th>Status</th>
            <th>Progress</th>
            <th>Speed</th>
            <th>Started</th>
            <th>Elapsed</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {#each jobs as job (job.id)}
            <tr class="row-{statusClass(job.status)}">
              <td class="cell-name" title={job.input_name}>{job.input_name}</td>
              <td><span class="badge badge-{statusClass(job.status)}">{job.status}</span></td>
              <td class="cell-progress">
                {#if stackedSegments(job.stages)}
                  {@const segments = stackedSegments(job.stages)}
                  <div class="pct-row">
                    <span class="pct-label">{Math.round((job.progress || 0) * 100)}%</span>
                    {#if job.status === "running" && job.stage && job.stage !== "queued"}
                      <span class="stage-label">{job.stage}</span>
                    {/if}
                  </div>
                  <div class="stacked-bar" role="progressbar" aria-valuenow={Math.round((job.progress || 0) * 100)} aria-valuemin="0" aria-valuemax="100">
                    {#each segments as seg (seg.name)}
                      <div
                        class="stage-segment {seg.klass}"
                        style="flex-basis: {(seg.weight * 100).toFixed(3)}%"
                        title="{seg.label} \u2014 {seg.status} ({Math.round(seg.fill * 100)}%)"
                      >
                        <div class="stage-fill" style="width: {(seg.fill * 100).toFixed(2)}%"></div>
                      </div>
                    {/each}
                  </div>
                  <ul class="stage-list" aria-label="Pipeline stages">
                    {#each segments as seg (seg.name)}
                      <li class="stage-item stage-item-{seg.status}">
                        <span class="stage-icon">{seg.icon}</span>
                        <span class="stage-name">{seg.label}</span>
                        {#if seg.status === "active"}
                          <span class="stage-pct">{Math.round(seg.fill * 100)}%</span>
                        {:else if seg.status === "done"}
                          <span class="stage-pct">done</span>
                        {:else if seg.status === "skipped"}
                          <span class="stage-pct">skipped</span>
                        {/if}
                      </li>
                    {/each}
                  </ul>
                {:else}
                  <!-- Legacy fallback for jobs that have no ``stages``
                       field yet (e.g. older running jobs after a
                       rolling deploy). The single-bar view still
                       works; once the worker emits a state with
                       ``stages`` this branch is replaced. -->
                  <div class="pct-row">
                    <span class="pct-label">{Math.round((job.progress || 0) * 100)}%</span>
                    {#if job.stage && job.status !== "completed" && job.stage !== "queued"}
                      <span class="stage-label">{job.stage}</span>
                    {/if}
                  </div>
                  <div class="progress">
                    <div
                      class="bar {statusClass(job.status)}"
                      style="width: {Math.round((job.progress || 0) * 100)}%"
                    ></div>
                  </div>
                {/if}
              </td>
              <td class="cell-speed">
                <div class="speed-main">
                  {fmtSpeed(avgSpeed(job))}
                </div>
                {#if job.status === "running"}
                  {#if recentSpeed(job) != null}
                    <div class="speed-sub">last 60s {fmtSpeed(recentSpeed(job))}</div>
                  {/if}
                  {#if etaSeconds(job) != null}
                    <div class="speed-sub">ETA {fmtDurationShort(etaSeconds(job))}</div>
                  {/if}
                {/if}
              </td>
              <td class="cell-time">{shortTime(job.started_at)}</td>
              <td class="cell-time">{fmtDuration(elapsed(job))}</td>
              <td class="cell-actions">
                <button class="link" onclick={() => toggleDetails(job.id)}>
                  {expandedRows.has(job.id) ? "Hide" : "Details"}
                </button>
                {#if job.status === "queued" || job.status === "running"}
                  <button class="link danger" onclick={() => onCancel(job.id)}>Cancel</button>
                {/if}
                <button class="link" onclick={() => useSettings(job)}>Use settings</button>
                <button class="link danger" onclick={() => onDelete(job.id)}>Delete</button>
              </td>
            </tr>
            {#if expandedRows.has(job.id)}
              <tr class="row-detail row-{statusClass(job.status)}">
                <td colspan="7" class="cell-detail">
                  <div class="detail-grid">
                    <div class="stat"><span class="k">Silences</span><span class="v">{job.num_silences ?? "—"}</span></div>
                    <div class="stat"><span class="k">Cuts</span><span class="v">{job.num_cuts ?? "—"}</span></div>
                    <div class="stat"><span class="k">Words</span><span class="v">{job.num_words ?? "—"}</span></div>
                    <div class="stat"><span class="k">Subtitles</span><span class="v">{job.num_subtitles ?? "—"}</span></div>
                    <div class="stat"><span class="k">Kept</span><span class="v">{fmtDuration(job.kept_duration)}</span></div>
                    <div class="stat"><span class="k">Removed</span><span class="v">{fmtDuration(job.removed_duration)}</span></div>
                    <div class="stat"><span class="k">Source</span><span class="v">{fmtDuration(job.input_duration)}</span></div>
                    <div class="stat"><span class="k">Size</span><span class="v">{job.input_size != null ? (job.input_size / 1048576).toFixed(1) + " MB" : "—"}</span></div>
                  </div>

                  <div class="detail-links">
                    {#if job.fcpxml_url}
                      <a class="link" href={job.fcpxml_url} target="_blank" rel="noopener">.fcpxml</a>
                    {/if}
                    {#if job.srt_url}
                      <a class="link" href={job.srt_url} target="_blank" rel="noopener">.srt</a>
                    {/if}
                    {#if job.report_md_url}
                      <a class="link" href={job.report_md_url} target="_blank" rel="noopener">report.md</a>
                    {/if}
                    {#if job.report_json_url}
                      <a class="link" href={job.report_json_url} target="_blank" rel="noopener">report.json</a>
                    {/if}
                    {#if job.error_log_url}
                      <a class="link danger" href={job.error_log_url} target="_blank" rel="noopener">log</a>
                    {/if}
                  </div>

                  {#if job.error || job.error_traceback}
                    <div class="cell-error">
                      <div class="err-head">
                        <strong>{job.error_kind === "cancelled" ? "Cancelled" : "Failed"}</strong>
                        {#if job.error_stage}<span class="err-stage">stage: {job.error_stage}</span>{/if}
                        {#if job.error_kind}<span class="err-kind">{job.error_kind}</span>{/if}
                      </div>
                      {#if job.error}
                        <div class="err-summary">{job.error}</div>
                      {/if}
                      {#if job.error_traceback}
                        <pre class="err-trace">{job.error_traceback}</pre>
                      {/if}
                    </div>
                  {/if}
                </td>
              </tr>
            {/if}
          {/each}
        </tbody>
      </table>
    {/if}
  </section>
</main>

