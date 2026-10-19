const form = document.querySelector('#client-form');
const rows = document.querySelector('#client-rows');
const notice = document.querySelector('#notice');
const searchInput = document.querySelector('#search');
const loginScreen = document.querySelector('#login-screen');
const appShell = document.querySelector('#app-shell');
let offset = 0;
const limit = 50;
let editingCode = null;

function notify(message, kind = 'success') {
  notice.textContent = message;
  notice.className = `notice ${kind}`;
  notice.hidden = false;
  window.scrollTo({top: 0, behavior: 'smooth'});
  setTimeout(() => { notice.hidden = true; }, 5000);
}

async function request(url, options = {}) {
  const token = sessionStorage.getItem('access_token');
  const headers = {'Content-Type': 'application/json', ...(token ? {Authorization: `Bearer ${token}`} : {}), ...(options.headers || {})};
  const response = await fetch(url, {...options, headers});
  if (response.status === 401 && token) {
    sessionStorage.removeItem('access_token');
    appShell.hidden = true;
    loginScreen.hidden = false;
  }
  if (!response.ok) {
    let message = 'Une erreur est survenue.';
    try { message = (await response.json()).detail || message; } catch {}
    throw new Error(message);
  }
  return response.status === 204 ? null : response.json();
}

function resetForm() {
  form.reset();
  form.elements.Type_Client.value = 'P';
  form.elements.Exonerer.value = 'N';
  editingCode = null;
  form.elements.code_client.disabled = false;
  document.querySelector('#form-title').textContent = 'Nouveau client';
  document.querySelector('#save-btn').textContent = 'Enregistrer le client';
}

function formData() {
  const data = {};
  for (const [key, value] of new FormData(form)) {
    if (value !== '') data[key] = ['cp', 'cp_b'].includes(key) ? Number(value) : value;
  }
  data.code_client = editingCode || form.elements.code_client.value.trim();
  return data;
}

async function loadClients() {
  rows.innerHTML = '<tr><td colspan="6" class="empty">Chargement…</td></tr>';
  try {
    const query = new URLSearchParams({q: searchInput.value.trim(), limit, offset});
    const data = await request(`/api/clients?${query}`);
    document.querySelector('#client-count').textContent = data.items.length;
    document.querySelector('#list-status').textContent = `Affichage ${data.items.length ? offset + 1 : 0}–${offset + data.items.length}`;
    document.querySelector('#prev-btn').disabled = offset === 0;
    document.querySelector('#next-btn').disabled = data.items.length < limit;
    if (!data.items.length) {
      rows.innerHTML = '<tr><td colspan="6" class="empty">Aucun client trouvé.</td></tr>';
      return;
    }
    rows.innerHTML = data.items.map(client => `<tr>
      <td class="client-code">${escapeHtml(client.code_client)}</td>
      <td>${escapeHtml(client.nom_client || [client.Nom, client.Prenom].filter(Boolean).join(' ') || '—')}</td>
      <td>${escapeHtml(client.ville || '—')}</td><td>${escapeHtml(client.tel || client.tel_b || '—')}</td>
      <td>${client.Type_Client === 'S' ? 'Société' : 'Particulier'}</td>
      <td class="row-actions"><button data-edit="${escapeHtml(client.code_client)}">Modifier</button><button class="delete" data-delete="${escapeHtml(client.code_client)}">Supprimer</button></td>
    </tr>`).join('');
  } catch (error) {
    rows.innerHTML = `<tr><td colspan="6" class="empty">${escapeHtml(error.message)}</td></tr>`;
    document.querySelector('#client-count').textContent = '—';
  }
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
}

async function editClient(code) {
  try {
    const client = await request(`/api/clients/${encodeURIComponent(code)}`);
    resetForm();
    for (const [key, value] of Object.entries(client)) {
      const control = form.elements.namedItem(key);
      if (control && value != null) control.value = key.startsWith('Date') ? String(value).slice(0, 10) : value;
    }
    editingCode = code;
    form.elements.code_client.disabled = true;
    document.querySelector('#form-title').textContent = `Modifier le client ${code}`;
    document.querySelector('#save-btn').textContent = 'Enregistrer les modifications';
    form.scrollIntoView({behavior: 'smooth', block: 'start'});
  } catch (error) { notify(error.message, 'error'); }
}

