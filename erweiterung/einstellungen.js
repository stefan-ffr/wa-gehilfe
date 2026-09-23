// Felder, die beim Speichern von Schraegstrichen am Ende befreit werden.
const ADRESSFELDER = ['dienst'];
const FELDER = ['dienst', 'token', 'profil', 'cfKennung', 'cfGeheimnis'];

const STANDARD = {
  dienst: 'http://localhost:8099',
  profil: 'standard',
  token: '',
  cfKennung: '',
  cfGeheimnis: '',
};

// Sprache nach dem Browser: Deutsch bleibt Vorgabe, alles andere Englisch.
const SPRACHE = (navigator.language || 'de').toLowerCase().startsWith('de') ? 'de' : 'en';
const T = (de, en) => (SPRACHE === 'en' ? en : de);

// Die Seite traegt Deutsch im Markup und Englisch in data-en.
if (SPRACHE === 'en') {
  document.querySelectorAll('[data-en]').forEach((el) => { el.innerHTML = el.dataset.en; });
  document.querySelectorAll('[data-en-placeholder]').forEach((el) => { el.placeholder = el.dataset.enPlaceholder; });
}

chrome.storage.sync.get(STANDARD)
  .then((w) => FELDER.forEach((f) => (document.getElementById(f).value = w[f])));

document.getElementById('speichern').addEventListener('click', async () => {
  const werte = Object.fromEntries(FELDER.map((f) => {
    let v = document.getElementById(f).value.trim();
    if (ADRESSFELDER.includes(f)) v = v.replace(/\/+$/, '');
    return [f, v];
  }));
  await chrome.storage.sync.set(werte);
  const s = document.getElementById('status');
  // Ehrlich bleiben: ohne Token laeuft nichts, das soll man hier schon sehen
  // und nicht erst als 401 mitten im Chat.
  s.textContent = werte.token
    ? T('Gespeichert.', 'Saved.')
    : T('Gespeichert — aber ohne Zugangstoken weist der Dienst jede Anfrage ab.',
        'Saved — but without an access token the service rejects every request.');
});
