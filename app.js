const countEl = document.getElementById('count');
const updatedEl = document.getElementById('updated');
const listEl = document.getElementById('list');
const emptyEl = document.getElementById('empty');
const reviewListEl = document.getElementById('reviewList');
const reviewEmptyEl = document.getElementById('reviewEmpty');
const branchFilterEl = document.getElementById('branchFilter');

let allEntries = [];

function fmtDate(value) {
  if (!value) return '—';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit'
  });
}

function entryMarkup(item, compact = false) {
  return `
    <article class="entry">
      <div class="topline">
        <div>
          <h3>${item.name || 'Unparsed official release'}</h3>
          <div class="muted small">${item.release_title || ''}</div>
        </div>
        ${item.branch ? `<span class="badge">${item.branch}</span>` : ''}
      </div>
      <div class="grid">
        <div class="field"><span class="k">Age</span>${item.age || 'N/A'}</div>
        <div class="field"><span class="k">Hometown</span>${item.hometown || 'N/A'}</div>
        <div class="field"><span class="k">Reported location</span>${item.reported_location || 'N/A'}</div>
        <div class="field"><span class="k">Release date</span>${item.release_date || 'N/A'}</div>
      </div>
      ${item.notes ? `<p class="small muted">${item.notes}</p>` : ''}
      <div class="links"><a href="${item.source_url}" target="_blank" rel="noopener noreferrer">Official source</a></div>
    </article>
  `;
}

function renderFilterOptions(entries) {
  const branches = [...new Set(entries.map((x) => x.branch).filter(Boolean))].sort();
  branchFilterEl.innerHTML = '<option value="all">All branches</option>';
  branches.forEach((branch) => {
    const option = document.createElement('option');
    option.value = branch;
    option.textContent = branch;
    branchFilterEl.appendChild(option);
  });
}

function renderConfirmed() {
  const branch = branchFilterEl.value;
  const filtered = branch === 'all' ? allEntries : allEntries.filter((x) => x.branch === branch);
  listEl.innerHTML = filtered.map((item) => entryMarkup(item)).join('');
  emptyEl.classList.toggle('hidden', filtered.length !== 0);
}

async function load() {
  const [confirmedRes, metaRes, reviewRes] = await Promise.all([
    fetch('data/fallen.json', { cache: 'no-store' }),
    fetch('data/meta.json', { cache: 'no-store' }),
    fetch('data/pending_review.json', { cache: 'no-store' })
  ]);

  const confirmed = await confirmedRes.json();
  const meta = await metaRes.json();
  const review = await reviewRes.json();

  allEntries = [...confirmed].sort((a, b) => (b.release_date || '').localeCompare(a.release_date || ''));
  countEl.textContent = String(allEntries.length);
  updatedEl.textContent = fmtDate(meta.last_updated_utc || meta.generated_at || '');

  renderFilterOptions(allEntries);
  renderConfirmed();

  reviewListEl.innerHTML = review.map((item) => entryMarkup(item, true)).join('');
  reviewEmptyEl.classList.toggle('hidden', review.length !== 0);
}

branchFilterEl.addEventListener('change', renderConfirmed);
load().catch((error) => {
  console.error(error);
  updatedEl.textContent = 'Failed to load';
  emptyEl.textContent = 'Unable to load data files.';
  emptyEl.classList.remove('hidden');
});
