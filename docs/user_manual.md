# dashboard — User Manual

**dashboard** is the individual / point-of-care **clinical-decision-assist**
tool of the PDHC platform. It lets a
treating clinician look at **one patient at a time**: their latest values,
time series, ambulatory glucose profile (AGP), and clinical events. It runs on
`dashboard.pdhc.se`.

> **Population / cohort analysis moved out.** The group features — the
> researcher cohort builder, cohort exports, and federated
> observation-search / statistics — are no longer here. They now live in the
> separate **analyse.pdhc** service (`analyse.pdhc.se`). If you are looking for
> aggregate distributions across many patients, go there.

## 1. Logging in and access
- **Development**: `AUTH_MODE=off` — you are logged in automatically as a dev
  super-user. No credentials required.
- **Production**: `AUTH_MODE=sso` — log in via the PDHC SSO portal. Your session
  is re-validated with SSO on every request, so a logout (or an admin revoking
  your session) takes effect immediately.
- **Access is a care relationship, not an analysis grant.** You reach dashboard
  if you are an SU admin, or a professional with at least one **care-unit
  scope** (a care relationship). You do **not** need the *analysis* phase — that
  phase gates analyse.pdhc, not this service.
- **Org scoping (Rule 24)**: admins see all organisations; a regular clinician
  sees only patients belonging to their own care units.
