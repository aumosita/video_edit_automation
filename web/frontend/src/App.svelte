<script>
  import { onMount, onDestroy } from "svelte";
  import { api, openJobSocket } from "./api.js";
  import UploadCard from "./UploadCard.svelte";

  let health = $state(null);
  let jobs = $state([]);
  let err = $state("");

  /** jobId → WebSocket */
  const sockets = new Map();

  async function refresh() {
    try {
      const [hp, list] = await Promise.all([api.health(), api.listJobs()]);
      health = hp;
      jobs = list;
      err = "";
    } catch (e) {
      err = e?.message || String(e);
    }
  }

  onMount(() => {
    refresh();
    const t = setInterval(refresh, 4000);
    return () => clearInterval(t);
  });

  onDestroy(() => {
    for (const ws of sockets.values()) {
      try { ws.close(); } catch {}
    }
    sockets.clear();
  });

  /** Patch one job in the local list (used by WebSocket events). */
  function patchJob(id, patch) {
    jobs = jobs.map((j) => (j.id === id ? { ...j, ...patch } : j));
  }

  function attachSocket(id) {
    if (sockets.has(id)) return;
    const ws = openJobSocket(id);
    sockets.set(id, ws);
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === "state") {
          patchJob(id, msg.payload);
          if (
            ["completed", "failed", "cancelled"].includes(msg.payload.status)
          ) {
            try { ws.close(); } catch {}
            sockets.delete(id);
            refresh();
          }
        } else if (msg.type === "log") {
          // Could push to a log panel; for now ignored.
        }
      } catch (e) {
        console.warn("ws parse error", e);
      }
    };
    ws.onclose = () => { sockets.delete(id); };
    ws.onerror = () => { sockets.delete(id); };
  }

  function ensureSocket(id) {
    attachSocket(id);
  }

  function onSubmitted(job) {
    // Prepend optimistically; backend will broadcast real state.
    jobs = [
      {
        ...job,
        input_name: "uploading…",
        input_size: 0,
        progress: 0,
        stage: "queued",
        message: "Submitting…",
      },
      ...jobs,
    ];
    attachSocket(job.id);
    setTimeout(refresh, 600);
  }

  async function onCancel(id) {
    try {
      await api.cancelJob(id);
      patchJob(id, { status: "cancelled" });
    } catch (e) {
      err = e?.message || String(e);
    }
  }

  // Attach sockets for all running/queued jobs on every refresh.
  $effect(() => {
    for (const j of jobs) {
      if (["queued", "running"].includes(j.status)) {
        ensureSocket(j.id);
      }
    }
  });

  function statusBadge(s) {
    switch (s) {
      case "queued":    return "queued";
      case "running":   return "running";
      case "completed": return "completed";
      case "failed":    return "failed";
      case "cancelled": return "cancelled";
      default: return "";
    }
  }

  function fmtBytes(n) {
    if (!n) return "—";
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
    return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
  }

  function fmtSeconds(s) {
    if (s == null) return "—";
    return `${Number(s).toFixed(1)}s`;
  }
</script>

