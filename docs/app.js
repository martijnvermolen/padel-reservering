/* ==========================================================================
   Padel Reservering Dashboard - app.js
   GitHub API integration, YAML config management, UI logic
   ========================================================================== */

const DAG_NAMEN = ['Maandag', 'Dinsdag', 'Woensdag', 'Donderdag', 'Vrijdag', 'Zaterdag', 'Zondag'];
const CONFIG_PATH = 'config.yaml';
const WORKFLOW_FILE = 'reserveer.yml';

// --------------------------------------------------------------------------
// State
// --------------------------------------------------------------------------

let state = {
  token: localStorage.getItem('gh_token') || '',
  repo: localStorage.getItem('gh_repo') || 'martijnvermolen/padel-reservering',
  config: null,       // Parsed YAML config
  configSha: null,    // SHA of the config file (needed for updates)
  dirty: false,       // Has the user made changes?
};

// --------------------------------------------------------------------------
// Init
// --------------------------------------------------------------------------

document.addEventListener('DOMContentLoaded', () => {
  registerEventListeners();

  if (!state.token) {
    showSetup();
  } else {
    loadConfig();
  }
});

function registerEventListeners() {
  // Setup
  document.getElementById('setup-save').addEventListener('click', handleSetupSave);

  // Settings
  document.getElementById('btn-settings').addEventListener('click', showSettings);
  document.getElementById('settings-cancel').addEventListener('click', () => hideOverlay('settings-panel'));
  document.getElementById('settings-save').addEventListener('click', handleSettingsSave);
  document.getElementById('settings-logout').addEventListener('click', handleLogout);

  // Add day modal
  document.getElementById('btn-add-dag').addEventListener('click', () => showOverlay('modal-add-dag'));
  document.getElementById('modal-cancel').addEventListener('click', () => hideOverlay('modal-add-dag'));
  document.getElementById('modal-confirm').addEventListener('click', handleAddDay);

  // Save
  document.getElementById('btn-save').addEventListener('click', handleSave);

  // Trigger workflow
  document.getElementById('btn-trigger').addEventListener('click', handleTriggerWorkflow);
}

// --------------------------------------------------------------------------
// Setup & Auth
// --------------------------------------------------------------------------

function showSetup() {
  document.getElementById('setup-repo').value = state.repo;
  showOverlay('setup-overlay');
  document.getElementById('loading').classList.add('hidden');
}

async function handleSetupSave() {
  const token = document.getElementById('setup-token').value.trim();
  const repo = document.getElementById('setup-repo').value.trim();
  const errorEl = document.getElementById('setup-error');

  if (!token) {
    errorEl.textContent = 'Voer een token in';
    errorEl.classList.remove('hidden');
    return;
  }

  // Test the token
  try {
    errorEl.classList.add('hidden');
    const res = await ghApi(`/repos/${repo}`, token);
    if (!res.ok) {
      const data = await res.json();
      throw new Error(data.message || 'Ongeldige token of repository');
    }
  } catch (err) {
    errorEl.textContent = err.message;
    errorEl.classList.remove('hidden');
    return;
  }

  state.token = token;
  state.repo = repo;
  localStorage.setItem('gh_token', token);
  localStorage.setItem('gh_repo', repo);

  hideOverlay('setup-overlay');
  loadConfig();
}

function showSettings() {
  document.getElementById('settings-token').value = state.token;
  document.getElementById('settings-repo').value = state.repo;

  if (state.config) {
    const baan = (state.config.reservering?.baan_voorkeur || []).join(', ');
    document.getElementById('settings-baan').value = baan;
    document.getElementById('settings-email').value = state.config.email?.ontvanger || '';
  }

  showOverlay('settings-panel');
}

function handleSettingsSave() {
  const token = document.getElementById('settings-token').value.trim();
  const repo = document.getElementById('settings-repo').value.trim();

  if (token) {
    state.token = token;
    localStorage.setItem('gh_token', token);
  }
  if (repo) {
    state.repo = repo;
    localStorage.setItem('gh_repo', repo);
  }

  if (state.config) {
    const baanStr = document.getElementById('settings-baan').value.trim();
    state.config.reservering.baan_voorkeur = baanStr
      ? baanStr.split(',').map(s => parseInt(s.trim())).filter(n => !isNaN(n))
      : [];

    const email = document.getElementById('settings-email').value.trim();
    if (email && state.config.email) {
      state.config.email.ontvanger = email;
      state.config.email.afzender = email;
    }

    markDirty();
  }

  hideOverlay('settings-panel');
}