- **Spärr (per-patient blocks)**: if a patient has blocked one or more clinics,
  data from those clinics is withheld from every view. By default the block is
  coarse (all of a blocked clinic's data is hidden). Where the DPO-approved
  *indispensable-care* lift is enabled, specifically-covered data may be shown,
  and every such exposure is separately audited. A metadata-only banner tells you
  when a patient has blocked sources so you know the picture may be incomplete.

## 2. Choosing a patient
After login you land on the **patient picker** (`/select`, "Välj patient"):
- One row per patient **listed at the care units you are affiliated to** in the
  patient register (eps), plus every patient with data for those units in CDR1.
  A patient registered today, with no data yet, is listed too ("ingen data än"),
  so you can open them and invite them to the patient app.
- Each row: **name** and **personnummer** (YYYYMMDD-XXXX) from the patient
  register, the care unit, the number of data points and the date of the
  latest data from CDR1. Patients with fresh data come first.
- A patient CDR1 has data for but the register does not list at your units
  shows its id and **"ej i patientregistret"**.
- The search box matches name, personnummer (with or without the dash, also
  YYMMDD-XXXX) and id. Click a row to open the patient.
- If the register does not answer, a yellow banner says **"Patientregistret
  (eps) svarar inte – namn saknas"** and the list shows only the CDR1 patients,
  by id ("namn okänt"). The list is never silently empty or nameless.
- Administrators: after stating a reason (break-glass), you see every patient
  with data in CDR1; names and personnummer are shown for patients at care
  units you are yourself affiliated to, the others read "namn okänt".
- If no CDR1 is configured (local dev), the page says so.

## 3. Per-patient charts (`/patient/<guid>/charts`)
The charts view is the core single-patient surface, backed by CDR1:
- **Parameter list** — the concepts this patient has data for (the dropdown).
- **Series** — pick one or more concepts and a time window; the chart draws the
  points. Data is read from CDR1 under a care-delivery basis and spärr-filtered
  on this side before it reaches your browser (the browser never talks to CDR1
  directly).
- The banner flags when the patient has blocked sources.
- **Bjud in till patientappen** (professionals): invite this patient to the
  patient app — see section 7b.

## 4. Nurse single-patient views (`/nurse`)
The **nurse workspace** page (`/nurse`, reachable via `/workspace`) drives four
single-patient data views, federated across the regional CDRs (CDR2–5):
- **Summary** — demographics, active conditions, current regimen, and the most
  recent value per concept over the last 90 days.
- **AGP** — the Ambulatory Glucose Profile (14-day or 90-day window): glucose
  percentile bands and summary statistics.
- **Variable** — a single variable's full time series, automatically
  downsampled for display when very dense.
- **Events** — clinical event markers: encounters and severe-hypoglycaemia
  events.

All four are care-delivery gated and spärr-filtered per patient, exactly like
the charts view. Access to the nurse page itself requires the **nurse** role (or
admin).

## 5. Saved designs
You can save a **design** — a personal, reusable view configuration (which
diagrams/variables to show). Designs are private to you: there is no shared or
admin view of another user's designs. Manage them via `/api/v1/designs`
(list / create / get / update / delete).

## 6. Audit (admins)
SU admins can browse the read-audit log at `/admin/audit` (filter by date,
user, patient, route, event type; paginated) and export the filtered set as CSV
at `/admin/audit/export.csv`. Every patient-touching read — including denials
and spärr-lift exposures — is recorded here.

## 7. EPS att granska (för vårdpersonal)
Patienter kan själva lämna uppgifter till sin EPS (patientöversikt) i
patientappen: allergier, läkemedel, tidigare sjukdomar, vaccinationer och
kontaktpersoner. Uppgifterna räknas inte som vårdens förrän någon i vården har
granskat dem. Det gör du i kön **EPS att granska**, som nås från menyraden.
Siffran efter länken är antalet svar som väntar. Ett frågetecken betyder att
antalet inte kunde läsas, inte att kön är tom.

**Vem kan granska.** Du ser svar från patienter som är listade på de
vårdenheter du är affilierad till. För att besluta behöver du vara läkare
eller sjuksköterska på patientens vårdenhet. En administratör kan se kön
(efter att ha angett skäl, eftersom det är åtkomst utanför den egna enheten)
men kan inte besluta. Poster du kan se men inte besluta om visas utan knappar.

**Så ser kön ut.** Svaren är grupperade per patient och visas i den ordning
de kommit in. Kön sorterar inte efter hur viktigt ett svar verkar och föreslår
inget beslut. För varje svar ser du avsnitt, fråga, patientens svar precis som
det skrevs, när det skickades in, och märket **Patient**, som betyder att
uppgiften kommer från patienten och inte är granskad.

**Tre val per svar.**

- **Godkänn**: svaret stämmer som det står.
- **Ändra och godkänn**: svaret behöver rättas, till exempel en stavning eller
  en dos. Du skriver det rätta värdet. Patientens ursprungliga svar står kvar
  och visas bredvid.
- **Avvisa**: svaret ska inte in i EPS. Du måste ange ett skäl.

Efter valet visas en sida där du kontrollerar uppgifterna. Först när du
trycker **Bekräfta och skicka** registreras beslutet. Vid godkännande kan du
lägga till en valfri anteckning. Därefter försvinner svaret ur kön.

**Vad ett godkännande innebär.** Det skapas en ny post i vårdens namn, med dig
som den som intygar, och den pekar på patientens original. Originalet ändras
aldrig, varken vid godkännande, ändring eller avvisning, så det går alltid att
se vad patienten skrev och vad vården intygade. Det är den nya posten, inte
patientens, som kan ingå i patientens EPS, där du anges som intygande.

**Vad patienten ser vid avvisning.** Patienten ser i appen att svaret har
avvisats, skälet du skrev och när beslutet fattades. Patienten ser inte vem
som beslutade. Skriv därför skälet så att patienten förstår det och vet vad
hen ska göra, till exempel kontakta mottagningen.

**Om något går fel.**

- *"Någon annan har redan beslutat"*: en kollega hann före. Ditt beslut
  registrerades inte.
- *"Du har inte behörighet"* eller *"Du får inte besluta"*: CDR avvisade dig.
  Inget registrerades. Det betyder inte att kön är tom.
- *"CDR svarar inte"*: kön kunde inte läsas. Försök igen om en stund.
- *"Beslutet kunde inte bekräftas"*: det är okänt om beslutet gick fram. Öppna
  kön igen. Finns svaret kvar har inget registrerats.

Att kön öppnas och varje beslut loggas (vem, när, vilken post), utan själva
svarstexten.

### 7.1 Patientkort (version 2)
I version 2 skickar patienten ett helt avsnitt av sitt patientkort åt gången,
till exempel alla sina allergier. Sådana versioner visas överst i kön under
**Patientkort att granska**. Enskilda svar från version 1 visas som förut
längre ned, under **Enskilda svar**.

**Det senast skickade gäller.** Skickar patienten en ny version av ett avsnitt
innan den förra har granskats, ersätts den förra och försvinner ur kön. Du
granskar alltid den senaste.

**Så läser du en version.** Versionerna är grupperade per patient och avsnitt.
Varje rad visar frågan, vad patienten nu föreslår, vad som är godkänt i dag och
en markering:

- **Ny**: finns inte i det som är godkänt i dag.
- **Ändrad**: samma fråga, annat svar.
- **Borttagen**: patienten har tagit bort uppgiften. Godkänner du versionen
  försvinner den ur patientens EPS.
- **Oförändrad**: samma som i dag.

En allergi (eller annan post med flera fält) jämförs som en helhet. Har
patienten ändrat något i en sådan post visas den ofta som en borttagen och en
ny post, eftersom det inte går att veta säkert att det är samma post. Läs båda.
Finns ingen godkänd version sedan tidigare är allt markerat **Ny**.

Markeringen är bara en jämförelse av texten. Den säger inget om vad som är
rätt och föreslår inget beslut.

På iPad i stående läge och på mindre skärmar visas varje rad som ett block
där varje värde har sin rubrik (Föreslaget, Nu godkänt, Markering).

**Två val per version.**

- **Godkänn versionen**: hela versionen godkänns. Vill du rätta enstaka värden
  öppnar du **Ändra enskilda värden (valfritt)** och skriver det rätta värdet i
  rätt fält. Lämna övriga fält tomma. Svar som patienten valt i en lista (till
  exempel svårighetsgrad) kan inte ändras här; avvisa versionen med ett skäl om
  de är fel.
- **Avvisa versionen**: ingen rad i versionen godkänns, och det som är godkänt
  i dag gäller som förut. Du måste ange ett skäl, som patienten ser.

Därefter visas en bekräftelsesida med jämförelsen och dina ändringar
(**Ändras av dig till**). Först **Bekräfta och skicka** registrerar beslutet.

**Om något går fel med en version.**

- *"Patienten har skickat en nyare version"*: versionen du granskade är
  ersatt. Inget registrerades. Granska den nya versionen.
- *"Någon annan har redan beslutat om den här versionen"*: en kollega hann
  före. Inget registrerades.
- *"CDR saknar granskning av patientkort"*: den anslutna CDR har ännu inte
  version 2. Enskilda svar kan fortfarande granskas.

## 7b. Bjud in till patientappen (för vårdpersonal, #1050)

Patienter kommer in i patientappen **bara genom en inbjudan från sin
vårdenhet**. Inbjudan görs för den patient du har öppen.

1. Välj patienten i patientlistan. På patientens sida finns knappen
   **Bjud in till patientappen**.
2. Sidan visar patientens **namn** (från patientregistret, eps). Du skriver
   inte in namn eller personnummer.
3. **Vårdenhet.** Bara vårdenheter där du är affilierad *och* där patienten är
   listad i patientregistret kan väljas. Finns en sådan är den redan vald.
   Finns flera måste du välja en; ingen är förvald. Är patienten inte listad
   vid någon av dina vårdenheter säger sidan det, och ingen inbjudan kan göras
   härifrån.
4. **Mobilnummer.** Numret i patientregistret är ifyllt: kontrollera det med
   patienten och ändra om det inte stämmer. Saknas nummer skriver du in det.
   Ett ändrat nummer används bara för det här meddelandet och sparas inte i
   patientregistret.
5. Välj **Fortsätt**, kontrollera patient, vårdenhet och mobil, och välj
   **Bekräfta och skapa inbjudan**. Inget skapas förrän du bekräftat.
6. Välj **Skicka inbjudan i Meddelanden**. Meddelanden öppnas med patientens
   nummer och texten ifylld – tryck skicka. Sidan kan inte se om meddelandet
   skickades. Öppnas inte Meddelanden (till exempel på en dator utan
   Meddelanden), använd **Kopiera texten** eller **Kopiera länken** och ge
   länken till patienten på annat sätt.

**Länken visas bara en gång.** Den kan användas en gång och gäller i sju dagar.
Patienten öppnar länken, skriver sitt personnummer (det kontrolleras mot
patientregistret), väljer e-post och lösenord och lägger till en passkey.

**Om det inte går**

| sidan säger | betyder | gör så här |
|---|---|---|
| Öppna patienten först | Sidan öppnades utan att patienten är öppen | Välj patienten i patientlistan och välj knappen där |
| Patienten är inte listad vid din vårdenhet | Patienten finns inte vid någon av dina vårdenheter i patientregistret | Inbjudan görs från en vårdenhet där patienten är listad |
| Patienten har redan ett konto | Patienten är redan kopplad | Ingen ny inbjudan behövs; patienten loggar in |
| Patienten finns inte i patientregistret, eller ligger utanför … | eps visar inte patienten för dig | Kontrollera att du har öppnat rätt patient |
| Patientregistret har inget fullständigt namn | Namnet saknas i eps | Namnet behöver finnas i patientregistret först |
| Patientregistret (eps) / Inloggningstjänsten (sso) svarade inte | En tjänst nåddes inte | Ingenting har gjorts. Försök igen om en stund |
| Ange ett svenskt mobilnummer | Numret är inte ett svenskt mobilnummer | Skriv 07… eller +467… |

## 8. Getting help
- Runtime logs: `results/<timestamp>_results/app.log` (local) or
  `shared/logs/gunicorn.*.log` (server).
- Technical details: `docs/technical.md`
- Deployment plan + progress: `readme.md`, `progress.md`
- For population / cohort work, see the **analyse.pdhc** manuals.
