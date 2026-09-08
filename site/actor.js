const DATALIST_LIMIT = 1200;
const LOCAL_DATA = './data.json';

const state = {
  data: null,
  currentActor: null,
  expanded: new Set(),
};

const $ = (id) => document.getElementById(id);
const normalize = (value) => (value || '').replace(/\s+/g, '').trim();

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[char]));
}

function formatDateTime(raw) {
  if (!raw) return '未知时间';
  return raw.replace(/-/g, '.');
}

function duplicatePreferenceScore(show) {
  const sourceUrl = show.sourceUrl || '';
  const sourceType = show.sourceType || '';
  let score = 0;
  if (sourceUrl.includes('/api/show/')) score += 50;
  if (sourceType === 'csv') score += 40;
  else if (sourceType === 'api-backfill') score += 30;
  else if (sourceType === 'search-day-backfill') score += 20;
  else if (sourceType === 'manual-verified-backfill') score += 10;
  score += Math.min((show.cast || []).length, 20);
  return score;
}

function uniqueShowIdsByTimeForActor(actor) {
  const showIds = (state.data.artistIndex && state.data.artistIndex[actor]) || [];
  const byTime = new Map();
  for (const id of showIds) {
    const show = state.data.shows[id];
    if (!show) continue;
    const key = show.time || '';
    const existing = byTime.get(key);
    if (!existing || duplicatePreferenceScore(show) > duplicatePreferenceScore(state.data.shows[existing])) {
      byTime.set(key, id);
    }
  }
  return [...byTime.values()];
}

function setStatus(message, isError = false) {
  const el = $('status');
  el.textContent = message || '';
  el.classList.toggle('error', Boolean(isError));
}

function findArtistName(input) {
  const key = normalize(input);
  if (!key) return null;
  const data = state.data;
  if (!data) return null;
  if (data.artistLookup && data.artistLookup[key]) return data.artistLookup[key];
  const exactInsensitive = data.artists.find((name) => normalize(name).toLowerCase() === key.toLowerCase());
  if (exactInsensitive) return exactInsensitive;
  const contains = data.artists.filter((name) => normalize(name).includes(key));
  if (contains.length === 1) return contains[0];
  return null;
}

function buildDatalist() {
  const list = $('artistList');
  const topArtists = state.data.artists.slice(0, DATALIST_LIMIT);
  list.innerHTML = topArtists.map((name) => `<option value="${escapeHtml(name)}"></option>`).join('');
}

function computeMusicalGroups(actor) {
  const showIds = uniqueShowIdsByTimeForActor(actor);
  const groups = new Map();
  for (const id of showIds) {
    const show = state.data.shows[id];
    if (!show) continue;
    const key = show.musical || '未知剧目';
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(show);
  }
  const result = [];
  for (const [musical, shows] of groups.entries()) {
    shows.sort((a, b) => (a.time || '').localeCompare(b.time || ''));
    const dates = shows.map((s) => (s.time || '').slice(0, 10)).filter(Boolean).sort();
    const cities = new Set(shows.map((s) => s.city).filter(Boolean));
    const theatres = new Set(shows.map((s) => s.theatre).filter(Boolean));
    result.push({
      musical,
      shows,
      count: shows.length,
      dateRange: dates.length ? [dates[0], dates[dates.length - 1]] : null,
      cities: [...cities],
      theatres: [...theatres],
    });
  }
  result.sort((a, b) => b.count - a.count || a.musical.localeCompare(b.musical, 'zh-CN'));
  return result;
}

function renderSummary(actor, groups) {
  const totalShow = groups.reduce((sum, g) => sum + g.count, 0);
  const allDates = [];
  for (const g of groups) {
    if (g.dateRange) allDates.push(g.dateRange[0], g.dateRange[1]);
  }
  allDates.sort();

  $('actorName').textContent = actor;
  $('totalShowCount').textContent = totalShow;
  $('musicalCount').textContent = groups.length;
  $('dateRange').textContent = allDates.length
    ? `${allDates[0].replace(/-/g, '.')} - ${allDates[allDates.length - 1].replace(/-/g, '.')}`
    : '—';

  if (!groups.length) {
    $('actorSummary').textContent = `在当前数据范围内，还没有找到 ${actor} 的演出记录。`;
  } else {
    const top = groups[0];
    $('actorSummary').textContent = `${actor} 在当前数据中累计参演 ${totalShow} 场，涉及 ${groups.length} 部剧目，最常演的是《${top.musical}》（${top.count} 场）。`;
  }
}

function actorRoleTags(show, actor) {
  const roles = (show.cast || []).filter((c) => c.artist === actor).map((c) => c.role).filter(Boolean);
  if (!roles.length) return '';
  return roles.map((r) => `<span class="role-pill">${escapeHtml(r)}</span>`).join('');
}

