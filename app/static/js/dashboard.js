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
    request: 'Planerna kunde inte läsas.',
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

  loadCard();
})();