function handleLogout() {
  localStorage.removeItem('gh_token');
  localStorage.removeItem('gh_repo');
  state.token = '';
  state.config = null;
  hideOverlay('settings-panel');
  document.getElementById('section-status').classList.add('hidden');
  document.getElementById('section-dagen').classList.add('hidden');
  showSetup();
}

// --------------------------------------------------------------------------
// GitHub API
// --------------------------------------------------------------------------

function ghApi(path, token) {
  token = token || state.token;
  return fetch(`https://api.github.com${path}`, {
    headers: {
      'Authorization': `Bearer ${token}`,
      'Accept': 'application/vnd.github.v3+json',
    },
  });
}

function ghApiPost(path, body) {
  return fetch(`https://api.github.com${path}`, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${state.token}`,
      'Accept': 'application/vnd.github.v3+json',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(body),
  });
}

function ghApiPut(path, body) {
  return fetch(`https://api.github.com${path}`, {
    method: 'PUT',
    headers: {
      'Authorization': `Bearer ${state.token}`,
      'Accept': 'application/vnd.github.v3+json',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(body),
  });
}

// --------------------------------------------------------------------------
// Load config
// --------------------------------------------------------------------------

async function loadConfig() {
  const loadingEl = document.getElementById('loading');
  loadingEl.classList.remove('hidden');

  try {
    // Load config.yaml
    const res = await ghApi(`/repos/${state.repo}/contents/${CONFIG_PATH}`);
    if (!res.ok) throw new Error('Kon config.yaml niet laden');

    const data = await res.json();
    state.configSha = data.sha;

    const content = atob(data.content);
    state.config = jsyaml.load(content);

    renderDagen();
    loadWorkflowRuns();

    document.getElementById('section-status').classList.remove('hidden');
    document.getElementById('section-dagen').classList.remove('hidden');
  } catch (err) {
    showToast('Fout: ' + err.message, 'error');
    console.error(err);
  } finally {
    loadingEl.classList.add('hidden');
  }
}

// --------------------------------------------------------------------------
// Load workflow runs
// --------------------------------------------------------------------------

async function loadWorkflowRuns() {
  try {
    const res = await ghApi(`/repos/${state.repo}/actions/runs?per_page=5`);
    if (!res.ok) return;

    const data = await res.json();
    const container = document.getElementById('workflow-runs');
    container.innerHTML = '';

    if (!data.workflow_runs || data.workflow_runs.length === 0) {
      container.innerHTML = '<p style="color: var(--text-muted); font-size: 14px; padding: 8px 0;">Nog geen reserveringspogingen.</p>';
      return;
    }

    for (const run of data.workflow_runs) {
      const statusClass = run.conclusion === 'success' ? 'success'
        : run.conclusion === 'failure' ? 'failure'
        : 'pending';

      const statusText = run.conclusion === 'success' ? 'Gelukt'
        : run.conclusion === 'failure' ? 'Mislukt'
        : run.status === 'in_progress' ? 'Bezig...'
        : 'Wachtend';

      const date = new Date(run.created_at);
      const dateStr = date.toLocaleDateString('nl-NL', {
        weekday: 'short', day: 'numeric', month: 'short',
        hour: '2-digit', minute: '2-digit'
      });

      container.innerHTML += `
        <div class="run-item">
          <div class="run-status ${statusClass}"></div>
          <div class="run-info">
            <div class="run-title">${statusText}</div>
            <div class="run-meta">${dateStr}</div>
          </div>
          <a href="${run.html_url}" target="_blank" rel="noopener" class="run-link" title="Bekijk op GitHub">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
          </a>
        </div>`;
    }
  } catch (err) {
    console.error('Kon workflow runs niet laden:', err);
  }
}

