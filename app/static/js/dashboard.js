/*
 * dashboard.js — the patient dashboard page (#976). Plain script, no build.
 *
 * ONE FAILING SERVICE MUST NOT BLANK THE PAGE. Each panel loads through its
 * own call and shows its own loading and error state; the card itself also
 * reports, per source, what could not be read (`unavailable`).
 *
 * DETAIL IS A DISCLOSURE, NOT A HOVER POPOVER. The EPS summary and a
 * diagnosis's detail open on an explicit action (click, tap, Enter/Space) and
 * STAY until closed, or until another chip is chosen. Esc closes. Where the
 * device has real hover (DashboardCapability: hover AND fine pointer), hover
 * additionally opens the EPS summary as an accelerator — never the only way in.
 *
 * All patient text is written with textContent. Nothing from the server is
 * parsed as HTML.
 */
(function () {
  'use strict';

  var root = document.getElementById('dashboard');
  if (!root) { return; }
  var guid = root.getAttribute('data-patient-guid');
  var cap = window.DashboardCapability || { hasHover: false };

  // euIPS sections that come first and are highlighted: alerts, allergies.
  var PRIORITY_SECTIONS = ['104605-1', '48765-2'];

  var DASH = '—';

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) { n.className = cls; }
    if (text !== undefined && text !== null) { n.textContent = String(text); }
    return n;
  }

  function panel(id) {
    var p = document.getElementById(id);
    return {
      node: p,
      status: p.querySelector('[data-role="status"]'),
      body: p.querySelector('[data-role="body"]')
    };
  }

  /* fetch JSON, and turn every failure into a sentence for THIS panel. A
   * refusal names itself ({error, message}); 503 `undetermined` means nobody
   * could check, which is not the same as "you may not". */
  function getJson(url) {
    return fetch(url, {
      credentials: 'same-origin',
      headers: { 'Accept': 'application/json' }
    }).then(function (res) {
      var type = res.headers.get('content-type') || '';
      if (type.indexOf('application/json') === -1) {
        if (res.redirected || res.status === 401) {
          throw new Error('Sessionen har gått ut. Ladda om sidan för att logga in igen.');
        }
        throw new Error('Oväntat svar från servern (' + res.status + ').');
      }
      return res.json().then(function (body) {
        if (!res.ok) {
          var msg = (body && (body.message || body.error)) || ('fel ' + res.status);
          var e = new Error(msg);
          e.status = res.status;
          e.code = body && body.error;
          throw e;
        }
        return body;
      });
    });
  }

  function showError(p, err) {
    p.node.setAttribute('aria-busy', 'false');
    p.status.hidden = false;
    p.status.classList.add('is-error');
    var prefix = err && err.code === 'undetermined'
      ? 'Åtkomsten kunde inte kontrolleras just nu: '
      : 'Kunde inte hämtas: ';
    p.status.textContent = prefix + ((err && err.message) || 'okänt fel');
  }

  /* ---- card -------------------------------------------------------------- */

  function ageFrom(birthDate) {
    if (!birthDate) { return null; }
    var b = new Date(birthDate);
    if (isNaN(b.getTime())) { return null; }
    var now = new Date();
    var age = now.getFullYear() - b.getFullYear();
    var m = now.getMonth() - b.getMonth();
    if (m < 0 || (m === 0 && now.getDate() < b.getDate())) { age -= 1; }
    return age >= 0 ? age : null;
  }

  function setField(scope, name, value) {
    var n = scope.querySelector('[data-field="' + name + '"]');
    if (n) { n.textContent = (value === null || value === undefined || value === '') ? DASH : String(value); }
  }

  function banner(container, text, isBlock) {
    container.appendChild(el('div', 'dash-banner' + (isBlock ? ' is-block' : ''), text));
  }

  var UNAVAILABLE_TEXT = {
    icd11: 'ICD-11-registret kunde inte läsas — diagnoserna nedan är okända, inte tomma.',
    eps: 'Patientsammanfattningen (EPS) kunde inte läsas.'
  };

  function renderBanners(container, data) {
    container.textContent = '';
    if (data.sparrChecked === false) {
      banner(container, 'Spärr kunde inte kontrolleras. Uppgifter kan saknas eller vara spärrade.', true);
    } else if (data.hasBlockedSources) {
      banner(container, 'Det finns spärrade uppgifter för patienten. De visas inte här.', true);
    }
    (data.unavailable || []).forEach(function (u) {
      var key = String(u).split(':')[0];
      if (u === 'eps:patient-not-in-ips-scope') {
        banner(container, 'Patienten finns inte i patientsammanfattningen (ips.pdhc) inom din behörighet.');
      } else {
        banner(container, UNAVAILABLE_TEXT[key] || ('Källa saknas: ' + u));
      }
    });
  }

  /* -- diagnosis chips + their disclosure -- */

  var dxDetail, openChip = null;

  function closeDx(returnFocus) {
    if (!openChip) { return; }
    var chip = openChip;
    chip.setAttribute('aria-expanded', 'false');
    dxDetail.hidden = true;
    dxDetail.textContent = '';
    openChip = null;
    if (returnFocus) { chip.focus(); }
  }

  function openDx(chip, d) {
    if (openChip === chip) { closeDx(false); return; }
    if (openChip) { closeDx(false); }
    openChip = chip;
    chip.setAttribute('aria-expanded', 'true');
    dxDetail.textContent = '';
    var head = el('div', 'dash-disclosure-head');
    head.appendChild(el('span', null, d.code + ' ' + (d.display || '')));
    var close = el('button', 'dash-close', 'Stäng');
    close.type = 'button';
    close.setAttribute('aria-label', 'Stäng diagnosdetaljen');
    close.addEventListener('click', function () { closeDx(true); });
    head.appendChild(close);
    dxDetail.appendChild(head);
    var dl = el('dl');
    [
      ['Kod', d.code],
      ['Postkoordinering', d.postcoordination],
      ['Klinisk status', d.clinicalStatus],
      ['Verifiering', d.verificationStatus],
      ['Debut', d.onset],
      ['Avslutad', d.abatement],
      ['Registrerad', d.recordedDate],
      ['Källa', 'ICD-11-register (icd11.pdhc)']
    ].forEach(function (pair) {
      if (pair[1] === null || pair[1] === undefined || pair[1] === '') { return; }
      dl.appendChild(el('dt', null, pair[0]));
      dl.appendChild(el('dd', null, pair[1]));
    });
    dxDetail.appendChild(dl);
    dxDetail.hidden = false;
  }

  function renderDiagnoses(scope, data) {
    var chips = scope.querySelector('[data-role="chips"]');
    var note = scope.querySelector('[data-role="dx-note"]');
    dxDetail = scope.querySelector('[data-role="dx-detail"]');
    chips.textContent = '';
    var card = data.patientCard || {};
    var list = card.diagnoses || [];

    // Active first, then the rest GREYED — never hidden.
    var sorted = list.slice().sort(function (a, b) {
      return (a.clinicalStatus === 'active' ? 0 : 1) - (b.clinicalStatus === 'active' ? 0 : 1);
    });
    sorted.forEach(function (d, i) {
      var chip = el('button', 'dash-chip' + (d.clinicalStatus === 'active' ? '' : ' is-inactive'));
      chip.type = 'button';
      chip.setAttribute('role', 'listitem');
      chip.setAttribute('aria-expanded', 'false');
      chip.setAttribute('aria-controls', 'dx-detail');
      chip.appendChild(el('code', null, d.code));
      chip.appendChild(el('span', null, d.display || DASH));
      if (d.clinicalStatus !== 'active') {
        chip.appendChild(el('span', 'visually-hidden', ' (' + (d.clinicalStatus || 'okänd status') + ')'));
        chip.title = d.clinicalStatus || '';
      }
      chip.addEventListener('click', function () { openDx(chip, d); });
      chips.appendChild(chip);
    });

    // THREE states, never collapsed: unknown / none / none you may see.
    var text = null;
    if (data.diagnosesDefinitivelyEmpty === null || data.diagnosesDefinitivelyEmpty === undefined) {
      text = 'Diagnoserna kunde inte hämtas — det betyder inte att patienten saknar diagnoser.';
    } else if (!list.length && data.diagnosesDefinitivelyEmpty) {
      text = 'Inga diagnoser i ICD-11-registret.';
    } else if (!list.length) {
      text = card.diagnosisScopeNote || 'Inga diagnoser som du har behörighet att se.';
    } else if (card.diagnosisScopeNote) {
      text = card.diagnosisScopeNote;
    }
    // Two registers, never merged (#984): say where the other one is.
    var ips = (data.diagnosisSources || []).filter(function (s) { return s.source === 'ips'; })[0];
    if (ips && ips.available && ips.count) {
      text = (text ? text + ' ' : '') + 'Patientsammanfattningen har ' + ips.count +
        ' problem kodade i SNOMED CT — ett separat register, inte avstämt mot ICD-11.';
    }
    note.hidden = !text;
    note.textContent = text || '';
  }

  /* -- EPS disclosure -- */

  var epsToggle, epsDetail;

  function setEps(open, returnFocus) {
    epsToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    epsDetail.hidden = !open;
    if (!open && returnFocus) { epsToggle.focus(); }
  }

  function renderEps(scope, data) {
    var host = scope.querySelector('[data-role="eps-sections"]');
    host.textContent = '';
    var sections = ((data.patientCard || {}).epsSections || []).slice();
    if (!sections.length) {
      host.appendChild(el('p', 'dash-status', 'Ingen patientsammanfattning tillgänglig.'));
      return;
    }
    // Priority sections first, in the order above; the rest in EPS order.
    sections.sort(function (a, b) {
      var ia = PRIORITY_SECTIONS.indexOf(a.sectionCode), ib = PRIORITY_SECTIONS.indexOf(b.sectionCode);
      ia = ia === -1 ? 99 : ia; ib = ib === -1 ? 99 : ib;
      return ia - ib;
    });
    sections.forEach(function (s) {
      var isPriority = PRIORITY_SECTIONS.indexOf(s.sectionCode) !== -1;
      var sec = el('section', 'eps-section' + (isPriority ? ' is-priority' : ''));
      sec.appendChild(el('h4', null, s.title || s.sectionCode));
      var entries = s.entries || [];
      if (entries.length) {
        var ul = el('ul');
        entries.forEach(function (e) {
          var li = el('li', null, e.display || DASH);
          var meta = [e.source, e.date, e.status].filter(Boolean).join(' · ');
          if (meta) { li.appendChild(el('span', 'eps-meta', meta)); }
          ul.appendChild(li);
        });
        sec.appendChild(ul);
      } else if (s.absentAssertion) {
        // A clinical statement ("no known allergies"), not a gap.
        sec.appendChild(el('p', null, s.narrative || 'Uttryckligen inga uppgifter.'));
      } else {
        sec.appendChild(el('p', 'dash-status', 'Ingen uppgift registrerad.'));
      }
      host.appendChild(sec);
    });
  }

  function wireEps(scope) {
    epsToggle = document.getElementById('eps-toggle');
    epsDetail = document.getElementById('eps-detail');
    epsToggle.addEventListener('click', function () {
      setEps(epsToggle.getAttribute('aria-expanded') !== 'true', false);
    });
    scope.querySelector('[data-role="eps-close"]').addEventListener('click', function () {
      setEps(false, true);
    });
    // Hover accelerator: OPENS only, and only with real hover. It never
    // closes on mouseleave — the detail stays until it is closed.
    epsToggle.addEventListener('mouseenter', function () {
      if (cap.hasHover) { setEps(true, false); }
    });
  }

  document.addEventListener('keydown', function (ev) {
    if (ev.key !== 'Escape') { return; }
    if (openChip) { closeDx(true); return; }
    if (epsToggle && epsToggle.getAttribute('aria-expanded') === 'true') { setEps(false, true); }
  });

  function loadCard() {
    var p = panel('panel-card');
    wireEps(p.body);
    getJson('/dashboard/patients/' + encodeURIComponent(guid)).then(function (data) {
      var card = data.patientCard || {};
      setField(p.body, 'name', card.name);
      var age = ageFrom(card.birthDate);
      setField(p.body, 'age', age === null ? null : age + ' år');
      setField(p.body, 'sex', card.sex);
      setField(p.body, 'personnummer', card.personnummer);
      setField(p.body, 'organisation', card.organisation);
      renderBanners(p.body.querySelector('[data-role="banners"]'), data);
      renderDiagnoses(p.body, data);
      renderEps(p.body, data);
      p.status.hidden = true;
      p.body.hidden = false;
      p.node.setAttribute('aria-busy', 'false');
    }).catch(function (err) { showError(p, err); });
  }

  /* ---- plan panel (#971) -------------------------------------------------
   * Plans come from request.pdhc ORDERS (production has no CarePlans). An
   * item's state is the ORDER's status, never the presence of observations.
   * Disagreement is SHOWN: a done item with no observation is flagged, and
   * observations whose transaction no order covers get their own group.
   * Items are not clickable yet: the link to the event list arrives with the
   * list itself (#977), and an affordance that does nothing is worse than none.
   */

  var STOCKHOLM = (function () {
    try {
      return new Intl.DateTimeFormat('sv-SE', { timeZone: 'Europe/Stockholm', dateStyle: 'short' });
    } catch (e) { return null; }
  })();

  function day(iso) {
    if (!iso) { return null; }
    var d = new Date(iso);
    if (isNaN(d.getTime())) { return null; }
    return STOCKHOLM ? STOCKHOLM.format(d) : iso.slice(0, 10);
  }

  // Done, then NOW, then what is still ahead. Cancelled last.
  var GROUPS = [
    { state: 'done', label: 'Klara' },
    { now: true },
    { state: 'overdue', label: 'Försenade' },
    { state: 'planned', label: 'Planerade' },
    { state: 'cancelled', label: 'Avbrutna' }
  ];

  var SOURCE_LABEL = { service_request: 'Beställning', care_plan: 'Vårdplan' };

  // request.pdhc's ServiceRequest statuses (draft -> active -> archived, or
  // revoked). `archived` IS completion there. Unknown values pass through.
  var STATUS_LABEL = { draft: 'utkast', active: 'aktiv', archived: 'klar',
                       completed: 'klar', revoked: 'avbruten' };
  function statusText(s) { return STATUS_LABEL[s] || s; }

  function countText(data, tx) {
    if (!data.observationsKnown) { return 'mätningar okända'; }
    var agg = (data.observationsByTransaction || {})[tx];
    var n = agg ? agg.count : 0;
    if (!data.observationCountsComplete) {
      // A bounded scan: a count is a lower bound, and zero is NOT "none".
      return n ? '≥' + n + (n === 1 ? ' mätning' : ' mätningar') : 'inga bland genomsökta';
    }
    return n + (n === 1 ? ' mätning' : ' mätningar');
  }

  function renderPlan(host, plan, data) {
    host.textContent = '';
    var head = el('div', 'plan-head');
    var name = plan.planDefinitionName || plan.carePlanTitle || 'Namnlös plan';
    head.appendChild(el('strong', null, name + (plan.planDefinitionVersion ? ' v' + plan.planDefinitionVersion : '')));
    var meta = [SOURCE_LABEL[plan.source] || plan.source];
    if (plan.status) { meta.push(statusText(plan.status)); }
    if (plan.orderedAt) { meta.push('beställd ' + day(plan.orderedAt)); }
    meta.push(plan.periodStart ? 'start ' + day(plan.periodStart)
                               : 'inget startdatum — ingen T+');
    head.appendChild(el('div', 'plan-meta', meta.join(' · ')));
    host.appendChild(head);

    var items = plan.items || [];
    if (!items.length) {
      host.appendChild(el('p', 'dash-status', 'Planen har inga transaktioner.'));
      return;
    }
    GROUPS.forEach(function (g) {
      if (g.now) {
        host.appendChild(el('div', 'plan-now', 'nu ' + (day(data.now) || '')));
        return;
      }
      var mine = items.filter(function (i) { return i.state === g.state; });
      if (!mine.length) { return; }
      var group = el('section', 'plan-group is-' + g.state);
      group.appendChild(el('h3', null, g.label + ' (' + mine.length + ')'));
      var ul = el('ul');
      mine.forEach(function (i) {
        var li = el('li', 'plan-item');
        var title = el('span', 'plan-item-title', i.title || DASH);
        var agg = (data.observationsByTransaction || {})[i.transactionGuid];
        if (i.state === 'done' && data.observationsKnown && !agg) {
          title.appendChild(el('span', 'plan-flag', data.observationCountsComplete
            ? 'Klar enligt beställningen, men inga mätvärden finns.'
            : 'Klar enligt beställningen, men inga mätvärden bland de genomsökta.'));
        }
        if (i.performedAt) {
          title.appendChild(el('span', 'plan-meta', ' utförd ' + day(i.performedAt)));
        }
        li.appendChild(title);
        li.appendChild(el('span', 'plan-count', countText(data, i.transactionGuid)));
        makePlanLink(li, i.transactionGuid, i.title, agg ? agg.count : (data.observationsKnown ? 0 : null),
                     data.observationCountsComplete);
        ul.appendChild(li);
      });
      group.appendChild(ul);
      host.appendChild(group);
    });
  }

  function renderUnordered(host, data) {
    host.textContent = '';
    var rows = data.unordered || [];
    if (!rows.length) { return; }
    var box = el('section', 'plan-unordered');
    box.appendChild(el('h3', null, 'Mätningar utan beställning (' + rows.length + ')'));
    box.appendChild(el('p', null,
      'Mätvärdena följer en planerad transaktion, men ingen beställning för patienten täcker den. ' +
      'Visas här i stället för att döljas.'));
    var ul = el('ul');
    rows.forEach(function (u) {
      var li = el('li', 'plan-item');
      var title = el('span', 'plan-item-title', u.display || u.transactionGuid);
      var span = [day(u.first), day(u.last)].filter(Boolean);
      if (span.length) { title.appendChild(el('span', 'plan-meta', ' ' + span.join(' – '))); }
      li.appendChild(title);
      li.appendChild(el('span', 'plan-count',
        (data.observationCountsComplete ? '' : '≥') + u.count + (u.count === 1 ? ' mätning' : ' mätningar')));
      makePlanLink(li, u.transactionGuid, u.display, u.count, data.observationCountsComplete);
      ul.appendChild(li);
    });
    box.appendChild(ul);
    host.appendChild(box);
  }

  function loadPlan() {
    var p = panel('panel-plan');
    var picker = p.body.querySelector('[data-role="plan-picker"]');
    var current = p.body.querySelector('[data-role="plan-current"]');
    getJson('/dashboard/patients/' + encodeURIComponent(guid) + '/plan').then(function (data) {
      var banners = p.body.querySelector('[data-role="plan-banners"]');
      banners.textContent = '';
      if (!data.plansKnown) {
        banner(banners, 'Beställningarna kunde inte läsas — planerna är okända, inte tomma.');
      }
      if (!data.observationsKnown) {
        banner(banners, 'Mätvärdena kunde inte läsas — antal mätningar är okänt.');
      } else if (!data.observationCountsComplete) {
        banner(banners, 'Lång historik: antalen är minst-värden.');
      }
      if (data.sparrChecked === false) {
        banner(banners, 'Spärr kunde inte kontrolleras.', true);
      } else if (data.hasBlockedSources) {
        banner(banners, 'Spärrade mätvärden räknas inte här.', true);
      }

      var plans = data.plans || [];
      picker.textContent = '';
      p.body.querySelector('[data-role="plan-select"]').hidden = plans.length < 2;
      plans.forEach(function (pl, i) {
        var label = (pl.planDefinitionName || 'Plan') +
          (pl.orderedAt ? ' — ' + day(pl.orderedAt) : '') +
          (pl.status ? ' (' + statusText(pl.status) + ')' : '');
        var opt = el('option', null, label);
        opt.value = String(i);
        picker.appendChild(opt);
      });
      picker.onchange = function () { renderPlan(current, plans[Number(picker.value)], data); };

      if (plans.length) {
        renderPlan(current, plans[0], data);
      } else if (data.plansKnown) {
        current.textContent = '';
        current.appendChild(el('p', 'dash-status', 'Inga beställningar för patienten i request.pdhc.'));
      }
      renderUnordered(p.body.querySelector('[data-role="plan-unordered"]'), data);
      p.status.hidden = true;
      p.body.hidden = false;
      p.node.setAttribute('aria-busy', 'false');
    }).catch(function (err) { showError(p, err); });
  }

  /* ---- plan item -> event list link ---------------------------------------
   * Click (or Enter/Space) marks the item's observations in the event list
   * and scrolls to the first; again clears. With real hover, hovering marks
   * without scrolling. An item with no observations is not made a control:
   * a button that can do nothing is worse than none.
   */
  var activePlanItem = null;

  function makePlanLink(li, tx, title, count, complete) {
    if (!tx || count === 0) { return; }
    li.classList.add('is-link');
    li.tabIndex = 0;
    li.setAttribute('role', 'button');
    li.setAttribute('aria-pressed', 'false');
    function activate() {
      var on = activePlanItem !== li;
      if (activePlanItem) {
        activePlanItem.classList.remove('is-active');
        activePlanItem.setAttribute('aria-pressed', 'false');
      }
      activePlanItem = on ? li : null;
      if (on) { li.classList.add('is-active'); li.setAttribute('aria-pressed', 'true'); }
      events.link(on ? { tx: tx, title: title, count: count, complete: complete } : null, true);
    }
    li.addEventListener('click', activate);
    li.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); activate(); }
    });
    li.addEventListener('mouseenter', function () {
      if (cap.hasHover && !activePlanItem) { events.link({ tx: tx, title: title, count: count, complete: complete }, false); }
    });
    li.addEventListener('mouseleave', function () {
      if (cap.hasHover && !activePlanItem) { events.link(null, false); }
    });
  }

  /* ---- event list (#977) ---------------------------------------------------
   * SELECTION IS THE MECHANISM. Click, tap or Enter selects; arrow keys move
   * the selection along the same path. The detail stays until another row is
   * chosen or Esc. With real hover, resting 300 ms on a row PREVIEWS it in
   * the same region (dashed border) and leaving the list restores the
   * selection. There is no pin: a persistent selection already is one.
   *
   * WINDOWING is hand-rolled: fixed row height, only visible rows (+ overscan)
   * are in the DOM. The height comes from DashboardCapability and changes at
   * runtime when a trackpad is attached to an iPad.
   */
  var events = (function () {
    var p, list, spacer, detail, countEl, linkEl;
    var rows = [], nextCursor = null, loading = false, done = false, failed = null;
    var selected = -1, linked = null, hoverTimer = null, previewing = false;
    var controller = null, cache = Object.create(null);
    var OVERSCAN = 8, PAGE = 200, PREFETCH_ROWS = 40;

    function rowH() { return cap.rowHeight ? cap.rowHeight() : 44; }

    var STOCKHOLM_TIME = (function () {
      try {
        return new Intl.DateTimeFormat('sv-SE', { timeZone: 'Europe/Stockholm',
          dateStyle: 'short', timeStyle: 'short' });
      } catch (e) { return null; }
    })();

    function when(iso) {
      if (!iso) { return null; }
      var d = new Date(iso);
      if (isNaN(d.getTime())) { return iso; }
      return STOCKHOLM_TIME ? STOCKHOLM_TIME.format(d) : iso;
    }

    function tPlus(seconds) {
      if (seconds === null || seconds === undefined) { return null; }
      var neg = seconds < 0, s = Math.abs(Math.round(seconds));
      var d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
      var out = d ? d + 'd ' + h + 'h' : (h ? h + 'h ' + m + 'm' : m + 'm');
      return (neg ? 'T−' : 'T+') + out;
    }

    function valueText(r) {
      if (r.value === null || r.value === undefined || r.value === '') { return null; }
      return String(r.value) + (r.unit ? ' ' + r.unit : '');
    }

    function render() {
      var h = rowH();
      list.style.setProperty('--ev-row-h', h + 'px');
      spacer.style.height = (rows.length * h) + 'px';
      var top = list.scrollTop, view = list.clientHeight || 352;
      var first = Math.max(0, Math.floor(top / h) - OVERSCAN);
      var last = Math.min(rows.length - 1, Math.ceil((top + view) / h) + OVERSCAN);
      spacer.textContent = '';
      for (var i = first; i <= last; i++) {
        spacer.appendChild(rowNode(i, h));
      }
      if (!done && !loading && !failed && last >= rows.length - PREFETCH_ROWS) { loadMore(); }
    }

    function rowNode(i, h) {
      var r = rows[i];
      var n = el('div', 'ev-row');
      n.id = 'ev-row-' + i;
      n.setAttribute('role', 'option');
      n.setAttribute('aria-selected', i === selected ? 'true' : 'false');
      n.setAttribute('aria-setsize', String(done ? rows.length : -1));
      n.setAttribute('aria-posinset', String(i + 1));
      n.style.top = (i * h) + 'px';
      if (linked && r.transactionGuid === linked.tx) { n.classList.add('is-linked'); }
      n.appendChild(el('span', null, r.displayName || r.codeCanonical || DASH));
      n.appendChild(el('span', null, valueText(r) || DASH));
      n.appendChild(el('span', 'ev-muted', when(r.effectiveAt) || 'odaterad'));
      n.appendChild(el('span', 'ev-muted', tPlus(r.tFromStartSeconds) || DASH));
      var badge = el('span');
      if (r.hasSeries) {
        badge = el('span', 'ev-badge', '×' + r.seriesCount);
        badge.title = r.seriesCount + ' mätningar av samma begrepp';
      }
      n.appendChild(badge);
      n.addEventListener('click', function () { select(i, true); list.focus({ preventScroll: true }); });
      n.addEventListener('mouseenter', function () {
        if (!cap.hasHover) { return; }
        clearTimeout(hoverTimer);
        hoverTimer = setTimeout(function () { preview(i); }, 300);
      });
      return n;
    }

    function updateCount() {
      var t = rows.length + (done ? ' händelser' : '+ händelser laddade');
      countEl.textContent = t;
    }

    function loadMore() {
      if (loading || done) { return Promise.resolve(); }
      loading = true;
      var url = '/dashboard/patients/' + encodeURIComponent(guid) + '/events?limit=' + PAGE +
        (nextCursor ? '&cursor=' + encodeURIComponent(nextCursor) : '');
      return getJson(url).then(function (data) {
        rows = rows.concat(data.rows || []);
        nextCursor = data.nextCursor || null;
        done = !nextCursor;
        renderEventBanners(data);
        loading = false;
        updateCount();
        if (linked) { describeLink(); }
        render();
      }).catch(function (err) {
        loading = false;
        failed = err;
        if (!rows.length) { showError(p, err); return; }
        countEl.textContent = rows.length + ' händelser laddade — resten kunde inte hämtas: ' + err.message;
      });
    }

    function renderEventBanners(data) {
      var host = p.body.querySelector('[data-role="ev-banners"]');
      host.textContent = '';
      if (data.sparrChecked === false) {
        banner(host, 'Spärr kunde inte kontrolleras.', true);
      } else if (data.hasBlockedSources) {
        banner(host, 'Spärrade mätvärden visas inte.', true);
      }
      if (data.undatedCount) {
        banner(host, data.undatedCount + ' mätvärden saknar tidpunkt.');
      }
      (data.unavailable || []).forEach(function (u) {
        banner(host, u.indexOf('plan:') === 0 ? 'Namn kunde inte hämtas — koder visas i stället.'
               : u === 'cdr1:summary' ? 'Antal per serie är okänt.' : 'Källa saknas: ' + u);
      });
    }

    function scrollToRow(i) {
      var h = rowH(), top = i * h, bottom = top + h;
      if (top < list.scrollTop) { list.scrollTop = top; }
      else if (bottom > list.scrollTop + list.clientHeight) { list.scrollTop = bottom - list.clientHeight; }
    }

    function select(i, fetchNow) {
      if (i < 0 || i >= rows.length) { return; }
      selected = i;
      previewing = false;
      list.setAttribute('aria-activedescendant', 'ev-row-' + i);
      scrollToRow(i);
      render();
      if (fetchNow) { showDetail(rows[i], false); }
    }

    function preview(i) {
      if (i === selected && !previewing) { return; }
      previewing = true;
      showDetail(rows[i], true);
    }

    function clear() {
      selected = -1;
      previewing = false;
      list.removeAttribute('aria-activedescendant');
      if (controller) { controller.abort(); }
      detail.classList.remove('is-preview');
      detail.textContent = '';
      detail.appendChild(el('p', 'dash-status', 'Välj en rad för att se dess proveniens.'));
      render();
    }

    /* Fetch with cancellation: a newer selection or preview aborts the
     * request in flight, so fast movement never queues stale work. */
    function showDetail(r, isPreview) {
      if (controller) { controller.abort(); }
      detail.classList.toggle('is-preview', !!isPreview);
      var cached = cache[r.observationGuid];
      if (cached) { renderDetail(r, cached); return; }
      detail.textContent = '';
      detail.appendChild(el('p', 'dash-status', 'Hämtar proveniens…'));
      controller = typeof AbortController === 'function' ? new AbortController() : null;
      fetch('/dashboard/events/' + encodeURIComponent(r.observationGuid), {
        credentials: 'same-origin', headers: { 'Accept': 'application/json' },
        signal: controller ? controller.signal : undefined
      }).then(function (res) {
        return res.json().then(function (body) { return { res: res, body: body }; },
          function () { throw new Error('Oväntat svar från servern (' + res.status + ').'); });
      }).then(function (x) {
        if (x.res.status === 404) { throw new Error('Mätvärdet hittades inte.'); }
        if (!x.res.ok) { throw new Error((x.body && (x.body.message || x.body.error)) || 'fel ' + x.res.status); }
        cache[r.observationGuid] = x.body;
        renderDetail(r, x.body);
      }).catch(function (err) {
        if (err && err.name === 'AbortError') { return; }
        detail.textContent = '';
        detail.appendChild(el('p', 'dash-status is-error', 'Proveniens kunde inte hämtas: ' + err.message));
      });
    }

    var MISSING = 'ej registrerat';

    /* STEP 18: every field is listed, and an absent one says so BY NAME.
     * This panel is the platform's instrument for how much provenance is
     * actually captured; dropping an empty field would hide the gap. */
    function renderDetail(r, body) {
      var d = body.detail || {}, row = d.row || r, ctx = body.context || {};
      detail.textContent = '';
      detail.appendChild(el('h3', null, (row.displayName || row.codeCanonical || 'Mätning') +
        (detail.classList.contains('is-preview') ? ' (förhandsvisning)' : '')));

      function block(title, pairs) {
        detail.appendChild(el('h4', null, title));
        var dl = el('dl');
        pairs.forEach(function (pr) {
          dl.appendChild(el('dt', null, pr[0]));
          var v = pr[1];
          var empty = v === null || v === undefined || v === '';
          var dd = el('dd', empty ? 'is-missing' : null, empty ? MISSING : v);
          if (pr[2] && !empty) { dd.title = pr[2]; }
          dl.appendChild(dd);
        });
        detail.appendChild(dl);
      }
      var ref = (d.referenceLow !== null && d.referenceLow !== undefined) ||
                (d.referenceHigh !== null && d.referenceHigh !== undefined)
        ? (d.referenceLow !== null && d.referenceLow !== undefined ? d.referenceLow : '…') + ' – ' +
          (d.referenceHigh !== null && d.referenceHigh !== undefined ? d.referenceHigh : '…')
        : null;
      block('Värde', [
        ['Värde', valueText(row)],
        ['Tid', when(row.effectiveAt)],
        ['T+', tPlus(row.tFromStartSeconds)],
        ['Referensintervall', ref],
        ['Tolkning', d.interpretation],
        ['Metod', d.method],
        ['Utrustning', d.device]
      ]);
      block('Plan och beställning', [
        ['PlanDefinition', d.planDefinitionName ? d.planDefinitionName +
          (d.planDefinitionVersion ? ' v' + d.planDefinitionVersion : '') : null],
        ['Vårdplan', row.carePlanGuid],
        ['Beställning', row.serviceRequestGuid || ctx.service_request_guid],
        ['Besök (plan activity)', row.planActivityGuid || ctx.plan_activity_guid],
        ['Transaktion', row.transactionGuid],
        ['Kontrakt', d.contractGuid]
      ]);
      block('Organisation och källa', [
        ['Patientens organisation', d.patientOrgGuid],
        ['Utförande organisation', d.providerOrgGuid],
        ['Källa', row.sourceCdr],
        ['Mottagen', when(d.ingestedAt)],
        ['Ingest-id', row.ingestRawGuid],
        ['Observation', row.observationGuid],
        ['Begrepp', row.conceptGuid],
        ['Kod', row.codeCanonical]
      ]);
      var notes = [];
      if (body.contextFound === false) { notes.push('Ingen kontextrad hittades för mätvärdet i CDR1.'); }
      if ((body.contextFilledFromRaw || []).length) {
        notes.push('Fyllt från rå FHIR: ' + body.contextFilledFromRaw.join(', ') + '.');
      }
      if (!d.patientOrgGuid) {
        notes.push('Patientens organisation registreras inte per mätvärde (#774); utförande organisation är en annan sak och används inte i stället.');
      }
      notes.forEach(function (t) { detail.appendChild(el('p', 'dash-note', t)); });
      if (d.rawFhirObservation) {
        var det = el('details');
        det.appendChild(el('summary', null, 'Rå FHIR Observation'));
        det.appendChild(el('pre', null, JSON.stringify(d.rawFhirObservation, null, 2)));
        detail.appendChild(det);
      }
    }

    /* -- the plan link -- */
    function describeLink() {
      if (!linked) { linkEl.textContent = ''; return; }
      var n = 0, firstIdx = -1;
      rows.forEach(function (r, i) {
        if (r.transactionGuid === linked.tx) { n++; if (firstIdx < 0) { firstIdx = i; } }
      });
      linkEl.textContent = '';
      var total = linked.count;
      var known = total !== null && total !== undefined;
      // The plan's count is a LOWER BOUND when its scan stopped at its limit;
      // "n of m" would then read as a total, and can even print n > m.
      var of = !known ? '' : linked.complete ? ' av ' + total : ' (planen räknade minst ' + total + ')';
      linkEl.appendChild(document.createTextNode('Markerar ' + n + of +
        ' för ”' + (linked.title || linked.tx) + '”' + (done ? '' : ' bland laddade') + '.'));
      if (!done && (!known || !linked.complete || n < total)) {
        var more = el('button', 'ev-load', 'Ladda fler');
        more.type = 'button';
        more.addEventListener('click', function () { loadMore(); });
        linkEl.appendChild(more);
      }
      return firstIdx;
    }

    function link(target, scroll) {
      linked = target;
      var first = describeLink();
      if (scroll && first !== undefined && first >= 0) { list.scrollTop = first * rowH(); }
      render();
    }

    function onKey(ev) {
      if (!rows.length) { return; }
      var k = ev.key, i = selected;
      if (k === 'ArrowDown') { i = selected < 0 ? 0 : Math.min(rows.length - 1, selected + 1); }
      else if (k === 'ArrowUp') { i = selected < 0 ? 0 : Math.max(0, selected - 1); }
      else if (k === 'Home') { i = 0; }
      else if (k === 'End') { i = rows.length - 1; }
      else if (k === 'PageDown') { i = Math.min(rows.length - 1, Math.max(0, selected) + Math.floor(list.clientHeight / rowH())); }
      else if (k === 'PageUp') { i = Math.max(0, selected - Math.floor(list.clientHeight / rowH())); }
      else if (k === 'Enter' || k === ' ') { if (selected >= 0) { ev.preventDefault(); showDetail(rows[selected], false); } return; }
      else if (k === 'Escape') { if (selected >= 0) { ev.preventDefault(); ev.stopPropagation(); clear(); } return; }
      else { return; }
      ev.preventDefault();
      select(i, true);
    }

    function start() {
      p = panel('panel-events');
      list = p.body.querySelector('[data-role="ev-list"]');
      spacer = p.body.querySelector('[data-role="ev-spacer"]');
      detail = p.body.querySelector('[data-role="ev-detail"]');
      countEl = p.body.querySelector('[data-role="ev-count"]');
      linkEl = p.body.querySelector('[data-role="ev-link"]');
      var ticking = false;
      list.addEventListener('scroll', function () {
        if (ticking) { return; }
        ticking = true;
        (window.requestAnimationFrame || setTimeout)(function () { ticking = false; render(); });
      });
      list.addEventListener('keydown', onKey);
      list.addEventListener('mouseleave', function () {
        clearTimeout(hoverTimer);
        if (!previewing) { return; }
        previewing = false;
        if (selected >= 0) { showDetail(rows[selected], false); } else { clear(); }
      });
      if (cap.subscribe) {
        cap.subscribe(function () {
          var keep = selected;
          render();
          if (keep >= 0) { scrollToRow(keep); }
        });
      }
      getJson('/dashboard/patients/' + encodeURIComponent(guid) + '/events?limit=' + PAGE)
        .then(function (data) {
          rows = data.rows || [];
          nextCursor = data.nextCursor || null;
          done = !nextCursor;
          renderEventBanners(data);
          p.status.hidden = true;
          p.body.hidden = false;
          p.node.setAttribute('aria-busy', 'false');
          if (!rows.length) {
            countEl.textContent = 'Inga händelser registrerade i CDR1.';
          } else {
            updateCount();
          }
          render();
        }).catch(function (err) { showError(p, err); });
    }

    return { start: start, link: link };
  })();

  loadCard();
  loadPlan();
  events.start();
})();