<header>
  <div class="brand">
    <span class="logo">🎬</span>
    <span class="title">veauto</span>
    <span class="muted subtitle">silence + subtitles · localhost</span>
  </div>
  <div class="status">
    {#if health}
      <span class="muted mono">v{health.version}</span>
      <span class="badge completed">● online</span>
    {:else}
      <span class="badge failed">● offline</span>
    {/if}
  </div>
</header>

<main>
  {#if err}
    <div class="card error-card">
      <strong>Connection error:</strong> {err}
      <p class="muted">Is the backend running? Start it with
        <code>uv run veauto serve</code>
      </p>
    </div>
  {/if}

  <UploadCard onSubmitted={onSubmitted} />

  <section class="card">
    <div class="card-head">
      <h2>Jobs</h2>
      <button class="ghost" on:click={refresh}>↻ Refresh</button>
    </div>
    {#if jobs.length === 0}
      <p class="muted">No jobs yet. Upload a video above to start.</p>
    {:else}
      <table>
        <thead>
          <tr>
            <th>Status</th>
            <th>File</th>
            <th>Size</th>
            <th>Stage / progress</th>
            <th>Stats</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {#each jobs as j (j.id)}
            <tr>
              <td>
                <span class="badge {statusBadge(j.status)}">{j.status}</span>
              </td>
              <td title={j.id}>
                <strong>{j.input_name}</strong>
                <div class="muted mono small">{j.id.slice(0, 12)}…</div>
              </td>
              <td>{fmtBytes(j.input_size)}</td>
              <td class="stage-cell">
                <div class="stage-line">
                  <span class="muted small">{j.stage}</span>
                  <span class="muted small">{j.message || ""}</span>
                </div>
                <div class="progress" title="{(j.progress * 100).toFixed(0)}%">
                  <div
                    class="bar"
                    class:running={j.status === "running"}
                    class:done={j.status === "completed"}
                    class:err={j.status === "failed"}
                    style="width: {Math.max(0, Math.min(1, j.progress || 0)) * 100}%"
                  ></div>
                </div>
                <div class="muted small">{Math.round((j.progress || 0) * 100)}%</div>
              </td>
              <td class="muted small">
                {#if j.num_silences != null}
                  ✂ {j.num_silences} silences → {j.num_cuts} cuts<br />
                  ⏱ kept {fmtSeconds(j.kept_duration)} / removed {fmtSeconds(j.removed_duration)}<br />
                  💬 {j.num_subtitles ?? 0} subtitles
                {:else}
                  —
                {/if}
              </td>
              <td>
                <div class="actions">
                  {#if ["queued", "running"].includes(j.status)}
                    <button class="ghost" on:click={() => onCancel(j.id)}>Cancel</button>
                  {/if}
                  {#if j.status === "completed" && j.fcpxml_name}
                    <a class="link" href={api.jobFcpxmlUrl(j.id)} download>FCPXML</a>
                  {/if}
                  {#if j.status === "completed" && j.report_md_name}
                    <a class="link" href={api.jobReportMdUrl(j.id)} download>Report</a>
                  {/if}
                  {#if j.status === "completed" && j.report_json_name}
                    <a class="link" href={api.jobReportJsonUrl(j.id)} download>JSON</a>
                  {/if}
                  {#if j.error}
                    <span class="error" title={j.error}>⚠ {j.error.slice(0, 60)}</span>
                  {/if}
                </div>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}
  </section>
</main>


<header>
  <div class="brand">
    <span class="logo">🎬</span>
    <span class="title">veauto</span>
    <span class="muted subtitle">silence + subtitles · localhost</span>
  </div>
  <div class="status">
    {#if health}
      <span class="muted mono">v{health.version}</span>
      <span class="badge completed">● online</span>
    {:else}
      <span class="badge failed">● offline</span>
    {/if}
  </div>
</header>

<main>
  {#if err}
    <div class="card error">
      <strong>Connection error:</strong> {err}
      <p class="muted">Is the backend running? Start it with:
        <code>uv run veauto serve</code>
      </p>
    </div>
  {/if}

  <UploadCard {defaults} {models} on:submitted={onSubmitted} />

  <JobsTable {jobs} {wsUrl} on:changed={onChanged} />
</main>

<style>
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 14px 24px;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
  }
  .brand { display: flex; align-items: baseline; gap: 10px; }
  .logo  { font-size: 22px; }
  .title { font-size: 18px; font-weight: 700; letter-spacing: 0.02em; }
  .subtitle { font-size: 12px; }
  .status { display: flex; gap: 10px; align-items: center; }
  main {
    max-width: 1100px;
    margin: 24px auto;
    padding: 0 24px;
    display: flex;
    flex-direction: column;
    gap: 20px;
  }
  .error {
    border-color: var(--danger);
    background: rgba(248, 81, 73, 0.08);
  }
  code {
    font-family: "SF Mono", Menlo, Consolas, monospace;
    font-size: 12px;
    background: var(--bg);
    padding: 1px 6px;
    border-radius: 3px;
    border: 1px solid var(--border);
  }
</style>