// --------------------------------------------------------------------------
// Trigger workflow
// --------------------------------------------------------------------------

async function handleTriggerWorkflow() {
  const btn = document.getElementById('btn-trigger');
  btn.disabled = true;
  btn.textContent = 'Starten...';

  try {
    // Get the default branch
    const repoRes = await ghApi(`/repos/${state.repo}`);
    const repoData = await repoRes.json();
    const branch = repoData.default_branch || 'master';

    const res = await ghApiPost(
      `/repos/${state.repo}/actions/workflows/${WORKFLOW_FILE}/dispatches`,
      { ref: branch }
    );

    if (res.ok || res.status === 204) {
      showToast('Reservering gestart! Controleer status over ~5 minuten.', 'success');
      // Refresh runs after a short delay
      setTimeout(loadWorkflowRuns, 5000);
    } else {
      const data = await res.json();
      throw new Error(data.message || 'Kon workflow niet starten');
    }
  } catch (err) {
    showToast('Fout: ' + err.message, 'error');
  } finally {
    btn.disabled = false;
    btn.innerHTML = `
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"/></svg>
      Nu reserveren`;
  }
}

// --------------------------------------------------------------------------
// Render days
// --------------------------------------------------------------------------

function renderDagen() {
  const container = document.getElementById('dagen-container');
  container.innerHTML = '';

  const dagen = state.config?.reservering?.dagen || [];

  for (let i = 0; i < dagen.length; i++) {
    const dag = dagen[i];
    const dagNaam = DAG_NAMEN[dag.dag] || `Dag ${dag.dag}`;
    const tijden = dag.tijden || [];
    const spelers = getSpelersVoorDag(dag.dag);

    const card = document.createElement('div');
    card.className = 'day-card';
    card.dataset.index = i;

    card.innerHTML = `
      <div class="day-card-header">
        <div>
          <div class="day-card-title">${dagNaam}</div>
          <div class="day-card-time">${tijden[0] || '--:--'}</div>
        </div>
        <button class="day-card-delete" data-dag-index="${i}" title="Dag verwijderen">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
        </button>
      </div>

      <div class="times-section">
        <div class="times-label">Tijden (eerste = voorkeur, rest = fallback)</div>
        <div class="times-list" data-dag-index="${i}">
          ${tijden.map((t, ti) => `
            <div class="time-chip">
              ${t}
              <button class="remove-btn" data-dag-index="${i}" data-time-index="${ti}" title="Verwijder">&times;</button>
            </div>
          `).join('')}
          <button class="add-time-btn" data-dag-index="${i}">+ Tijd</button>
        </div>
      </div>

      <div class="players-section">
        <div class="players-label">Spelers</div>
        <div class="player-row">
          <span class="player-name">Martijn Vermolen <span class="you-badge">Jij</span></span>
        </div>
        ${spelers.map((s, si) => `
          <div class="player-row">
            <span class="player-name">${escapeHtml(s)}</span>
            <button class="player-remove" data-dag="${dag.dag}" data-player-index="${si}" title="Verwijder">&times;</button>
          </div>
        `).join('')}
        <div class="add-player-row">
          <input type="text" placeholder="Naam toevoegen..." data-dag="${dag.dag}">
          <button data-dag="${dag.dag}">Voeg toe</button>
        </div>
      </div>
    `;

    container.appendChild(card);
  }

  // Event listeners for dynamically created elements
  attachDayCardListeners();
}