function renderMusicalGroups(actor, groups) {
  const listEl = $('musicalList');
  if (!groups.length) {
    listEl.innerHTML = '<div class="empty">没有找到这位演员的场次记录。</div>';
    return;
  }
  const cards = groups.map((group, index) => {
    const isOpen = state.expanded.has(group.musical);
    const dateRange = group.dateRange
      ? `${group.dateRange[0].replace(/-/g, '.')} — ${group.dateRange[1].replace(/-/g, '.')}`
      : '—';
    const cityText = group.cities.length ? group.cities.join('、') : '未知城市';
    const showRows = group.shows.map((show) => `
      <li class="show-row">
        <div class="show-row__time">${escapeHtml(formatDateTime(show.time))}</div>
        <div class="show-row__where">
          <div class="show-row__city">${escapeHtml(show.city || '未知城市')}</div>
          <div class="show-row__theatre">${escapeHtml(show.theatre || '未知剧院')}</div>
        </div>
        <div class="show-row__roles">${actorRoleTags(show, actor) || '<span class="role-pill role-pill--muted">未标注角色</span>'}</div>
      </li>
    `).join('');
    return `
      <details class="musical-card"${isOpen ? ' open' : ''} data-musical="${escapeHtml(group.musical)}">
        <summary class="musical-card__summary">
          <span class="musical-card__rank">#${index + 1}</span>
          <span class="musical-card__title">${escapeHtml(group.musical)}</span>
          <span class="musical-card__count"><strong>${group.count}</strong><span>场</span></span>
          <span class="musical-card__meta">${escapeHtml(cityText)} · ${escapeHtml(dateRange)}</span>
          <span class="musical-card__chev" aria-hidden="true">▾</span>
        </summary>
        <ol class="show-rows">${showRows}</ol>
      </details>
    `;
  }).join('');
  listEl.innerHTML = cards;
  listEl.querySelectorAll('.musical-card').forEach((card) => {
    card.addEventListener('toggle', () => {
      const musical = card.dataset.musical;
      if (card.open) state.expanded.add(musical);
      else state.expanded.delete(musical);
    });
  });
}

function updateUrl(actor) {
  const url = new URL(window.location.href);
  if (actor) url.searchParams.set('actor', actor);
  else url.searchParams.delete('actor');
  window.history.replaceState({}, '', url.toString());
}

function runSearch(rawInput) {
  if (!state.data) return;
  const raw = (rawInput ?? $('actor').value ?? '').trim();
  if (!raw) {
    setStatus('请输入 1 位演员的姓名。', true);
    return;
  }
  const artist = findArtistName(raw);
  if (!artist) {
    setStatus(`没有在数据中找到：${raw}。建议检查姓名，或从输入框候选中选择。`, true);
    $('resultPanel').classList.add('hidden');
    return;
  }
  state.currentActor = artist;
  state.expanded = new Set();
  const groups = computeMusicalGroups(artist);
  $('actor').value = artist;
  updateUrl(artist);
  $('resultPanel').classList.remove('hidden');
  renderSummary(artist, groups);
  renderMusicalGroups(artist, groups);
  setStatus(groups.length
    ? `已生成 ${artist} 的剧目场次（共 ${groups.length} 部剧，${groups.reduce((s, g) => s + g.count, 0)} 场）。`
    : `${artist} 在当前数据里暂时没有可展示的场次。`);
}

function applyDataset(data, sourceLabel) {
  state.data = data;
  const meta = data.meta || {};
  const years = Array.isArray(meta.years) ? meta.years : [];
  $('coverageText').textContent = years.length ? `覆盖：${years.join('、')} 年` : '覆盖：—';
  $('showCountText').textContent = `${(data.shows || []).length.toLocaleString('zh-CN')} 场演出，${(data.artists || []).length.toLocaleString('zh-CN')} 位演员`;
  const updatedAt = meta.updatedAt || '刚刚';
  const sourceNote = sourceLabel ? `（${sourceLabel}）` : '';
  $('updateText').textContent = `数据更新时间：${updatedAt}${sourceNote}`;
  buildDatalist();
  $('searchBtn').disabled = false;
}

async function loadData() {
  setStatus('正在加载 GitHub Pages 数据快照…');
  try {
    const response = await fetch(LOCAL_DATA, { cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    applyDataset(data, 'GitHub Pages 每日快照');
  } catch (error) {
    console.error('[loadData] 读取本地 data.json 失败:', error);
    setStatus(`数据加载失败：${error.message}`, true);
    return;
  }
  setStatus('数据加载完成，可以开始查询。');
  const url = new URL(window.location.href);
  const preset = url.searchParams.get('actor');
  if (preset) {
    $('actor').value = preset;
    runSearch(preset);
  }
}

function setup() {
  $('searchBtn').disabled = true;
  $('searchBtn').addEventListener('click', () => runSearch());
  $('clearBtn').addEventListener('click', () => {
    $('actor').value = '';
    $('resultPanel').classList.add('hidden');
    state.currentActor = null;
    updateUrl(null);
    setStatus('已清空。');
  });
  $('actor').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') runSearch();
  });
  loadData();
}

setup();
