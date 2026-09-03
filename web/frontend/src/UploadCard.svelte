<script>
  import { submitJob, listModels, ApiError } from "./api.js";

  let { onSubmitted = () => {} } = $props();

  let file = $state(null);
  let filename = $state("");
  let model = $state("tiny");
  let language = $state("ko");
  let device = $state("auto");
  let computeType = $state("auto");
  let noiseDb = $state(-30.0);
  let minSilence = $state(1.5);
  let margin = $state(0.3);
  let minKeepSeconds = $state(0.15);
  let noSilence = $state(false);
  let noSubtitles = $state(false);
  let projectName = $state("Auto Edit");
  let eventName = $state("veauto");
  let submitting = $state(false);
  let error = $state("");

  let models = $state(["tiny", "base", "small", "medium", "large-v3", "distil-large-v3"]);

  async function refreshModels() {
    try {
      const remote = await listModels();
      if (Array.isArray(remote) && remote.length) {
        models = remote;
      }
    } catch (_e) { /* keep static list */ }
  }
  refreshModels();

  function onFile(e) {
    const t = e.target;
    if (t.files && t.files.length) {
      file = t.files[0];
      filename = file.name;
    }
  }

  function onDrop(e) {
    e.preventDefault();
    if (e.dataTransfer.files && e.dataTransfer.files.length) {
      file = e.dataTransfer.files[0];
      filename = file.name;
    }
  }
  function onDragOver(e) { e.preventDefault(); }

  async function onSubmit(e) {
    e.preventDefault();
    if (!file || submitting) return;
    error = "";
    submitting = true;
    try {
      const job = await submitJob(file, {
        model, language, device, compute_type: computeType,
        noise_db: noiseDb, min_silence: minSilence, margin,
        min_keep_seconds: minKeepSeconds,
        no_silence: noSilence, no_subtitles: noSubtitles,
        project_name: projectName, event_name: eventName,
      });
      onSubmitted(job);
      // Reset file input but keep options
      file = null;
      filename = "";
      const input = document.getElementById("file-input");
      if (input) input.value = "";
    } catch (e) {
      error = e instanceof ApiError
        ? `API error: ${e.status} ${e.body || e.statusText}`
        : `Submit failed: ${e?.message || e}`;
    } finally {
      submitting = false;
    }
  }
</script>

<section class="card">
  <h2>New job</h2>
  <form onsubmit={onSubmit}>
    <div
      class="dropzone"
      class:has-file={!!filename}
      ondrop={onDrop}
      ondragover={onDragOver}
      role="button"
      tabindex="0"
    >
      <input id="file-input" type="file" accept="video/*,audio/*" onchange={onFile} />
      <p class="dz-text">
        {#if filename}
          <strong>{filename}</strong>
        {:else}
          Drag &amp; drop a video, or
          <label for="file-input" class="link">browse</label>
        {/if}
      </p>
    </div>

    <details open>
      <summary>Options</summary>
      <div class="grid">
        <label>Model
          <select bind:value={model}>
            {#each models as m}<option value={m}>{m}</option>{/each}
          </select>
        </label>
        <label>Language
          <input type="text" bind:value={language} placeholder="ko / en / auto" />
        </label>
        <label>Device
          <select bind:value={device}>
            <option value="auto">auto</option>
            <option value="cpu">cpu</option>
            <option value="cuda">cuda</option>
            <option value="mps">mps</option>
          </select>
        </label>
        <label>Compute type
          <select bind:value={computeType}>
            <option value="auto">auto</option>
            <option value="int8">int8</option>
            <option value="int8_float16">int8_float16</option>
            <option value="float16">float16</option>
            <option value="float32">float32</option>
          </select>
        </label>
        <label>Noise (dB)
          <input type="number" step="1" bind:value={noiseDb} />
        </label>
        <label>Min silence (s)
          <input type="number" step="0.1" min="0.1" bind:value={minSilence} />
        </label>
        <label>Margin (s)
          <input type="number" step="0.05" min="0" bind:value={margin} />
        </label>
        <label>Min keep (s)
          <input type="number" step="0.05" min="0" bind:value={minKeepSeconds} />
        </label>
        <label>Project name
          <input type="text" bind:value={projectName} />
        </label>
        <label>Event name
          <input type="text" bind:value={eventName} />
        </label>
        <label class="checkbox">
          <input type="checkbox" bind:checked={noSilence} /> Skip silence removal
        </label>
        <label class="checkbox">
          <input type="checkbox" bind:checked={noSubtitles} /> Skip subtitles
        </label>
      </div>
    </details>

    {#if error}
      <p class="error">{error}</p>
    {/if}

    <button class="primary" type="submit" disabled={!file || submitting}>
      {submitting ? "Submitting…" : "Submit job"}
    </button>
  </form>
</section>

<style>
  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 1.2rem;
    margin-bottom: 1.5rem;
  }
  h2 { margin: 0 0 0.8rem; font-size: 1.1rem; }
  .dropzone {
    border: 2px dashed var(--border);
    border-radius: 6px;
    padding: 1.5rem;
    text-align: center;
    transition: border-color 0.2s;
    background: var(--bg);
  }
  .dropzone.has-file { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 5%, var(--bg)); }
  .dropzone input[type="file"] { display: none; }
  .dz-text { margin: 0; color: var(--muted); }
  .has-file .dz-text { color: var(--fg); }
  .link { color: var(--accent); cursor: pointer; }
  details { margin: 1rem 0; }
  summary { cursor: pointer; color: var(--muted); }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 0.6rem 1rem;
    margin-top: 0.8rem;
  }
  label { display: flex; flex-direction: column; font-size: 0.85rem; color: var(--muted); gap: 0.25rem; }
  label.checkbox { flex-direction: row; align-items: center; gap: 0.5rem; }
  input, select {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 0.4rem 0.5rem;
    color: var(--fg);
    font: inherit;
  }
  input:focus, select:focus {
    outline: 2px solid var(--accent);
    outline-offset: 0;
  }
  button.primary {
    background: var(--accent);
    color: white;
    border: 0;
    padding: 0.6rem 1.2rem;
    border-radius: 6px;
    font-weight: 600;
    cursor: pointer;
  }
  button.primary:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .error {
    color: var(--danger);
    background: color-mix(in srgb, var(--danger) 10%, var(--bg));
    padding: 0.5rem 0.8rem;
    border-radius: 4px;
    margin: 0.6rem 0;
  }
</style>