function attachDayCardListeners() {
  // Delete day
  document.querySelectorAll('.day-card-delete').forEach(btn => {
    btn.addEventListener('click', (e) => {
      const idx = parseInt(e.currentTarget.dataset.dagIndex);
      handleDeleteDay(idx);
    });
  });

  // Remove time
  document.querySelectorAll('.time-chip .remove-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      const dagIdx = parseInt(e.currentTarget.dataset.dagIndex);
      const timeIdx = parseInt(e.currentTarget.dataset.timeIndex);
      handleRemoveTime(dagIdx, timeIdx);
    });
  });

  // Add time
  document.querySelectorAll('.add-time-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      const dagIdx = parseInt(e.currentTarget.dataset.dagIndex);
      handleAddTime(dagIdx);
    });
  });

  // Remove player
  document.querySelectorAll('.player-remove').forEach(btn => {
    btn.addEventListener('click', (e) => {
      const dag = parseInt(e.currentTarget.dataset.dag);
      const playerIdx = parseInt(e.currentTarget.dataset.playerIndex);
      handleRemovePlayer(dag, playerIdx);
    });
  });

  // Add player (button click)
  document.querySelectorAll('.add-player-row button').forEach(btn => {
    btn.addEventListener('click', (e) => {
      const dag = parseInt(e.currentTarget.dataset.dag);
      const input = e.currentTarget.parentElement.querySelector('input');
      handleAddPlayer(dag, input);
    });
  });

  // Add player (enter key)
  document.querySelectorAll('.add-player-row input').forEach(input => {
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        const dag = parseInt(e.currentTarget.dataset.dag);
        handleAddPlayer(dag, e.currentTarget);
      }
    });
  });
}

// --------------------------------------------------------------------------
// Data helpers
// --------------------------------------------------------------------------

function getSpelersVoorDag(dagNr) {
  const perDag = state.config?.medespelers?.spelers_per_dag;
  if (perDag && perDag[dagNr]) {
    return perDag[dagNr];
  }
  return state.config?.medespelers?.standaard_spelers || [];
}

function setSpelersVoorDag(dagNr, spelers) {
  if (!state.config.medespelers) {
    state.config.medespelers = {};
  }
  if (!state.config.medespelers.spelers_per_dag) {
    state.config.medespelers.spelers_per_dag = {};
  }
  state.config.medespelers.spelers_per_dag[dagNr] = [...spelers];
}

// --------------------------------------------------------------------------
// Day actions
// --------------------------------------------------------------------------

function handleDeleteDay(index) {
  if (!confirm(`${DAG_NAMEN[state.config.reservering.dagen[index].dag]} verwijderen?`)) return;

  const dagNr = state.config.reservering.dagen[index].dag;
  state.config.reservering.dagen.splice(index, 1);

  // Also remove player config for this day
  if (state.config.medespelers?.spelers_per_dag?.[dagNr]) {
    delete state.config.medespelers.spelers_per_dag[dagNr];
  }

  markDirty();
  renderDagen();
}

function handleAddDay() {
  const dagNr = parseInt(document.getElementById('new-dag-select').value);
  const tijd = document.getElementById('new-dag-tijd').value || '20:00';

  // Check if day already exists
  const exists = state.config.reservering.dagen.some(d => d.dag === dagNr);
  if (exists) {
    showToast(`${DAG_NAMEN[dagNr]} bestaat al`, 'error');
    return;
  }

  state.config.reservering.dagen.push({
    dag: dagNr,
    tijden: [tijd],
  });

  // Sort days by dag number
  state.config.reservering.dagen.sort((a, b) => a.dag - b.dag);

  // Add default players
  setSpelersVoorDag(dagNr, state.config.medespelers?.standaard_spelers || []);

  hideOverlay('modal-add-dag');
  markDirty();
  renderDagen();
}

// --------------------------------------------------------------------------
// Time actions
// --------------------------------------------------------------------------

function handleRemoveTime(dagIndex, timeIndex) {
  const tijden = state.config.reservering.dagen[dagIndex].tijden;
  if (tijden.length <= 1) {
    showToast('Minimaal 1 tijdstip nodig', 'error');
    return;
  }
  tijden.splice(timeIndex, 1);
  markDirty();
  renderDagen();
}

function handleAddTime(dagIndex) {
  const tijd = prompt('Voer een tijd in (bijv. 20:30):');
  if (!tijd) return;

  // Validate time format
  if (!/^\d{1,2}:\d{2}$/.test(tijd)) {
    showToast('Ongeldige tijd. Gebruik formaat HH:MM', 'error');
    return;
  }

  state.config.reservering.dagen[dagIndex].tijden.push(tijd);
  markDirty();
  renderDagen();
}

// --------------------------------------------------------------------------
// Player actions
// --------------------------------------------------------------------------

