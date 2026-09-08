const SHOW_PAGE_SIZE = 80;
const DATALIST_LIMIT = 1200;

const state = {
  data: null,
  currentMatches: [],
  currentActors: [],
  visibleShowCount: SHOW_PAGE_SIZE
};

const $ = (id) => document.getElementById(id);
const normalize = (value) => (value || '').replace(/\s+/g, '').trim();

function formatDateTime(raw) {
  if (!raw) return '未知时间';
  return raw.replace(/-/g, '.');
}

function unique(arr) {
  return [...new Set(arr.filter(Boolean))];
}

function compareShows(a, b, mode) {
  if (mode === 'asc') return a.time.localeCompare(b.time);
  if (mode === 'musical') return a.musical.localeCompare(b.musical, 'zh-CN') || a.time.localeCompare(b.time);
  return b.time.localeCompare(a.time);
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

function uniqueShowsByTime(showList) {
  const byTime = new Map();
  for (const show of showList) {
    if (!show) continue;
    const key = show.time || '';
    const existing = byTime.get(key);
    if (!existing || duplicatePreferenceScore(show) > duplicatePreferenceScore(existing)) {
      byTime.set(key, show);
    }
  }
  return [...byTime.values()];
}

function uniqueShowIdsByTimeForActor(actor) {
  const showIds = state.data.artistIndex[actor] || [];
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

function intersectSortedArrays(arrays) {
  if (!arrays.length) return [];
  arrays.sort((a, b) => a.length - b.length);
  let result = arrays[0];
  for (let i = 1; i < arrays.length; i += 1) {
    const nextSet = new Set(arrays[i]);
    result = result.filter((id) => nextSet.has(id));
    if (!result.length) break;
  }
  return result;
}

function findArtistName(input) {
  const key = normalize(input);
  if (!key) return null;
  if (state.data.artistLookup[key]) return state.data.artistLookup[key];

  const exactInsensitive = state.data.artists.find((name) => normalize(name).toLowerCase() === key.toLowerCase());
  if (exactInsensitive) return exactInsensitive;

  const contains = state.data.artists.filter((name) => normalize(name).includes(key));
  if (contains.length === 1) return contains[0];
  return null;
}

function setStatus(message, isError = false) {
  const el = $('status');
  el.textContent = message || '';
  el.classList.toggle('error', Boolean(isError));
}

function buildDatalist() {
  const list = $('artistList');
  const topArtists = state.data.artists.slice(0, DATALIST_LIMIT);
  list.innerHTML = topArtists.map((name) => `<option value="${escapeHtml(name)}"></option>`).join('');
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[char]));
}

function searchOverlap() {
  if (!state.data) return;
  const rawActors = [$('actor1').value, $('actor2').value, $('actor3').value].map((v) => v.trim()).filter(Boolean);
  if (rawActors.length < 1) {
    setStatus('请至少输入一位演员。', true);
    return;
  }

  const actors = rawActors.map(findArtistName);
  const missing = rawActors.filter((_, index) => !actors[index]);
  if (missing.length) {
    setStatus(`没有在数据中找到：${missing.join('、')}。建议检查姓名，或从输入框候选中选择。`, true);
    return;
  }

  const duplicated = actors.filter((name, index) => actors.indexOf(name) !== index);
  if (duplicated.length) {
    setStatus('请填写不同的演员姓名。', true);
    return;
  }

  if (actors.length === 1) {
    renderRanking(actors[0]);
    return;
  }

  const arrays = actors.map((name) => uniqueShowIdsByTimeForActor(name));
  const ids = intersectSortedArrays(arrays);
  const rawMatches = ids.map((id) => state.data.shows[id]).filter(Boolean);
  const matches = uniqueShowsByTime(rawMatches);

  state.currentActors = actors;
  state.currentMatches = matches;
  state.visibleShowCount = SHOW_PAGE_SIZE;
  $('rankingPanel').classList.add('hidden');
  setStatus(matches.length ? `已找到 ${matches.length} 场同场记录。` : '没有找到同场记录。');
  renderResults();
}

function computeRanking(actor) {
  const showIds = uniqueShowIdsByTimeForActor(actor);
  const counter = new Map();
  for (const id of showIds) {
    const show = state.data.shows[id];
    if (!show) continue;
    const seenInShow = new Set();
    for (const c of show.cast) {
      const name = c && c.artist;
      if (!name || name === actor || seenInShow.has(name)) continue;
      seenInShow.add(name);
      const entry = counter.get(name) || { count: 0, musicals: new Set(), latest: '' };
      entry.count += 1;
      if (show.musical) entry.musicals.add(show.musical);
      if (show.time && show.time > entry.latest) entry.latest = show.time;
      counter.set(name, entry);
    }
  }
  return [...counter.entries()]
    .map(([name, entry]) => ({
      name,
      count: entry.count,
      musicals: [...entry.musicals],
      latest: entry.latest,
    }))
    .sort((a, b) => b.count - a.count || a.name.localeCompare(b.name, 'zh-CN'));
}

