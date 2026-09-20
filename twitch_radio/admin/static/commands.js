
(function () {
  var tabs = document.querySelectorAll('.tab');
  var cardsEl = document.getElementById('cards');
  var searchEl = document.getElementById('search');
  var titleEl = document.getElementById('panel-title');
  var subEl = document.getElementById('panel-sub');
  var DATA = window.__COMMANDS__ || [];
  var CATS = window.__CATEGORIES__ || [];
  var active = CATS[0] || '';

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function cardHtml(c) {
    var alias = c.aliases && c.aliases.length
      ? '<span class="cmd-alias">also ' + c.aliases.map(function (a) { return escapeHtml(a); }).join(', ') + '</span>'
      : '';
    var badge = c.group === 'moderators' ? '<span class="cmd-badge">Mods</span>' : '';
    return (
      '<div class="cmd-card' + (c.group === 'moderators' ? ' mod' : '') + '">' +
        '<div class="cmd-head"><span class="cmd-name">' + escapeHtml(c.usage) + '</span>' + alias + badge + '</div>' +
        '<p class="cmd-desc">' + escapeHtml(c.description) + '</p>' +
        '<span class="cmd-who">' + escapeHtml(c.who) + '</span>' +
      '</div>'
    );
  }

  function renderCards(list) {
    cardsEl.classList.remove('fade-enter-active');
    cardsEl.classList.add('fade-enter');
    cardsEl.innerHTML = list.length
      ? list.map(cardHtml).join('')
      : '<p class="empty-state">No commands match that search.</p>';
    // Same enter-transition trick used on the settings/overlay pages:
    // apply the pre-transition state, then flip to active next frame so
    // there's something for the CSS transition to animate from.
    requestAnimationFrame(function () {
      cardsEl.classList.remove('fade-enter');
      cardsEl.classList.add('fade-enter-active');
    });
  }

  function showTab(cat) {
    active = cat;
    tabs.forEach(function (t) { t.classList.toggle('active', t.dataset.cat === cat); });
    titleEl.textContent = cat;
    subEl.textContent = 'Everything under ' + cat.toLowerCase() + '.';
    renderCards(DATA.filter(function (c) { return c.category === cat; }));
  }

  tabs.forEach(function (t) {
    t.addEventListener('click', function () {
      searchEl.value = '';
      showTab(t.dataset.cat);
    });
  });

  var searchTimer = null;
  searchEl.addEventListener('input', function () {
    clearTimeout(searchTimer);
    var q = searchEl.value.trim().toLowerCase();
    // A short debounce, not because filtering a few dozen commands is
    // slow, but so the fade transition below doesn't restart on every
    // single keystroke while someone's still typing.
    searchTimer = setTimeout(function () {
      if (!q) { showTab(active); return; }
      tabs.forEach(function (t) { t.classList.remove('active'); });
      var matches = DATA.filter(function (c) {
        return c.name.indexOf(q) !== -1 ||
               c.description.toLowerCase().indexOf(q) !== -1 ||
               (c.aliases || []).some(function (a) { return a.indexOf(q) !== -1; });
      });
      titleEl.textContent = 'Search: "' + searchEl.value.trim() + '"';
      subEl.textContent = matches.length + ' match' + (matches.length === 1 ? '' : 'es') + '.';
      renderCards(matches);
    }, 120);
  });

  showTab(active);
})();