rows.addEventListener('click', async event => {
  const editCode = event.target.dataset.edit;
  const deleteCode = event.target.dataset.delete;
  if (editCode) return editClient(editCode);
  if (deleteCode && confirm(`Supprimer le client ${deleteCode} ?`)) {
    try { await request(`/api/clients/${encodeURIComponent(deleteCode)}`, {method: 'DELETE'}); notify('Client supprimé.'); await loadClients(); }
    catch (error) { notify(error.message, 'error'); }
  }
});

form.addEventListener('submit', async event => {
  event.preventDefault();
  const data = formData();
  try {
    await request(editingCode ? `/api/clients/${encodeURIComponent(editingCode)}` : '/api/clients', {
      method: editingCode ? 'PUT' : 'POST', body: JSON.stringify(data)
    });
    notify(editingCode ? 'Modifications enregistrées.' : 'Client créé.');
    resetForm(); offset = 0; await loadClients();
  } catch (error) { notify(error.message, 'error'); }
});

document.querySelector('#new-btn').addEventListener('click', resetForm);
document.querySelector('#search-btn').addEventListener('click', () => { offset = 0; loadClients(); });
searchInput.addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); offset = 0; loadClients(); } });
document.querySelector('#prev-btn').addEventListener('click', () => { offset = Math.max(0, offset - limit); loadClients(); });
document.querySelector('#next-btn').addEventListener('click', () => { offset += limit; loadClients(); });

document.querySelector('#login-form').addEventListener('submit', async event => {
  event.preventDefault();
  const errorBox = document.querySelector('#login-error');
  const button = event.currentTarget.querySelector('button[type="submit"]');
  const data = new FormData(event.currentTarget);
  errorBox.hidden = true;
  button.disabled = true;
  try {
    const result = await request('/api/auth/login', {method: 'POST', body: JSON.stringify({username: data.get('username').trim(), password: data.get('password')})});
    sessionStorage.setItem('access_token', result.access_token);
    document.querySelector('#current-user').textContent = result.user.username;
    loginScreen.hidden = true;
    appShell.hidden = false;
    event.currentTarget.reset();
    offset = 0;
    await loadClients();
  } catch (error) {
    errorBox.textContent = error.message;
    errorBox.hidden = false;
  } finally { button.disabled = false; }
});

document.querySelector('#logout-btn').addEventListener('click', () => {
  sessionStorage.removeItem('access_token');
  appShell.hidden = true;
  loginScreen.hidden = false;
  document.querySelector('#login-form').reset();
});

async function restoreSession() {
  if (!sessionStorage.getItem('access_token')) return;
  try {
    const user = await request('/api/auth/me');
    document.querySelector('#current-user').textContent = user.username;
    loginScreen.hidden = true;
    appShell.hidden = false;
    await loadClients();
  } catch { sessionStorage.removeItem('access_token'); }
}

restoreSession();

const expertModal = document.querySelector('#expert-modal');
const expertForm = document.querySelector('#expert-form');
const expertMessage = document.querySelector('#expert-message');
const expertListPanel = document.querySelector('#expert-list-panel');
let expertLookups = null;
let selectedExpertCode = null;
let editingExpertCode = null;

function expertNotice(message, isError = false) {
  expertMessage.textContent = message;
  expertMessage.className = `expert-message${isError ? ' error' : ''}`;
  expertMessage.hidden = false;
}

function fillExpertSelect(select, items, placeholder, selectedValue = '') {
  select.innerHTML = `<option value="">${escapeHtml(placeholder)}</option>` + items.map(item =>
    `<option value="${escapeHtml(item.id)}" ${String(item.id) === String(selectedValue) ? 'selected' : ''}>${escapeHtml(item.label || '')}</option>`
  ).join('');
}

function renderExpertChecks(containerId, items, name, selected = []) {
  const container = document.querySelector(`#${containerId}`);
  const checked = new Set(selected.map(String));
  container.innerHTML = items.map(item => `<label class="check-option"><input type="checkbox" name="${name}" value="${escapeHtml(item.id)}" ${checked.has(String(item.id)) ? 'checked' : ''}><span>${escapeHtml(item.label || '')}</span></label>`).join('');
}