function renderRanking(actor) {
  $('resultPanel').classList.add('hidden');
  const panel = $('rankingPanel');
  panel.classList.remove('hidden');

  const showIds = uniqueShowIdsByTimeForActor(actor);
  const ranking = computeRanking(actor);
  state.currentRankingActor = actor;
  state.currentRanking = ranking;

  $('rankingArtist').textContent = actor;
  $('rankingShowCount').textContent = showIds.length;
  $('rankingPeerCount').textContent = ranking.length;
  $('rankingTopPeer').textContent = ranking.length ? `${ranking[0].name}（${ranking[0].count} 场）` : '—';
  $('rankingSummary').textContent = ranking.length
    ? `在当前数据范围内，${actor} 累计参演 ${showIds.length} 场，与 ${ranking.length} 位演员有过同场。`
    : `${actor} 在当前数据中还没有可统计的同场演员。`;

  setStatus(ranking.length ? `已生成 ${actor} 的同场演员排行（共 ${ranking.length} 位）。` : `${actor} 暂无同场演员数据。`);
  renderRankingList();
}

function renderRankingList() {
  const listEl = $('rankingList');
  const ranking = state.currentRanking || [];
  if (!ranking.length) {
    listEl.innerHTML = '<div class="empty">没有可展示的同场演员。</div>';
    return;
  }
  const limitRaw = $('rankingLimit').value;
  const limit = limitRaw === 'all' ? ranking.length : Math.min(Number(limitRaw) || 30, ranking.length);
  const rows = ranking.slice(0, limit).map((item, index) => {
    const rankLabel = `#${index + 1}`;
    const musicalsPreview = item.musicals.slice(0, 4).map(escapeHtml).join('、');
    const more = item.musicals.length > 4 ? `<span class="ranking-more">等 ${item.musicals.length} 部</span>` : '';
    return `
      <button type="button" class="ranking-row" data-artist="${escapeHtml(item.name)}">
        <span class="ranking-rank">${rankLabel}</span>
        <span class="ranking-name">${escapeHtml(item.name)}</span>
        <span class="ranking-count"><strong>${item.count}</strong><span>场同台</span></span>
        <span class="ranking-musicals">${musicalsPreview}${more}</span>
      </button>
    `;
  }).join('');
  listEl.innerHTML = rows;
  listEl.querySelectorAll('.ranking-row').forEach((row) => {
    row.addEventListener('click', () => {
      const target = row.dataset.artist;
      $('actor2').value = target;
      $('actor3').value = '';
      searchOverlap();
      $('resultPanel').scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  });
}

function renderResults() {
  const panel = $('resultPanel');
  panel.classList.remove('hidden');

  const matches = state.currentMatches;
  const actors = state.currentActors;
  const musicals = unique(matches.map((show) => show.musical));
  const cities = unique(matches.map((show) => show.city));
  const dates = matches.map((show) => show.time.slice(0, 10)).sort();

  $('overlapCount').textContent = `${matches.length} 场`;
  $('summaryText').textContent = matches.length
    ? `${actors.join('、')} 共同出演过 ${musicals.length} 部剧，覆盖 ${cities.length} 座城市。`
    : `${actors.join('、')} 暂未在当前数据范围内找到同场演出。`;
  $('musicalCount').textContent = musicals.length;
  $('cityCount').textContent = cities.length;
  $('dateRange').textContent = dates.length ? `${dates[0].replace(/-/g, '.')} - ${dates[dates.length - 1].replace(/-/g, '.')}` : '—';

  renderFilters(musicals);
  renderBreakdown(matches);
  renderShowList();
}

function renderFilters(musicals) {
  const filter = $('musicalFilter');
  const current = filter.value;
  filter.innerHTML = '<option value="">全部剧目</option>' + musicals
    .sort((a, b) => a.localeCompare(b, 'zh-CN'))
    .map((name) => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`)
    .join('');
  if (musicals.includes(current)) filter.value = current;
}

function renderBreakdown(matches) {
  const counts = new Map();
  for (const show of matches) counts.set(show.musical, (counts.get(show.musical) || 0) + 1);
  const cards = [...counts.entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0], 'zh-CN'))
    .map(([musical, count]) => `<button class="breakdown-card" type="button" data-musical="${escapeHtml(musical)}"><strong>${count}</strong><span>${escapeHtml(musical)}</span></button>`)
    .join('');
  $('musicalBreakdown').innerHTML = cards || '';
  document.querySelectorAll('.breakdown-card').forEach((card) => {
    card.addEventListener('click', () => {
      $('musicalFilter').value = card.dataset.musical;
      state.visibleShowCount = SHOW_PAGE_SIZE;
      renderShowList();
    });
  });
}

function actorRoles(show) {
  return state.currentActors.map((actor) => {
    const roles = show.cast.filter((item) => item.artist === actor).map((item) => item.role).filter(Boolean);
    return `${actor}${roles.length ? `：${roles.join(' / ')}` : ''}`;
  });
}

function renderShowList() {
  const filter = $('musicalFilter').value;
  const sortMode = $('sortMode').value;
  const list = state.currentMatches
    .filter((show) => !filter || show.musical === filter)
    .slice()
    .sort((a, b) => compareShows(a, b, sortMode));

  if (!list.length) {
    $('showList').innerHTML = '<div class="empty">当前筛选条件下没有记录。</div>';
    return;
  }

  const visibleCount = Math.min(state.visibleShowCount || SHOW_PAGE_SIZE, list.length);
  const visibleShows = list.slice(0, visibleCount);
  const cards = visibleShows.map((show) => `
    <article class="show-card">
      <div>
        <div class="show-date">${escapeHtml(formatDateTime(show.time))}</div>
        <div class="show-city">${escapeHtml(show.city || '未知城市')}</div>
      </div>
      <div>
        <div class="show-title">${escapeHtml(show.musical)}</div>
        <div class="show-theatre">${escapeHtml(show.theatre || '未知剧院')}</div>
        <div class="roles">${actorRoles(show).map((role) => `<span class="role-pill">${escapeHtml(role)}</span>`).join('')}</div>
      </div>
    </article>
  `).join('');

  const more = visibleCount < list.length
    ? `<div class="load-more-wrap"><button type="button" class="secondary" id="loadMoreBtn">再显示 ${Math.min(SHOW_PAGE_SIZE, list.length - visibleCount)} 场（已显示 ${visibleCount}/${list.length}）</button></div>`
    : `<div class="hint">已显示全部 ${list.length} 场记录。</div>`;

  $('showList').innerHTML = cards + more;
  const loadMoreBtn = $('loadMoreBtn');
  if (loadMoreBtn) {
    loadMoreBtn.addEventListener('click', () => {
      state.visibleShowCount += SHOW_PAGE_SIZE;
      renderShowList();
    });
  }
}

const LOCAL_DATA = './data.json';

function applyDataset(data, sourceLabel) {
  state.data = data;
  $('coverageText').textContent = `覆盖：${data.meta.years.join('、')} 年`;
  $('showCountText').textContent = `${data.shows.length.toLocaleString('zh-CN')} 场演出，${data.artists.length.toLocaleString('zh-CN')} 位演员`;
  const updatedAt = data.meta.updatedAt || '刚刚';
  $('updateText').textContent = `数据更新时间：${updatedAt}（${sourceLabel}）`;
  buildDatalist();
  $('searchBtn').disabled = false;
}

async function loadData() {
  try {
    setStatus('正在加载 GitHub Pages 数据快照…');
    const response = await fetch(LOCAL_DATA, { cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    applyDataset(data, 'GitHub Pages 每日快照');
    setStatus('数据已加载。');
  } catch (error) {
    console.error('[loadData] 读取本地 data.json 失败：', error);
    setStatus(`数据加载失败：${error.message}`, true);
  }
}

function setup() {
  $('searchBtn').disabled = true;
  $('searchBtn').addEventListener('click', searchOverlap);
  $('clearBtn').addEventListener('click', () => {
    ['actor1', 'actor2', 'actor3'].forEach((id) => { $(id).value = ''; });
    $('resultPanel').classList.add('hidden');
    $('rankingPanel').classList.add('hidden');
    setStatus('已清空。');
  });
  ['actor1', 'actor2', 'actor3'].forEach((id) => {
    $(id).addEventListener('keydown', (event) => {
      if (event.key === 'Enter') searchOverlap();
    });
  });
  $('musicalFilter').addEventListener('change', () => {
    state.visibleShowCount = SHOW_PAGE_SIZE;
    renderShowList();
  });
  $('sortMode').addEventListener('change', () => {
    state.visibleShowCount = SHOW_PAGE_SIZE;
    renderShowList();
  });
  $('rankingLimit').addEventListener('change', renderRankingList);
  loadData();
}

setup();
