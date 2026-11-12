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

  // What one panel learned that another draws: the card's diagnoses (ICD-11
  // onset/abatement markers) and the plan data (T0, done markers).
  var shared = { diagnoses: [], planData: null };
  var planResolve;
  var planReady = new Promise(function (r) { planResolve = r; });

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
      shared.diagnoses = card.diagnoses || [];
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
      shared.planData = data;
      planResolve(data);
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
    }).catch(function (err) { planResolve(null); showError(p, err); });
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
    var filterQS = '', gen = 0, filtered = false;
    var selected = -1, linked = null, hoverTimer = null, previewing = false;
    var controller = null, cache = Object.create(null), selectListeners = [];
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
      countEl.textContent = t + (filtered ? ' (filtrerat)' : '');
    }

    function loadMore() {
      if (loading || done) { return Promise.resolve(); }
      loading = true;
      var mine = gen;
      var url = '/dashboard/patients/' + encodeURIComponent(guid) + '/events?limit=' + PAGE +
        filterQS + (nextCursor ? '&cursor=' + encodeURIComponent(nextCursor) : '');
      return getJson(url).then(function (data) {
        if (mine !== gen) { return; }   // a filter change superseded this page
        rows = rows.concat(data.rows || []);
        nextCursor = data.nextCursor || null;
        done = !nextCursor;
        renderEventBanners(data);
        loading = false;
        updateCount();
        if (linked) { describeLink(); }
        render();
      }).catch(function (err) {
        if (mine !== gen) { return; }
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
      selectListeners.forEach(function (fn) { fn(rows[i]); });
    }

    /* The chart selects a row by its observation guid. False when that row
     * is not loaded yet (the list is paged). */
    function selectGuid(g) {
      for (var i = 0; i < rows.length; i++) {
        if (rows[i].observationGuid === g) { select(i, true); return true; }
      }
      return false;
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
    }

    /* (Re)load the list under a filter. Everything list-local resets; a
     * page still in flight for the old filter is discarded by `gen`. An
     * empty-result filter says so, never "no events registered". */
    function reload(qs, isFiltered, noMatch) {
      gen += 1;
      filterQS = qs || '';
      filtered = !!isFiltered;
      rows = []; nextCursor = null; loading = false; done = false; failed = null;
      selected = -1; previewing = false;
      list.removeAttribute('aria-activedescendant');
      list.scrollTop = 0;
      p.status.hidden = true;
      p.body.hidden = false;
      p.node.setAttribute('aria-busy', 'false');
      if (noMatch) {
        done = true;
        countEl.textContent = 'Inga händelser visas — se filtret ovan.';
        render();
        return;
      }
      countEl.textContent = 'Hämtar…';
      render();
      var mine = gen;
      loadMore().then(function () {
        if (mine !== gen) { return; }
        if (!rows.length && done && !failed) {
          countEl.textContent = filtered ? 'Inga händelser matchar filtret.'
                                         : 'Inga händelser registrerade i CDR1.';
        }
      });
    }

    return { start: start, link: link, selectGuid: selectGuid, reload: reload,
             onSelect: function (fn) { selectListeners.push(fn); } };
  })();

  /* ---- filters (#977 part B) ----------------------------------------------
   * Held in the page URL (replaceState), so a filtered view can be reloaded
   * or shared. Every filter is applied BY CDR1 over the whole history, not
   * over the loaded pages:
   *   - free text matches the patient's concept NAMES and becomes a concept
   *     filter; text that matches nothing shows nothing, never everything;
   *   - a plan becomes its transactions (cdr1 must confirm that filter, or
   *     the server refuses rather than return the unfiltered history);
   *   - dates are Stockholm calendar days, validated by the server.
   */
  var filters = (function () {
    var form, concepts = [], conceptsKnown = false, timer = null, rejected = [];

    function readUrl() {
      var q = new URLSearchParams(window.location.search);
      ['q', 'code', 'from', 'to'].forEach(function (k) {
        if (!q.get(k) || !form.elements[k]) { return; }
        form.elements[k].value = q.get(k);
        // A date input REFUSES an invalid value silently (it becomes ''),
        // which would drop the filter and show the whole history as if
        // filtered — the very defect the server guards against. Record it.
        if (form.elements[k].value !== q.get(k)) { rejected.push(k + '=' + q.get(k)); }
      });
      return q;
    }

    function writeUrl() {
      var q = new URLSearchParams();
      ['q', 'code', 'from', 'to', 'plan'].forEach(function (k) {
        var v = form.elements[k].value;
        if (v) { q.set(k, v); }
      });
      var qs = q.toString();
      try {
        history.replaceState(null, '', window.location.pathname + (qs ? '?' + qs : ''));
      } catch (e) { /* a sandboxed frame may refuse; the filter still applies */ }
    }

    function planTransactions(planKey) {
      var data = shared.planData || {};
      if (planKey === 'unordered') {
        return (data.unordered || []).map(function (u) { return u.transactionGuid; });
      }
      var plan = (data.plans || []).filter(function (pl) {
        return (pl.serviceRequestGuid || pl.carePlanGuid) === planKey;
      })[0];
      return plan ? (plan.items || []).map(function (i) { return i.transactionGuid; }).filter(Boolean) : [];
    }

    function apply() {
      if (rejected.length) {
        // Leave the URL as typed so the reader can see what was refused.
        form.parentNode.querySelector('[data-role="ev-filter-note"]').textContent =
          'Ogiltigt filter i adressen (' + rejected.join(', ') + '). Rätta det eller tryck Rensa.';
        events.reload('', true, true);
        return;
      }
      writeUrl();
      var el_ = form.elements, parts = [], note = '', noMatch = false;
      var code = el_.code.value, text = el_.q.value.trim().toLowerCase();
      if (code) {
        parts.push('code=' + encodeURIComponent(code));
      } else if (text) {
        if (!conceptsKnown) {
          note = 'Begreppslistan kunde inte läsas — fritextsökning är inte möjlig just nu.';
          noMatch = true;
        } else {
          var hits = concepts.filter(function (c) {
            return (c.displayName || '').toLowerCase().indexOf(text) !== -1 ||
                   (c.code || '').toLowerCase().indexOf(text) !== -1;
          });
          if (!hits.length) { noMatch = true; note = 'Inga av patientens begrepp matchar ”' + el_.q.value.trim() + '”.'; }
          hits.forEach(function (c) { parts.push('code=' + encodeURIComponent(c.code)); });
          if (hits.length) { note = 'Söker i ' + hits.length + ' begrepp.'; }
        }
      }
      if (el_.from.value) { parts.push('from=' + encodeURIComponent(el_.from.value)); }
      if (el_.to.value) { parts.push('to=' + encodeURIComponent(el_.to.value)); }
      if (el_.plan.value) {
        var txs = planTransactions(el_.plan.value);
        if (!txs.length) { noMatch = true; note = 'Planen har inga transaktioner att filtrera på.'; }
        txs.forEach(function (t) { parts.push('transaction=' + encodeURIComponent(t)); });
      }
      form.parentNode.querySelector('[data-role="ev-filter-note"]').textContent = note;
      events.reload(parts.length ? '&' + parts.join('&') : '', parts.length > 0 || noMatch, noMatch);
    }

    function fillConcepts() {
      var dl = form.parentNode.querySelector('[data-role="ev-concepts"]');
      var sel = form.querySelector('[data-role="ev-code"]');
      var keep = sel.value;
      concepts.forEach(function (c) {
        dl.appendChild(el('option', null, c.displayName));
        var o = el('option', null, c.displayName + ' (' + c.count + ')');
        o.value = c.code;
        sel.appendChild(o);
      });
      sel.value = keep;
    }

    function fillPlans(data) {
      var sel = form.querySelector('[data-role="ev-plan"]');
      var keep = sel.value;
      ((data || {}).plans || []).forEach(function (pl) {
        var key = pl.serviceRequestGuid || pl.carePlanGuid;
        if (!key) { return; }
        var o = el('option', null, (pl.planDefinitionName || 'Plan') +
          (pl.orderedAt ? ' — ' + String(pl.orderedAt).slice(0, 10) : ''));
        o.value = key;
        sel.appendChild(o);
      });
      if (((data || {}).unordered || []).length) {
        var u = el('option', null, 'Mätningar utan beställning');
        u.value = 'unordered';
        sel.appendChild(u);
      }
      sel.value = keep;
    }

    function start() {
      form = document.querySelector('[data-role="ev-filters"]');
      var url = readUrl();
      var conceptsLoaded = getJson('/dashboard/patients/' + encodeURIComponent(guid) + '/concepts')
        .then(function (d) { concepts = d.concepts || []; conceptsKnown = true; fillConcepts(); })
        .catch(function () { conceptsKnown = false; });
      var plansLoaded = planReady.then(fillPlans);
      // Apply the URL's filter only once what it refers to is known; a
      // plain visit loads at once.
      var waits = [];
      if (url.get('q') || url.get('code')) { waits.push(conceptsLoaded); }
      if (url.get('plan')) { waits.push(plansLoaded); }
      Promise.all(waits).then(apply);

      form.addEventListener('submit', function (ev) { ev.preventDefault(); apply(); });
      form.addEventListener('change', function (ev) {
        rejected = [];
        if (ev.target.name !== 'q') { apply(); }
      });
      form.elements.q.addEventListener('input', function () {
        clearTimeout(timer);
        timer = setTimeout(apply, 350);
      });
      form.addEventListener('reset', function () { rejected = []; setTimeout(apply, 0); });
    }

    return { start: start };
  })();

  /* ---- series panel (#978) -------------------------------------------------
   * Driven by the SELECTED row, never by hover: the chart has a stable subject
   * and does not change under the pointer while it is being read.
   *
   *  - continuous time axis; a gap longer than 3x the median interval BREAKS
   *    the line rather than being bridged;
   *  - out-of-range values get a TRIANGLE, the selected point a DIAMOND —
   *    shape and label, never colour alone;
   *  - vertical markers: T0 (plan start), done plan items, ICD-11 onset and
   *    abatement — each a different dash AND a text label;
   *  - zoom/pan via chartjs-plugin-zoom (vendored, with hammer.js): pinch and
   *    horizontal pan on touch, ctrl+wheel and drag with a mouse. The canvas
   *    gets `touch-action: pan-y`, so a vertical drag still scrolls the page.
   */
  var series = (function () {
    var p, canvas, chart = null, current = null, selectedGuid = null;
    var unitIdx = 0, rangeDays = 'all', controller = null;
    var refineTimer = null, refineCtl = null, refined = false;
    var DAY = 86400000;

    function token(name, fallback) {
      var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
      return v || fallback;
    }

    function codePath(code) {
      // Keep '/' literal: the route takes <path:...>, and an encoded %2F is
      // something a reverse proxy may normalise or refuse.
      return String(code).split('/').map(encodeURIComponent).join('/');
    }

    function fmtDay(ms) {
      try {
        return new Intl.DateTimeFormat('sv-SE', { timeZone: 'Europe/Stockholm', dateStyle: 'short' })
          .format(new Date(ms));
      } catch (e) { return new Date(ms).toISOString().slice(0, 10); }
    }

    /* An axis tick: the day, plus the time when the tick is not at local
     * midnight — otherwise two ticks on one day print the same label. */
    var TICK_TIME = (function () {
      try { return new Intl.DateTimeFormat('sv-SE', { timeZone: 'Europe/Stockholm', hour: '2-digit', minute: '2-digit' }); }
      catch (e) { return null; }
    })();
    function fmtTick(ms) {
      var day = fmtDay(ms);
      var t = TICK_TIME ? TICK_TIME.format(new Date(ms)) : '00:00';
      return t === '00:00' ? day : day + ' ' + t;
    }

    function num(v) {
      return Math.abs(v) >= 100 ? String(Math.round(v * 10) / 10) : String(Math.round(v * 100) / 100);
    }

    /* Insert a null between two points further apart than 3x the median
     * interval, so the line breaks there instead of implying continuity. */
    function withGaps(pts) {
      if (pts.length < 3) { return pts.slice(); }
      var gaps = [];
      for (var i = 1; i < pts.length; i++) { gaps.push(pts[i].x - pts[i - 1].x); }
      var sorted = gaps.slice().sort(function (a, b) { return a - b; });
      var median = sorted[Math.floor(sorted.length / 2)];
      var out = [pts[0]];
      for (var j = 1; j < pts.length; j++) {
        if (median > 0 && pts[j].x - pts[j - 1].x > 3 * median) {
          out.push({ x: (pts[j].x + pts[j - 1].x) / 2, y: null, gap: true });
        }
        out.push(pts[j]);
      }
      return out;
    }

    function outOfRange(pt) {
      return (pt.lo !== null && pt.lo !== undefined && pt.y < pt.lo) ||
             (pt.hi !== null && pt.hi !== undefined && pt.y > pt.hi);
    }

    function markers() {
      var out = [];
      ((shared.planData || {}).plans || []).forEach(function (pl) {
        if (pl.periodStart) { out.push({ x: Date.parse(pl.periodStart), label: 'T0 ' + (pl.planDefinitionName || ''), dash: [], kind: 't0' }); }
        (pl.items || []).forEach(function (it) {
          if (it.state === 'done' && it.performedAt) {
            out.push({ x: Date.parse(it.performedAt), label: 'Klar: ' + it.title, dash: [2, 3], kind: 'done' });
          }
        });
      });
      (shared.diagnoses || []).forEach(function (d) {
        if (d.onset) { out.push({ x: Date.parse(d.onset), label: d.code + ' debut', dash: [6, 4], kind: 'dx' }); }
        if (d.abatement) { out.push({ x: Date.parse(d.abatement), label: d.code + ' avslutad', dash: [10, 3, 2, 3], kind: 'dx' }); }
      });
      return out.filter(function (m) { return !isNaN(m.x); });
    }

    function t0() {
      var starts = ((shared.planData || {}).plans || []).map(function (pl) { return pl.periodStart; }).filter(Boolean);
      return starts.length === 1 ? Date.parse(starts[0]) : null;
    }

    var markerPlugin = {
      id: 'pdhcMarkers',
      afterDatasetsDraw: function (c) {
        var x = c.scales.x, area = c.chartArea, ctx = c.ctx;
        if (!x || !area) { return; }
        var band = c.options.plugins.pdhcMarkers || {};
        ctx.save();
        if (band.lo !== null && band.lo !== undefined && band.hi !== null && band.hi !== undefined) {
          var y = c.scales.y, top = y.getPixelForValue(band.hi), bottom = y.getPixelForValue(band.lo);
          ctx.fillStyle = band.fill;
          ctx.fillRect(area.left, Math.max(area.top, top), area.right - area.left,
            Math.min(area.bottom, bottom) - Math.max(area.top, top));
        }
        ctx.font = '11px system-ui, sans-serif';
        ctx.fillStyle = band.text;
        ctx.strokeStyle = band.line;
        (band.markers || []).forEach(function (m, i) {
          var px = x.getPixelForValue(m.x);
          if (px < area.left || px > area.right) { return; }
          ctx.setLineDash(m.dash);
          ctx.beginPath(); ctx.moveTo(px, area.top); ctx.lineTo(px, area.bottom); ctx.stroke();
          ctx.setLineDash([]);
          ctx.fillText(m.label, Math.min(px + 3, area.right - ctx.measureText(m.label).width),
            area.top + 12 + (i % 3) * 12);
        });
        ctx.restore();
      }
    };

    function stats(s) {
      var pts = s.points || [];
      var host = p.body.querySelector('[data-role="ser-stats"]');
      host.textContent = '';
      if (!pts.length) { return; }
      var vals = pts.map(function (q) { return q.value; });
      var pairs = [
        ['Punkter', s.downsampledFrom ? pts.length + ' av ' + s.downsampledFrom : String(pts.length)],
        ['Första', fmtDay(Date.parse(pts[0].effectiveAt))],
        ['Senaste', fmtDay(Date.parse(pts[pts.length - 1].effectiveAt))],
        ['Min', num(Math.min.apply(null, vals))],
        ['Max', num(Math.max.apply(null, vals))],
        ['Senaste värde', num(vals[vals.length - 1]) + (s.unit ? ' ' + s.unit : '')]
      ];
      if (s.downsampledFrom) { pairs[3][0] = 'Min (visade)'; pairs[4][0] = 'Max (visade)'; }
      pairs.forEach(function (pr) {
        var d = el('div');
        d.appendChild(el('dt', null, pr[0]));
        d.appendChild(el('dd', null, pr[1]));
        host.appendChild(d);
      });
    }

    function notes(data, s) {
      var host = p.body.querySelector('[data-role="ser-notes"]');
      host.textContent = '';
      if (s && s.downsampledFrom) {
        banner(host, 'Visar ' + s.points.length + ' av ' + s.downsampledFrom +
          ' punkter (LTTB, toppar och dalar bevaras).');
      }
      if (!data.complete) { banner(host, 'Ofullständig serie: äldre punkter saknas (genomsökningen nådde sin gräns).'); }
      if (data.nonNumericCount) { banner(host, data.nonNumericCount + ' mätvärden är inte numeriska och ritas inte.'); }
      if (data.sparrChecked === false) { banner(host, 'Spärr kunde inte kontrolleras.', true); }
      else if (data.hasBlockedSources) { banner(host, 'Spärrade mätvärden visas inte.', true); }
      if (data.series.length > 1) {
        banner(host, 'Begreppet finns i ' + data.series.length + ' enheter som inte går att räkna om — en serie per enhet.');
      }
      if (t0() === null) { banner(host, 'Ingen planstart (T0) registrerad — ingen T+-axel.'); }
    }

    function units(data) {
      var host = p.body.querySelector('[data-role="ser-units"]');
      host.textContent = '';
      if (data.series.length < 2) { return; }
      data.series.forEach(function (s, i) {
        var b = el('button', 'dash-toggle ser-btn', (s.unit || 'utan enhet') + ' (' + (s.downsampledFrom || s.points.length) + ')');
        b.type = 'button';
        b.setAttribute('aria-pressed', i === unitIdx ? 'true' : 'false');
        b.addEventListener('click', function () { unitIdx = i; draw(); });
        host.appendChild(b);
      });
    }

    function applyRange() {
      if (!chart) { return; }
      var xs = chart.data.datasets[0].data.map(function (q) { return q.x; });
      if (!xs.length) { return; }
      var max = xs[xs.length - 1];
      var min = rangeDays === 'all' ? xs[0] : max - Number(rangeDays) * DAY;
      if (chart.resetZoom) { chart.resetZoom('none'); }
      chart.options.scales.x.min = rangeDays === 'all' ? undefined : min;
      chart.options.scales.x.max = rangeDays === 'all' ? undefined : max;
      chart.update('none');
      if (rangeDays === 'all' && refined) { refined = false; draw(); return; }
      if (rangeDays !== 'all') { refine(); }
      Array.prototype.forEach.call(p.body.querySelectorAll('.ser-range .ser-btn'), function (b) {
        b.setAttribute('aria-pressed', b.getAttribute('data-days') === String(rangeDays) ? 'true' : 'false');
      });
    }

    function draw() {
      var data = current.data, s = data.series[unitIdx];
      p.body.querySelector('[data-role="ser-name"]').textContent = data.displayName || current.code;
      p.body.querySelector('[data-role="ser-code"]').textContent = current.code + (s && s.unit ? ' · ' + s.unit : '');
      p.body.querySelector('[data-role="ser-csv"]').href =
        '/dashboard/patients/' + encodeURIComponent(guid) + '/series/' + codePath(current.code) + '?format=csv';
      units(data);
      notes(data, s);
      if (chart) { chart.destroy(); chart = null; }
      if (!s || !s.points.length) {
        p.body.querySelector('[data-role="ser-stats"]').textContent = '';
        p.body.querySelector('[data-role="ser-legend"]').textContent = 'Inga numeriska punkter för begreppet.';
        return;
      }
      stats(s);
      var pts = s.points.map(function (q) {
        return { x: Date.parse(q.effectiveAt), y: q.value, guid: q.observationGuid,
                 lo: q.referenceLow, hi: q.referenceHigh };
      });
      var los = pts.map(function (q) { return q.lo; }), his = pts.map(function (q) { return q.hi; });
      var sameBand = los.every(function (v) { return v === los[0]; }) && his.every(function (v) { return v === his[0]; });
      var primary = token('--primary', '#2563eb'), danger = token('--danger', '#dc2626');
      var text = token('--text', '#1e293b'), muted = token('--text-muted', '#64748b');
      var dense = pts.length > 300;
      var zero = t0();

      chart = new Chart(canvas, {
        type: 'line',
        data: { datasets: [{
          data: withGaps(pts), parsing: false, spanGaps: false,
          borderColor: primary, borderWidth: 1.5, tension: 0,
          pointStyle: function (c) {
            var q = c.raw || {};
            if (q.guid && q.guid === selectedGuid) { return 'rectRot'; }
            return outOfRange(q) ? 'triangle' : 'circle';
          },
          pointRadius: function (c) {
            var q = c.raw || {};
            if (q.guid && q.guid === selectedGuid) { return 8; }
            if (outOfRange(q)) { return 5; }
            return dense ? 0 : 3;
          },
          pointHitRadius: 8,
          pointBackgroundColor: function (c) {
            var q = c.raw || {};
            if (q.guid && q.guid === selectedGuid) { return text; }
            return outOfRange(q) ? danger : primary;
          }
        }] },
        options: {
          animation: false, maintainAspectRatio: false, normalized: true,
          interaction: { mode: 'nearest', intersect: true },
          scales: {
            x: { type: 'time', time: { tooltipFormat: 'yyyy-MM-dd HH:mm' },
                 ticks: { color: muted, maxRotation: 0, autoSkip: true,
                   callback: function (v) {
                     var day = fmtTick(v);
                     if (zero === null) { return day; }
                     var d = Math.round((v - zero) / DAY);
                     return [day, (d < 0 ? 'T−' : 'T+') + Math.abs(d) + ' d'];
                   } },
                 grid: { color: token('--border', '#e2e8f0') } },
            y: { ticks: { color: muted }, grid: { color: token('--border', '#e2e8f0') },
                 title: { display: !!s.unit, text: s.unit || '', color: muted } }
          },
          plugins: {
            legend: { display: false },
            tooltip: { callbacks: { label: function (c) {
              var q = c.raw || {};
              return num(q.y) + (s.unit ? ' ' + s.unit : '') + (outOfRange(q) ? ' (utanför referens)' : '') +
                (q.guid === selectedGuid ? ' (vald)' : '');
            } } },
            pdhcMarkers: { lo: sameBand ? los[0] : null, hi: sameBand ? his[0] : null,
              fill: 'rgba(22, 163, 74, 0.10)', line: muted, text: text, markers: markers() },
            zoom: {
              zoom: { wheel: { enabled: true, modifierKey: 'ctrl' }, pinch: { enabled: true }, mode: 'x' },
              pan: { enabled: true, mode: 'x', onPanComplete: function () { refine(); } },
              limits: { x: { min: 'original', max: 'original' } },
              onZoomComplete: function () { refine(); }
            }
          },
          onClick: function (evt, els) {
            if (!els.length) { return; }
            var q = chart.data.datasets[0].data[els[0].index];
            if (q && q.guid && !events.selectGuid(q.guid)) {
              p.body.querySelector('[data-role="ser-legend"]').textContent =
                'Punkten ligger längre bak i händelselistan än vad som är laddat.';
            }
          }
        },
        plugins: [markerPlugin]
      });
      // AFTER Chart.js and hammer attach: let a vertical drag scroll the page.
      canvas.style.touchAction = 'pan-y';
      canvas.setAttribute('aria-label', (data.displayName || current.code) + ': ' + pts.length +
        ' punkter från ' + fmtDay(pts[0].x) + ' till ' + fmtDay(pts[pts.length - 1].x) +
        ', senaste ' + num(pts[pts.length - 1].y) + (s.unit ? ' ' + s.unit : '') + '.');
      p.body.querySelector('[data-role="ser-legend"]').textContent =
        'Romb = vald mätning. Triangel = utanför referensintervall. Streckade linjer = plan- och diagnoshändelser. ' +
        'Zooma: nyp (touch) eller ctrl+hjul. Panorera: dra i sidled.';
      applyRange();
    }

    /* FULL RESOLUTION WHEN ZOOMED IN (plan step 20). The overview is LTTB-
     * downsampled server-side; once the visible window is narrower, ask for
     * just that window. If it still exceeds the target it comes back
     * downsampled too, and the note says so — never a claim of "all". */
    function refine() {
      var s = current && current.data.series[unitIdx];
      if (!chart || !s || !s.downsampledFrom) { return; }
      clearTimeout(refineTimer);
      refineTimer = setTimeout(function () {
        var x = chart.scales.x;
        var frm = new Date(x.min).toISOString(), to = new Date(x.max).toISOString();
        if (refineCtl) { refineCtl.abort(); }
        refineCtl = typeof AbortController === 'function' ? new AbortController() : null;
        fetch('/dashboard/patients/' + encodeURIComponent(guid) + '/series/' + codePath(current.code) +
              '?from=' + encodeURIComponent(frm) + '&to=' + encodeURIComponent(to), {
          credentials: 'same-origin', headers: { 'Accept': 'application/json' },
          signal: refineCtl ? refineCtl.signal : undefined
        }).then(function (res) { return res.ok ? res.json() : null; }).then(function (data) {
          if (!data || !chart) { return; }
          var w = (data.series || []).filter(function (q) { return q.unit === s.unit; })[0];
          if (!w) { return; }
          chart.data.datasets[0].data = withGaps(w.points.map(function (q) {
            return { x: Date.parse(q.effectiveAt), y: q.value, guid: q.observationGuid,
                     lo: q.referenceLow, hi: q.referenceHigh };
          }));
          chart.update('none');
          refined = true;
          var host = p.body.querySelector('[data-role="ser-notes"]');
          var first = host.firstChild;
          var text = w.downsampledFrom
            ? 'Intervallet: visar ' + w.points.length + ' av ' + w.downsampledFrom + ' punkter (LTTB).'
            : 'Intervallet: alla ' + w.points.length + ' punkter (full upplösning).';
          if (first && /^Visar |^Intervallet: /.test(first.textContent)) { first.textContent = text; }
        }).catch(function () { /* the overview stays; nothing is claimed */ });
      }, 300);
    }

    function load(row) {
      if (controller) { controller.abort(); }
      controller = typeof AbortController === 'function' ? new AbortController() : null;
      p.status.hidden = false;
      p.status.classList.remove('is-error');
      p.status.textContent = 'Hämtar tidsserie…';
      fetch('/dashboard/patients/' + encodeURIComponent(guid) + '/series/' + codePath(row.codeCanonical), {
        credentials: 'same-origin', headers: { 'Accept': 'application/json' },
        signal: controller ? controller.signal : undefined
      }).then(function (res) {
        return res.json().then(function (body) {
          if (!res.ok) { throw new Error((body && (body.message || body.error)) || 'fel ' + res.status); }
          return body;
        }, function () { throw new Error('Oväntat svar från servern (' + res.status + ').'); });
      }).then(function (data) {
        current = { code: row.codeCanonical, data: data };
        unitIdx = 0;
        data.series.forEach(function (s, i) {
          if (s.points.some(function (q) { return q.observationGuid === selectedGuid; })) { unitIdx = i; }
        });
        p.status.hidden = true;
        p.body.hidden = false;
        p.node.setAttribute('aria-busy', 'false');
        draw();
      }).catch(function (err) {
        if (err && err.name === 'AbortError') { return; }
        showError(p, err);
      });
    }

    function show(row) {
      if (!row || !row.codeCanonical) { return; }
      selectedGuid = row.observationGuid;
      if (current && current.code === row.codeCanonical && chart) {
        chart.update('none');   // same series: only the highlight moves
        return;
      }
      load(row);
    }

    function start() {
      p = panel('panel-series');
      canvas = p.body.querySelector('[data-role="ser-canvas"]');
      Array.prototype.forEach.call(p.body.querySelectorAll('.ser-range .ser-btn'), function (b) {
        b.addEventListener('click', function () { rangeDays = b.getAttribute('data-days'); applyRange(); });
      });
      events.onSelect(show);
    }

    return { start: start };
  })();

  loadCard();
  loadPlan();
  events.start();
  series.start();
  filters.start();
})();