async function loadExpertLookups() {
  expertLookups = await request('/api/prestataires/lookups');
  fillExpertSelect(document.querySelector('#expert-family'), expertLookups.families, 'Choisir une famille');
  fillExpertSelect(document.querySelector('#expert-region'), expertLookups.regions, 'Choisir une région');
  document.querySelector('#expert-note').innerHTML = '<option value="">Choisir une note</option>' + expertLookups.notes.map(note => `<option value="${escapeHtml(note.value)}">${escapeHtml(note.label)}</option>`).join('');
  renderExpertChecks('expert-companies', expertLookups.companies, 'Compagnies');
  renderExpertChecks('expert-branches', expertLookups.branches, 'Branches');
}

function resetExpertForm() {
  expertForm.reset();
  expertForm.elements.Taux_RS.value = '0';
  selectedExpertCode = null;
  editingExpertCode = null;
  expertForm.elements.Code_Prestataire.disabled = false;
  document.querySelector('#expert-selected-label').textContent = 'Nouveau prestataire';
  document.querySelector('#expert-save').textContent = 'Enregistrer le prestataire';
  if (expertLookups) {
    renderExpertChecks('expert-companies', expertLookups.companies, 'Compagnies');
    renderExpertChecks('expert-branches', expertLookups.branches, 'Branches');
  }
}

async function openExpertModule() {
  expertModal.hidden = false;
  expertMessage.hidden = true;
  try {
    if (!expertLookups) await loadExpertLookups();
    await loadExpertList();
  } catch (error) { expertNotice(error.message, true); }
}

async function loadExpertList() {
  const q = document.querySelector('#expert-search').value.trim();
  const data = await request(`/api/prestataires?${new URLSearchParams({q, limit: 200, offset: 0})}`);
  const familyNames = new Map(expertLookups.families.map(item => [String(item.id), item.label]));
  const regionNames = new Map(expertLookups.regions.map(item => [String(item.id), item.label]));
  const body = document.querySelector('#expert-rows');
  if (!data.items.length) {
    body.innerHTML = '<tr><td colspan="4" class="empty">Aucun prestataire trouvé.</td></tr>';
    return;
  }
  body.innerHTML = data.items.map(item => `<tr data-code="${escapeHtml(item.Code_Prestataire)}" class="${String(item.Code_Prestataire) === String(selectedExpertCode) ? 'selected' : ''}"><td>${escapeHtml(item.Code_Prestataire)}</td><td>${escapeHtml(item.Nom_Prestataire)}</td><td>${escapeHtml(familyNames.get(String(item.Code_Famille)) || '')}</td><td>${escapeHtml(regionNames.get(String(item.Code_Region)) || '')}</td></tr>`).join('');
}

async function editSelectedExpert() {
  if (selectedExpertCode == null) {
    expertListPanel.hidden = false;
    expertNotice('Choisissez un prestataire dans la liste, puis cliquez sur Modifier.', true);
    await loadExpertList();
    return;
  }
  try {
    const item = await request(`/api/prestataires/${encodeURIComponent(selectedExpertCode)}`);
    resetExpertForm();
    for (const [key, value] of Object.entries(item)) {
      const control = expertForm.elements.namedItem(key);
      if (control && !Array.isArray(value) && value != null) control.value = value;
    }
    renderExpertChecks('expert-companies', expertLookups.companies, 'Compagnies', item.Compagnies || []);
    renderExpertChecks('expert-branches', expertLookups.branches, 'Branches', item.Branches || []);
    editingExpertCode = item.Code_Prestataire;
    selectedExpertCode = item.Code_Prestataire;
    expertForm.elements.Code_Prestataire.disabled = true;
    document.querySelector('#expert-selected-label').textContent = `Modification · ${item.Code_Prestataire}`;
    document.querySelector('#expert-save').textContent = 'Enregistrer les modifications';
    expertMessage.hidden = true;
  } catch (error) { expertNotice(error.message, true); }
}

