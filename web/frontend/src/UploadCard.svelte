<script>
  import { submitJob, listModels, ApiError } from "./api.js";

  /** @type {(job: any) => void} */
  export let onSubmitted;

  let file = null;
  let filename = "";
  let models = ["tiny", "base", "small", "medium", "large-v3", "distil-large-v3"];
  let model = "tiny";
  let language = "ko";
  let device = "auto";
  let computeType = "auto";
  let noiseDb = -30.0;
  let minSilence = 1.5;
  let margin = 0.2;
  let noSilence = false;
  let noSubtitles = false;
  let projectName = "Auto Edit";
  let eventName = "veauto";
  let submitting = false;
  let error = "";

  // Best-effort model refresh (backend exposes /api/config/models? but
  // we keep a local list as fallback for offline / first-load).
  let modelListTried = false;
  async function refreshModels() {
    try {
      const remote = await listModels();
      if (Array.isArray(remote) && remote.length) {
        models = remote;
      }
    } catch (e) {
      // ignore — keep static list
    } finally {
      modelListTried = true;
    }
  }
  refreshModels();

  /** @param {Event} e */
  function onFile(e) {
    const t = e.target;
    if (t.files && t.files.length) {
      file = t.files[0];
      filename = file.name;
    }
  }

  /** @param {DragEvent} e */
  function onDrop(e) {
    e.preventDefault();
    if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
      file = e.dataTransfer.files[0];
      filename = file.name;
    }
  }
  function onDragOver(e) {
    e.preventDefault();
  }

  async function submit() {
    if (!file || submitting) return;
    submitting = true;
    error = "";
    try {
      const job = await submitJob({
        file,
        model,
        language,
        device,
        computeType,
        noiseDb,
        minSilence,
        margin,
        noSilence,
        noSubtitles,
        projectName,
        eventName,
      });
      onSubmitted(job);
      // reset
      file = null;
      filename = "";
    } catch (e) {
      if (e instanceof ApiError) {
        error = e.message;
      } else {
        error = String(e);
      }
    } finally {
      submitting = false;
    }
  }
</script>

<section class="upload-card card">
  <h2>Upload &amp; submit</h2>
  <p class="muted">
    Drop a video file or pick one. The pipeline will run in the background;
    you can submit more files while it processes.
  </p>

  <div
    class="dropzone"
    class:active={filename}
    on:drop={onDrop}
    on:dragover={onDragOver}
    role="region"
    aria-label="File drop zone"
  >
    {#if filename}
      <strong>📄 {filename}</strong>
    {:else}
      <span>Drop a video here, or pick one below</span>
    {/if}
    <input type="file" accept="video/*,audio/*" on:change={onFile} />
  </div>

  <div class="grid">
    <label>
      <span>Whisper model</span>
      <select bind:value={model} disabled={submitting}>
        {#each models as m}
          <option value={m}>{m}</option>
        {/each}
      </select>
    </label>
    <label>
      <span>Language</span>
      <input type="text" bind:value={language} placeholder="ko / en / …" disabled={submitting} />
    </label>
    <label>
      <span>Device</span>
      <select bind:value={device} disabled={submitting}>
        <option value="auto">auto</option>
        <option value="cpu">cpu</option>
        <option value="cuda">cuda</option>
        <option value="mps">mps</option>
      </select>
    </label>
    <label>
      <span>Compute type</span>
      <select bind:value={computeType} disabled={submitting}>
        <option value="auto">auto</option>
        <option value="int8">int8</option>
        <option value="int8_float16">int8_float16</option>
        <option value="float16">float16</option>
        <option value="float32">float32</option>
      </select>
    </label>
    <label>
      <span>Noise dB</span>
      <input type="number" step="1" bind:value={noiseDb} disabled={submitting || noSilence} />
    </label>
    <label>
      <span>Min silence (s)</span>
      <input type="number" step="0.1" min="0.1" bind:value={minSilence} disabled={submitting || noSilence} />
    </label>
    <label>
      <span>Margin (s)</span>
      <input type="number" step="0.05" min="0" bind:value={margin} disabled={submitting || noSilence} />
    </label>
    <label class="check">
      <input type="checkbox" bind:checked={noSilence} disabled={submitting} />
      <span>Skip silence removal</span>
    </label>
    <label class="check">
      <input type="checkbox" bind:checked={noSubtitles} disabled={submitting} />
      <span>Skip subtitles</span>
    </label>
    <label>
      <span>Project name</span>
      <input type="text" bind:value={projectName} disabled={submitting} />
    </label>
    <label>
      <span>Event name</span>
      <input type="text" bind:value={eventName} disabled={submitting} />
    </label>
  </div>

  {#if error}
    <p class="error">⚠ {error}</p>
  {/if}

  <button
    class="primary"
    on:click={submit}
    disabled={!file || submitting}
  >
    {submitting ? "Uploading…" : "Submit job"}
  </button>
</section>

<style>
  .dropzone {
    border: 2px dashed var(--border);
    border-radius: 8px;
    padding: 1.2rem;
    text-align: center;
    background: var(--bg-alt);
    margin: 0.8rem 0;
    position: relative;
  }
  .dropzone.active {
    border-color: var(--accent);
    background: color-mix(in srgb, var(--accent) 8%, var(--bg-alt));
  }
  .dropzone input[type="file"] {
    display: block;
    margin: 0.6rem auto 0;
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
    gap: 0.6rem;
    margin: 0.8rem 0;
  }
  label {
    display: flex;
    flex-direction: column;
    font-size: 0.85rem;
    color: var(--fg-dim);
  }
  label.check {
    flex-direction: row;
    align-items: center;
    gap: 0.4rem;
  }
  label.check span {
    color: var(--fg);
  }
  input,
  select {
    margin-top: 0.2rem;
    padding: 0.35rem 0.5rem;
    border: 1px solid var(--border);
    border-radius: 4px;
    background: var(--bg);
    color: var(--fg);
    font: inherit;
  }
  input:focus,
  select:focus {
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