function handleRemovePlayer(dagNr, playerIndex) {
  const spelers = getSpelersVoorDag(dagNr);
  if (spelers.length <= 1) {
    showToast('Minimaal 1 medespeler nodig', 'error');
    return;
  }
  spelers.splice(playerIndex, 1);
  setSpelersVoorDag(dagNr, spelers);
  markDirty();
  renderDagen();
}

function handleAddPlayer(dagNr, inputEl) {
  const naam = inputEl.value.trim();
  if (!naam) return;

  const spelers = getSpelersVoorDag(dagNr);
  if (spelers.length >= 3) {
    showToast('Maximaal 3 medespelers (+ jijzelf = 4)', 'error');
    return;
  }

  if (spelers.includes(naam)) {
    showToast('Speler is al toegevoegd', 'error');
    return;
  }

  spelers.push(naam);
  setSpelersVoorDag(dagNr, spelers);

  inputEl.value = '';
  markDirty();
  renderDagen();
}

// --------------------------------------------------------------------------
// Save to GitHub
// --------------------------------------------------------------------------

async function handleSave() {
  const btn = document.getElementById('btn-save');
  const statusEl = document.getElementById('save-status');
  btn.disabled = true;
  statusEl.textContent = 'Opslaan...';

  try {
    // Generate YAML with comments
    const yamlContent = generateYaml();

    // Base64 encode
    const encoded = btoa(unescape(encodeURIComponent(yamlContent)));

    const res = await ghApiPut(`/repos/${state.repo}/contents/${CONFIG_PATH}`, {
      message: 'Config bijgewerkt via dashboard',
      content: encoded,
      sha: state.configSha,
    });

    if (!res.ok) {
      const data = await res.json();
      throw new Error(data.message || 'Opslaan mislukt');
    }

    const data = await res.json();
    state.configSha = data.content.sha;
    state.dirty = false;

    hideSaveBar();
    showToast('Configuratie opgeslagen!', 'success');
  } catch (err) {
    showToast('Fout bij opslaan: ' + err.message, 'error');
    statusEl.textContent = 'Opslaan mislukt - probeer opnieuw';
  } finally {
    btn.disabled = false;
  }
}

function generateYaml() {
  // Build a clean config object for YAML serialization
  const config = state.config;

  // Generate YAML
  const yamlStr = jsyaml.dump(config, {
    indent: 2,
    lineWidth: 120,
    noRefs: true,
    sortKeys: false,
    quotingType: '"',
    forceQuotes: false,
  });

  // Add header comment
  return `# =============================================================================
# Configuratie voor automatische padelbaan reservering - TPV Heksenwiel
# Bijgewerkt via het dashboard op ${new Date().toLocaleString('nl-NL')}
# =============================================================================

${yamlStr}`;
}

// --------------------------------------------------------------------------
// Dirty state / save bar
// --------------------------------------------------------------------------

function markDirty() {
  state.dirty = true;
  showSaveBar();
}

function showSaveBar() {
  const bar = document.getElementById('save-bar');
  bar.classList.remove('hidden');
  document.getElementById('save-status').textContent = 'Niet-opgeslagen wijzigingen';
  // Force reflow for animation
  requestAnimationFrame(() => {
    bar.classList.add('visible');
  });
}

function hideSaveBar() {
  const bar = document.getElementById('save-bar');
  bar.classList.remove('visible');
  setTimeout(() => bar.classList.add('hidden'), 300);
}

// --------------------------------------------------------------------------
// UI helpers
// --------------------------------------------------------------------------

function showOverlay(id) {
  document.getElementById(id).classList.remove('hidden');
}

function hideOverlay(id) {
  document.getElementById(id).classList.add('hidden');
}

function showToast(message, type = 'info') {
  const toast = document.getElementById('toast');
  toast.textContent = message;
  toast.className = `toast toast-${type} show`;

  clearTimeout(toast._timeout);
  toast._timeout = setTimeout(() => {
    toast.classList.remove('show');
  }, 3500);
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

// --------------------------------------------------------------------------
// Service Worker registration
// --------------------------------------------------------------------------

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('sw.js').catch(() => {
    // SW registration failed - that's OK, app still works
  });
}