document.querySelector('#open-expert').addEventListener('click', openExpertModule);
document.querySelector('#expert-close').addEventListener('click', () => { expertModal.hidden = true; });
document.querySelector('#expert-close-toolbar').addEventListener('click', () => { expertModal.hidden = true; });
expertModal.addEventListener('click', event => { if (event.target === expertModal) expertModal.hidden = true; });
document.querySelector('#expert-add').addEventListener('click', () => {
  resetExpertForm();
  expertListPanel.hidden = true;
  expertMessage.hidden = true;
  expertForm.elements.Code_Prestataire.focus();
});
document.querySelector('#expert-edit').addEventListener('click', editSelectedExpert);
document.querySelector('#expert-list-toggle').addEventListener('click', async () => {
  expertListPanel.hidden = !expertListPanel.hidden;
  if (!expertListPanel.hidden) { try { await loadExpertList(); } catch (error) { expertNotice(error.message, true); } }
});
document.querySelector('#expert-search-btn').addEventListener('click', async () => {
  try { await loadExpertList(); } catch (error) { expertNotice(error.message, true); }
});
document.querySelector('#expert-search').addEventListener('keydown', async event => {
  if (event.key === 'Enter') { event.preventDefault(); try { await loadExpertList(); } catch (error) { expertNotice(error.message, true); } }
});
document.querySelector('#expert-rows').addEventListener('click', event => {
  const row = event.target.closest('tr[data-code]');
  if (!row) return;
  selectedExpertCode = row.dataset.code;
  document.querySelectorAll('#expert-rows tr').forEach(item => item.classList.toggle('selected', item === row));
  document.querySelector('#expert-selected-label').textContent = `Sélection · ${selectedExpertCode}`;
});
document.querySelector('#expert-delete').addEventListener('click', async () => {
  if (selectedExpertCode == null) {
    expertListPanel.hidden = false;
    expertNotice('Choisissez un prestataire dans la liste avant de le supprimer.', true);
    try { await loadExpertList(); } catch (error) { expertNotice(error.message, true); }
    return;
  }
  if (!confirm(`Supprimer le prestataire ${selectedExpertCode} et ses liens compagnies/branches ?`)) return;
  try {
    await request(`/api/prestataires/${encodeURIComponent(selectedExpertCode)}`, {method: 'DELETE'});
    expertNotice('Prestataire supprimé.');
    resetExpertForm();
    await loadExpertList();
  } catch (error) { expertNotice(error.message, true); }
});

expertForm.addEventListener('submit', async event => {
  event.preventDefault();
  const raw = Object.fromEntries(new FormData(expertForm).entries());
  const numberFields = ['Code_Prestataire', 'Code_Famille', 'Code_Region', 'Taux_RS'];
  for (const key of numberFields) raw[key] = raw[key] === '' || raw[key] == null ? null : Number(raw[key]);
  if (editingExpertCode != null) raw.Code_Prestataire = Number(editingExpertCode);
  raw.Compagnies = [...expertForm.querySelectorAll('input[name="Compagnies"]:checked')].map(input => Number(input.value));
  raw.Branches = [...expertForm.querySelectorAll('input[name="Branches"]:checked')].map(input => Number(input.value));
  if (!raw.Compagnies.length || !raw.Branches.length) {
    expertNotice('Sélectionnez au moins une compagnie et une branche.');
    return;
  }
  for (const key of ['Matricule_Fiscal','Adresse_Prof','Adresse_Residence','Tel1','Tel2','Fax','Adresse_EMail','Observation','Note']) if (raw[key] === '') raw[key] = null;
  const button = document.querySelector('#expert-save');
  button.disabled = true;
  try {
    const editing = editingExpertCode != null;
    const saved = await request(editing ? `/api/prestataires/${encodeURIComponent(editingExpertCode)}` : '/api/prestataires', {method: editing ? 'PUT' : 'POST', body: JSON.stringify(raw)});
    if (!saved.Compagnies?.length || !saved.Branches?.length) throw new Error('Le prestataire est enregistré, mais ses compagnies ou branches sont absentes. Vérifiez les sélections.');
    selectedExpertCode = raw.Code_Prestataire;
    expertNotice(editing ? 'Prestataire modifié avec ses compagnies et branches.' : 'Prestataire enregistré avec ses compagnies et branches.');
    resetExpertForm();
    expertListPanel.hidden = false;
    await loadExpertList();
  } catch (error) { expertNotice(error.message, true); }
  finally { button.disabled = false; }
});
