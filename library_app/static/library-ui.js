(() => {
  const pageSize = 10;
  const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });

  function setupCatalogSwitch() {
    const buttons = [...document.querySelectorAll('[data-view-choice]')];
    if (!buttons.length) return;

    let view = 'shelf';
    try { view = localStorage.getItem('library-catalog-view') || view; } catch (_) {}
    const applyView = (nextView) => {
      view = nextView;
      document.body.classList.toggle('catalog-grid-mode', view === 'grid');
      document.querySelectorAll('.cover-grid').forEach((grid) => { grid.hidden = view !== 'grid'; });
      buttons.forEach((button) => button.setAttribute('aria-pressed', String(button.dataset.viewChoice === view)));
      try { localStorage.setItem('library-catalog-view', view); } catch (_) {}
    };
    buttons.forEach((button) => button.addEventListener('click', () => applyView(button.dataset.viewChoice)));
    applyView(view);
  }

  function setupTable(table) {
    const body = table.tBodies[0];
    if (!body || !table.tHead) return;
    const rows = [...body.rows];
    const tools = document.createElement('div');
    tools.className = 'table-tools';
    const search = document.createElement('input');
    search.className = 'table-search';
    search.type = 'search';
    search.placeholder = 'Search this table';
    search.setAttribute('aria-label', 'Search table rows');
    const count = document.createElement('span');
    count.className = 'table-count';
    tools.append(search, count);

    const pagination = document.createElement('div');
    pagination.className = 'table-pagination';
    const previous = document.createElement('button');
    previous.type = 'button';
    previous.textContent = 'Previous';
    const pageLabel = document.createElement('span');
    pageLabel.className = 'table-count';
    const next = document.createElement('button');
    next.type = 'button';
    next.textContent = 'Next';
    pagination.append(previous, pageLabel, next);
    table.before(tools);
    table.after(pagination);

    let page = 0;
    let sortColumn = -1;
    let sortDirection = 1;
    const filteredRows = () => {
      const needle = search.value.trim().toLocaleLowerCase();
      const matching = rows.filter((row) => row.textContent.toLocaleLowerCase().includes(needle));
      if (sortColumn >= 0) {
        matching.sort((left, right) => {
          const a = left.cells[sortColumn]?.textContent.trim() || '';
          const b = right.cells[sortColumn]?.textContent.trim() || '';
          return collator.compare(a, b) * sortDirection;
        });
      }
      return matching;
    };
    const render = () => {
      const matching = filteredRows();
      const pageCount = Math.max(1, Math.ceil(matching.length / pageSize));
      page = Math.min(page, pageCount - 1);
      body.replaceChildren(...matching.slice(page * pageSize, (page + 1) * pageSize));
      count.textContent = `${matching.length} row${matching.length === 1 ? '' : 's'}`;
      pageLabel.textContent = `Page ${page + 1} of ${pageCount}`;
      previous.disabled = page === 0;
      next.disabled = page >= pageCount - 1;
    };

    table.tHead.rows[0].querySelectorAll('th').forEach((header, column) => {
      header.tabIndex = 0;
      header.setAttribute('aria-sort', 'none');
      const sort = () => {
        sortDirection = sortColumn === column ? -sortDirection : 1;
        sortColumn = column;
        table.tHead.rows[0].querySelectorAll('th').forEach((item) => item.setAttribute('aria-sort', 'none'));
        header.setAttribute('aria-sort', sortDirection === 1 ? 'ascending' : 'descending');
        page = 0;
        render();
      };
      header.addEventListener('click', sort);
      header.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); sort(); }
      });
    });
    search.addEventListener('input', () => { page = 0; render(); });
    previous.addEventListener('click', () => { page -= 1; render(); });
    next.addEventListener('click', () => { page += 1; render(); });
    render();
  }

  function setupScanFocus() {
    const scanForms = [...document.querySelectorAll('.scan-panel form, form.scan-panel')];
    const fields = [...document.querySelectorAll('.scan-panel .scan-focus')];
    if (!fields.length) return;
    let target = fields[0];
    try {
      const mode = sessionStorage.getItem('library-scan-mode');
      if (mode) {
        target = document.querySelector(`.scan-panel[action$="/${mode}"] .scan-focus`) || target;
        sessionStorage.removeItem('library-scan-mode');
      }
    } catch (_) {}
    scanForms.forEach((form) => form.addEventListener('submit', () => {
      const mode = form.action.endsWith('/return') ? 'return' : 'issue';
      try { sessionStorage.setItem('library-scan-mode', mode); } catch (_) {}
    }));
    requestAnimationFrame(() => { target.focus(); target.select(); });
  }

  document.addEventListener('DOMContentLoaded', () => {
    setupCatalogSwitch();
    document.querySelectorAll('table.js-table').forEach(setupTable);
    setupScanFocus();
  });
})();
