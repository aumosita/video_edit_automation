<script>
  import { onMount, onDestroy } from "svelte";
  import UploadCard from "./UploadCard.svelte";
  import { listJobs, cancelJob, deleteJob, openGlobalSocket } from "./api.js";

  let jobs = $state([]);
  let ws = null;
  let reconnectTimer = null;
  let pollTimer = null;
  // Per-job toggle for the expanded error details row. Using a Set
  // keeps each row's expand state independent of WS re-renders.
  let expandedErrors = $state(new Set());

  function toggleError(id) {
    // Reassign a fresh Set so Svelte's reactivity picks up the change.
    const next = new Set(expandedErrors);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    expandedErrors = next;
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
      jobs = await listJobs();
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
    const idx = jobs.findIndex((j) => j.id === job.id);
    if (idx === -1) jobs = [job, ...jobs];
    else {
      const copy = jobs.slice();
      copy[idx] = job;
      jobs = copy;
    }
  }

  async function onCancel(id) {
    try { await cancelJob(id); } catch (e) { console.error("cancel failed:", e); }
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
  <UploadCard onSubmitted={onSubmitted} />

  <section class="jobs">
    <h2>Jobs <span class="count">({jobs.length})</span></h2>

    {#if jobs.length === 0}
      <p class="empty">No jobs yet. Upload a video to get started.</p>
    {:else}
      <table>
        <thead>
          <tr>
            <th>Input</th>
            <th>Status</th>
            <th>Progress</th>
            <th>Silences</th>
            <th>Words</th>
            <th>Subs</th>
            <th>Kept</th>
            <th>Removed</th>
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
              <td>
                <div class="progress">
                  <div class="bar" style="width: {Math.round((job.progress || 0) * 100)}%"></div>
                  <span class="pct">{Math.round((job.progress || 0) * 100)}%</span>
                </div>
              </td>
              <td>{job.num_silences ?? "—"}</td>
              <td>{job.num_words ?? "—"}</td>
              <td>{job.num_subtitles ?? "—"}</td>
              <td>{fmtDuration(job.kept_duration)}</td>
              <td>{fmtDuration(job.removed_duration)}</td>
              <td class="cell-time">{shortTime(job.started_at)}</td>
              <td class="cell-time cell-elapsed">
                {#if job.status === "running"}
                  {fmtDuration(elapsed(job))}{job.stage ? ` · ${job.stage}` : ""}
                {:else}
                  {fmtDuration(elapsed(job))}
                {/if}
              </td>
              <td class="cell-actions">
                {#if job.status === "queued" || job.status === "running"}
                  <button class="link danger" onclick={() => onCancel(job.id)}>Cancel</button>
                {/if}
                {#if job.fcpxml_url}
                  <a class="link" href={job.fcpxml_url} target="_blank" rel="noopener">.fcpxml</a>
                {/if}
                {#if job.report_md_url}
                  <a class="link" href={job.report_md_url} target="_blank" rel="noopener">.md</a>
                {/if}
                {#if job.report_json_url}
                  <a class="link" href={job.report_json_url} target="_blank" rel="noopener">.json</a>
                {/if}
                {#if job.srt_url}
                  <a class="link" href={job.srt_url} target="_blank" rel="noopener">.srt</a>
                {/if}
                {#if job.error_log_url}
                  <a class="link danger" href={job.error_log_url} target="_blank" rel="noopener">log</a>
                {/if}
                {#if (job.status === "failed" || job.status === "cancelled") && (job.error || job.error_traceback)}
                  <button class="link" onclick={() => toggleError(job.id)}>
                    {expandedErrors.has(job.id) ? "Hide" : "Details"}
                  </button>
                {/if}
                <button class="link danger" onclick={() => onDelete(job.id)}>Delete</button>
              </td>
            </tr>
            {#if (job.status === "failed" || job.status === "cancelled") && expandedErrors.has(job.id) && (job.error || job.error_traceback)}
              <tr class="row-detail row-{statusClass(job.status)}">
                <td colspan="11" class="cell-error">
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
                </td>
              </tr>
            {/if}
          {/each}
        </tbody>
      </table>
    {/if}
  </section>
</main>

